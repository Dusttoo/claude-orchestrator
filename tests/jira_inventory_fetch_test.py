#!/usr/bin/env python3
"""Adversarial tests for the authenticated Jira inventory adapter."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "jira_inventory_fetch", ROOT / "scripts/jira_inventory_fetch.py"
)
assert SPEC and SPEC.loader
jira = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jira)


class JiraInventoryFetchTest(unittest.TestCase):
    def test_cross_origin_redirect_is_rejected_before_authorization_can_follow(
        self,
    ) -> None:
        handler = jira.ApprovedOriginRedirectHandler("https://jira.example")
        request = Request(
            "https://jira.example/rest/api/3/search/jql",
            headers={"Authorization": "Bearer secret"},
        )
        with self.assertRaisesRegex(ValueError, "cross-origin redirect"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://attacker.example/collect",
            )

    def test_same_origin_redirect_preserves_auth_and_cross_origin_never_does(
        self,
    ) -> None:
        handler = jira.ApprovedOriginRedirectHandler("https://jira.example")
        request = Request(
            "https://jira.example/old",
            headers={"Authorization": "Bearer secret", "Accept": "application/json"},
        )
        redirected = handler.redirect_request(
            request, None, 307, "Temporary Redirect", {}, "https://jira.example/new"
        )
        self.assertEqual(redirected.get_header("Authorization"), "Bearer secret")
        with self.assertRaises(ValueError):
            handler.redirect_request(
                request, None, 307, "Temporary Redirect", {}, "https://evil.example/new"
            )

    def test_inventory_is_derived_from_pages_not_conflicting_template(self) -> None:
        template = {
            "project": "EVIL",
            "sprint": {"id": "forged", "name": "forged"},
            "source_query": "project = PROJ AND sprint = 42",
            "subtask_source_query": "parent in (PROJ-1)",
            "tickets": [{"key": "EVIL-9", "status": "Done", "dependencies": []}],
            "dependency_status": {"EXT-9": "Done"},
        }
        root_issue = {
            "key": "PROJ-1",
            "fields": {
                "summary": "provider summary",
                "status": {"name": "Ready"},
                "priority": {"id": "2"},
                "sprint": {"id": "42", "name": "Provider Sprint"},
                "subtasks": [{"key": "PROJ-2"}],
                "issuelinks": [
                    {
                        "type": {"name": "Blocks"},
                        "outwardIssue": {"key": "EXT-9"},
                    }
                ],
            },
        }
        child = {
            "startAt": 0,
            "total": 1,
            "isLast": True,
            "issues": [
                {
                    "key": "PROJ-2",
                    "fields": {
                        "summary": "child",
                        "status": {"name": "Ready"},
                        "priority": None,
                        "sprint": {"id": "42", "name": "Provider Sprint"},
                        "parent": {"key": "PROJ-1"},
                        "subtasks": [],
                        "issuelinks": [],
                    },
                }
            ],
        }
        parent = {
            "startAt": 0,
            "total": 2,
            "isLast": True,
            "issues": [root_issue, child["issues"][0]],
        }
        external = {
            "startAt": 0,
            "total": 1,
            "isLast": True,
            "issues": [{"key": "EXT-9", "fields": {"status": {"name": "In Progress"}}}],
        }
        pages = {"parents": [parent], "children": [child], "external": [external]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, artifact = jira.build_inventory(
                template,
                jira.fixture_fetch(pages, template),
                root / "raw",
                authority="test-only",
                approved_origin="test-only",
                fields=jira.required_fields("sprint"),
                sprint_field="sprint",
                dependency_links=[{"type": "Blocks", "blocked_side": "inward"}],
            )
        self.assertEqual(output["project"], "PROJ")
        self.assertEqual(output["sprint"], {"id": "42", "name": "Provider Sprint"})
        self.assertEqual(
            [item["key"] for item in output["tickets"]], ["PROJ-1", "PROJ-2"]
        )
        self.assertEqual(output["tickets"][0]["dependencies"], ["EXT-9"])
        self.assertEqual(output["dependency_status"], {"EXT-9": "In Progress"})
        self.assertNotIn("EVIL-9", json.dumps(output))
        self.assertEqual(artifact["authority"], "test-only")

    def test_truncated_total_and_contradictory_relations_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary)
            with self.assertRaisesRegex(ValueError, "declared total"):
                jira.exhaustive(
                    lambda *_: {"startAt": 0, "total": 2, "isLast": True, "issues": []},
                    "q",
                    "parents",
                    raw,
                    ["key"],
                )
        parent = {"key": "PROJ-1", "fields": {"subtasks": [{"key": "PROJ-2"}]}}
        child = {"key": "PROJ-2", "fields": {"parent": {"key": "PROJ-999"}}}
        with self.assertRaisesRegex(ValueError, "parent/child"):
            jira.validate_relations([parent], [child])

    def test_external_dependency_response_must_match_requested_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "external dependency"):
            jira.external_statuses(
                ["EXT-9"],
                [{"key": "EXT-10", "fields": {"status": {"name": "Done"}}}],
            )

    def test_evidence_keeps_only_pagination_and_requested_issue_fields(self) -> None:
        page = jira.sanitize_page(
            {
                "startAt": 0,
                "total": 1,
                "isLast": True,
                "expand": "schema,names",
                "warningMessages": ["large"],
                "issues": [
                    {
                        "key": "PROJ-1",
                        "changelog": {"histories": [1]},
                        "fields": {"summary": "small", "description": "discard"},
                    }
                ],
            },
            ["key", "summary"],
        )
        self.assertEqual(set(page), {"startAt", "total", "isLast", "issues"})
        self.assertEqual(page["issues"][0]["fields"], {"summary": "small"})


if __name__ == "__main__":
    unittest.main()
