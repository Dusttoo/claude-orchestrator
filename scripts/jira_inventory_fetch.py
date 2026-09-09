#!/usr/bin/env python3
"""Fetch exhaustive Jira inventory evidence through an authenticated adapter."""

from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def origin(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "Jira base URL must be an HTTPS origin without embedded credentials"
        )
    return f"{parsed.scheme}://{parsed.hostname.lower()}{':' + str(parsed.port) if parsed.port else ''}"


def network_fetch(
    base_url: str, jql: str, start_at: int, max_results: int, cursor: str
) -> dict[str, Any]:
    approved = origin(base_url)
    url = urljoin(base_url.rstrip("/") + "/", "rest/api/3/search/jql")
    if origin(url) != approved:
        raise ValueError("Jira request escaped the approved origin")
    token = os.environ.get("JIRA_API_TOKEN", "")
    if not token:
        raise ValueError("JIRA_API_TOKEN is required")
    email = os.environ.get("JIRA_EMAIL", "")
    auth = (
        "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        if email
        else "Bearer " + token
    )
    query = {"jql": jql, "maxResults": max_results}
    if cursor:
        query["nextPageToken"] = cursor
    request = Request(
        url + "?" + urlencode(query),
        headers={"Accept": "application/json", "Authorization": auth},
    )
    with urlopen(request, timeout=30) as response:  # nosec B310 -- approved origin checked before and after redirects
        if origin(response.geturl()) != approved:
            raise ValueError("Jira redirect escaped the approved origin")
        return json.loads(response.read())


def exhaustive(
    fetch: Any, jql: str, raw_dir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    start_at = 0
    cursor = ""
    pages = []
    issues = []
    while True:
        response = fetch(jql, start_at, 100, cursor)
        page_issues = response.get("issues")
        response_start = response.get("startAt", start_at)
        if not isinstance(page_issues, list) or response_start != start_at:
            raise ValueError("Jira returned invalid or non-contiguous pagination")
        raw = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        raw_path = raw_dir / f"sha256-{digest}.json"
        if not raw_path.exists():
            write_json(raw_path, response)
        pages.append(
            {
                "start_at": start_at,
                "count": len(page_issues),
                "total": response.get("total"),
                "item_keys": [str(x["key"]).upper() for x in page_issues],
                "terminal": bool(response.get("isLast", False)),
                "cursor_in": cursor,
                "cursor_out": str(response.get("nextPageToken") or ""),
                "raw_sha256": digest,
                "raw_path": str(raw_path.resolve()),
            }
        )
        issues.extend(page_issues)
        next_start = start_at + len(page_issues)
        total = response.get("total")
        next_cursor = str(response.get("nextPageToken") or "")
        terminal = bool(response.get("isLast", False)) or (
            isinstance(total, int) and next_start >= total
        )
        if terminal:
            if isinstance(total, int) and next_start != total:
                raise ValueError("Jira terminated before the declared total")
            break
        if not next_cursor and total is None:
            raise ValueError(
                "Jira response does not prove exhaustion or provide a next cursor"
            )
        if next_start <= start_at:
            raise ValueError("Jira pagination made no progress")
        if next_cursor and next_cursor == cursor:
            raise ValueError("Jira pagination repeated its cursor")
        start_at = next_start
        cursor = next_cursor
    return pages, issues


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inventory-template", required=True)
    p.add_argument("--artifact", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--base-url", default=os.environ.get("JIRA_BASE_URL", ""))
    p.add_argument("--test-transport", help=argparse.SUPPRESS)
    a = p.parse_args()
    inventory = json.loads(Path(a.inventory_template).read_text())
    raw_dir = Path(a.artifact).resolve().parent / "jira-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    authority = "provider-network"
    approved = origin(a.base_url) if not a.test_transport else "test-only"
    if a.test_transport:
        authority = "test-only"
        supplied = json.loads(Path(a.test_transport).read_text())
        positions = {"parents": 0, "children": 0}

        def fetch(jql: str, start: int, maximum: int, cursor: str) -> dict[str, Any]:
            kind = "parents" if jql == inventory["source_query"] else "children"
            page = supplied[kind][positions[kind]]
            positions[kind] += 1
            return page
    else:
        if not a.base_url:
            raise ValueError("JIRA_BASE_URL is required")

        def fetch(jql: str, start: int, maximum: int, cursor: str) -> dict[str, Any]:
            return network_fetch(a.base_url, jql, start, maximum, cursor)

    parent_pages, parents = exhaustive(fetch, inventory["source_query"], raw_dir)
    child_pages, children = exhaustive(
        fetch, inventory["subtask_source_query"], raw_dir
    )
    relations = [
        {"parent": str(issue["key"]).upper(), "child": str(child["key"]).upper()}
        for issue in parents
        for child in ((issue.get("fields") or {}).get("subtasks") or [])
    ]
    child_parents = {
        str(issue["key"]).upper(): str(
            (((issue.get("fields") or {}).get("parent") or {}).get("key")) or ""
        ).upper()
        for issue in children
    }
    artifact = {
        "schema_version": 2,
        "adapter": "jira-rest-v3",
        "authority": authority,
        "approved_origin": approved,
        "queries": [
            {
                "kind": "parents",
                "jql": inventory["source_query"],
                "pages": parent_pages,
            },
            {
                "kind": "children",
                "jql": inventory["subtask_source_query"],
                "pages": child_pages,
            },
        ],
        "relations": sorted(relations, key=lambda x: (x["parent"], x["child"])),
        "child_parents": dict(sorted(child_parents.items())),
    }
    digest = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    artifact_path = Path(a.artifact).resolve()
    write_json(artifact_path, artifact)
    inventory["fetch_artifact"] = {"path": str(artifact_path), "sha256": digest}
    write_json(Path(a.output), inventory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
