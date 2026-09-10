#!/usr/bin/env python3
"""Create bounded, idempotently discoverable Jira children from a scope assessment."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, build_opener

from api_agent import load_yaml
from jira_inventory_fetch import (
    ApprovedOriginRedirectHandler,
    url_origin,
    validate_base_url,
    write_json,
)


class DecompositionError(RuntimeError):
    pass


def auth_headers() -> dict[str, str]:
    token = os.environ.get("JIRA_API_TOKEN", "")
    if not token:
        raise DecompositionError("JIRA_API_TOKEN is required")
    email = os.environ.get("JIRA_EMAIL", "")
    authorization = (
        "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        if email
        else "Bearer " + token
    )
    return {
        "Accept": "application/json",
        "Authorization": authorization,
        "Content-Type": "application/json",
    }


def adf(slice_: dict[str, Any], parent: str) -> dict[str, Any]:
    paragraphs = [
        f"Automatically decomposed from {parent}.",
        "Behavior: " + str(slice_["behavior"]).strip(),
        "Acceptance criteria:",
        *[f"- {value.strip()}" for value in slice_["acceptance_criteria"]],
    ]
    return {
        "version": 1,
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": paragraph}],
            }
            for paragraph in paragraphs
        ],
    }


class Jira:
    def __init__(self, base_url: str) -> None:
        self.origin = validate_base_url(base_url)
        self.base_url = base_url.rstrip("/") + "/"
        self.headers = auth_headers()
        self.opener = build_opener(ApprovedOriginRedirectHandler(self.origin))

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        endpoint = urljoin(self.base_url, path.lstrip("/"))
        if url_origin(endpoint) != self.origin:
            raise DecompositionError("Jira request escaped the configured origin")
        request = Request(
            endpoint,
            data=(json.dumps(body).encode() if body is not None else None),
            headers=self.headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                if url_origin(response.geturl()) != self.origin:
                    raise DecompositionError("Jira response escaped the configured origin")
                payload = response.read()
                return json.loads(payload) if payload else {}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise DecompositionError(f"Jira {method} {path} failed ({exc.code}): {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DecompositionError(f"Jira {method} {path} outcome is uncertain: {exc}") from exc

    def find_child(self, parent: str, label: str) -> str | None:
        jql = f'parent = {parent} AND labels = "{label}"'
        query = urlencode({"jql": jql, "fields": "key", "maxResults": 2})
        result = self.request("GET", "rest/api/3/search/jql?" + query)
        issues = result.get("issues", [])
        if not isinstance(issues, list):
            raise DecompositionError("Jira child lookup returned an invalid issue list")
        keys = [str(issue.get("key") or "").upper() for issue in issues]
        if len(keys) > 1:
            raise DecompositionError(f"multiple Jira children carry idempotency label {label}")
        return keys[0] if keys else None

    def create_child(
        self,
        *,
        project: str,
        parent: str,
        issue_type: str,
        slice_: dict[str, Any],
        label: str,
    ) -> str:
        body = {
            "fields": {
                "project": {"key": project},
                "parent": {"key": parent},
                "issuetype": {"name": issue_type},
                "summary": str(slice_["summary"]).strip(),
                "description": adf(slice_, parent),
                "labels": ["orchestration-slice", label],
            }
        }
        try:
            result = self.request("POST", "rest/api/3/issue", body)
        except DecompositionError as exc:
            # POST may have been accepted before a transport failure. Re-query
            # the deterministic label before deciding whether a retry is safe.
            recovered = self.find_child(parent, label)
            if recovered:
                return recovered
            raise exc
        key = str(result.get("key") or "").upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key):
            raise DecompositionError("Jira create response omitted a valid child key")
        return key

    def issue_links(self, key: str) -> list[dict[str, Any]]:
        result = self.request("GET", f"rest/api/3/issue/{key}?fields=issuelinks")
        links = ((result.get("fields") or {}).get("issuelinks") or [])
        if not isinstance(links, list):
            raise DecompositionError(f"Jira issue {key} returned invalid links")
        return links

    def ensure_dependency(
        self, *, blocked: str, prerequisite: str, link_type: str, blocked_side: str
    ) -> bool:
        for link in self.issue_links(blocked):
            type_name = str((link.get("type") or {}).get("name") or "")
            inward = str((link.get("inwardIssue") or {}).get("key") or "").upper()
            outward = str((link.get("outwardIssue") or {}).get("key") or "").upper()
            correct_direction = (
                inward == blocked and outward == prerequisite
                if blocked_side == "inward"
                else outward == blocked and inward == prerequisite
            )
            if type_name.casefold() == link_type.casefold() and correct_direction:
                return False
        body: dict[str, Any] = {"type": {"name": link_type}}
        if blocked_side == "inward":
            body.update({"inwardIssue": {"key": blocked}, "outwardIssue": {"key": prerequisite}})
        else:
            body.update({"outwardIssue": {"key": blocked}, "inwardIssue": {"key": prerequisite}})
        try:
            self.request("POST", "rest/api/3/issueLink", body)
        except DecompositionError as exc:
            for link in self.issue_links(blocked):
                type_name = str((link.get("type") or {}).get("name") or "")
                inward = str((link.get("inwardIssue") or {}).get("key") or "").upper()
                outward = str((link.get("outwardIssue") or {}).get("key") or "").upper()
                correct_direction = (
                    inward == blocked and outward == prerequisite
                    if blocked_side == "inward"
                    else outward == blocked and inward == prerequisite
                )
                if type_name.casefold() == link_type.casefold() and correct_direction:
                    return False
            raise exc
        return True


def validated_input(config: dict[str, Any], assessment: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
    feature = config.get("sprint_decomposition") or {}
    if not isinstance(feature, dict):
        raise DecompositionError("sprint_decomposition must be a map")
    if feature.get("auto_decompose_large_tickets") is not True:
        raise DecompositionError("automatic decomposition is not enabled by repository policy")
    if assessment.get("schema_version") != 1 or assessment.get("verdict") != "decompose":
        raise DecompositionError("assessment must be a schema-v1 decompose verdict")
    ticket_policy = config.get("ticket") or {}
    if not isinstance(ticket_policy, dict) or ticket_policy.get("kind") != "jira":
        raise DecompositionError("automatic decomposition requires ticket.kind jira")
    parent = str(assessment.get("ticket") or "").upper()
    project = str((ticket_policy.get("project") or "")).upper()
    if (
        not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", parent)
        or not project
        or not parent.startswith(project + "-")
    ):
        raise DecompositionError("assessment ticket and configured Jira project are required")
    slices = assessment.get("slices")
    maximum = min(int(feature.get("max_auto_slices", 6)), 10)
    threshold = int(feature.get("complexity_threshold", 70))
    if not isinstance(slices, list) or not 2 <= len(slices) <= maximum:
        raise DecompositionError(f"assessment must contain 2 through {maximum} slices")
    score = assessment.get("complexity_score")
    if isinstance(score, bool) or not isinstance(score, int) or score < threshold:
        raise DecompositionError(
            f"assessment complexity must reach configured threshold {threshold}"
        )
    identifiers: set[str] = set()
    for item in slices:
        if not isinstance(item, dict):
            raise DecompositionError("every decomposition slice must be an object")
        identifier = str(item.get("id") or "")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", identifier) or identifier in identifiers:
            raise DecompositionError(f"invalid or duplicate slice id: {identifier!r}")
        identifiers.add(identifier)
        if not str(item.get("summary") or "").strip() or not str(item.get("behavior") or "").strip():
            raise DecompositionError(f"slice {identifier} requires summary and behavior")
        if len(str(item["summary"])) > 255 or len(str(item["behavior"])) > 8000:
            raise DecompositionError(f"slice {identifier} exceeds Jira field limits")
        criteria = item.get("acceptance_criteria")
        if not isinstance(criteria, list) or not criteria or any(
            not isinstance(value, str) or not value.strip() for value in criteria
        ):
            raise DecompositionError(f"slice {identifier} requires acceptance criteria")
        if len(criteria) > 30 or any(len(value) > 2000 for value in criteria):
            raise DecompositionError(f"slice {identifier} acceptance criteria exceed limits")
    for item in slices:
        dependencies = item.get("depends_on", [])
        if not isinstance(dependencies, list) or item["id"] in dependencies:
            raise DecompositionError(f"slice {item['id']} has invalid dependencies")
        unknown = set(dependencies) - identifiers
        if unknown:
            raise DecompositionError(
                f"slice {item['id']} depends on unknown slices: {', '.join(sorted(unknown))}"
            )
    graph = {item["id"]: set(item.get("depends_on", [])) for item in slices}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise DecompositionError("decomposition dependencies contain a cycle")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in graph[identifier]:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in graph:
        visit(identifier)
    return project, parent, slices, feature


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--assessment", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = load_yaml(Path(args.config).resolve())
    assessment = json.loads(Path(args.assessment).read_text(encoding="utf-8"))
    project, parent, slices, feature = validated_input(config, assessment)
    labels = {
        item["id"]: f"orchestration-slice-{parent.casefold()}-{item['id']}"
        for item in slices
    }
    output: dict[str, Any] = {
        "schema_version": 1,
        "parent": parent,
        "mode": "apply" if args.apply else "plan",
        "slices": [
            {"id": item["id"], "summary": item["summary"], "label": labels[item["id"]]}
            for item in slices
        ],
    }
    if args.apply:
        base_url = str(config.get("jira_base_url") or "")
        jira = Jira(base_url)
        issue_type = str(feature.get("jira_child_issue_type") or "Sub-task")
        keys: dict[str, str] = {}
        for item in slices:
            label = labels[item["id"]]
            keys[item["id"]] = jira.find_child(parent, label) or jira.create_child(
                project=project,
                parent=parent,
                issue_type=issue_type,
                slice_=item,
                label=label,
            )
        links = config.get("sprint_dependency_links") or [{"type": "Blocks", "blocked_side": "inward"}]
        link = links[0]
        linked = []
        for item in slices:
            for dependency in item.get("depends_on", []):
                created = jira.ensure_dependency(
                    blocked=keys[item["id"]],
                    prerequisite=keys[dependency],
                    link_type=str(link.get("type") or "Blocks"),
                    blocked_side=str(link.get("blocked_side") or "inward"),
                )
                linked.append({"blocked": keys[item["id"]], "prerequisite": keys[dependency], "created": created})
        output["children"] = [keys[item["id"]] for item in slices]
        output["links"] = linked
    destination = Path(args.output)
    write_json(destination, output)
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DecompositionError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"jira-decomposition: {exc}", file=sys.stderr)
        raise SystemExit(2)
