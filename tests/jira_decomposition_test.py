import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import jira_decomposition as decomposition  # noqa: E402


class JiraDecompositionTests(unittest.TestCase):
    def config(self):
        return {
            "ticket": {"kind": "jira", "project": "PROJ"},
            "jira_base_url": "https://jira.example",
            "sprint_decomposition": {
                "auto_decompose_large_tickets": True,
                "max_auto_slices": 4,
                "jira_child_issue_type": "Sub-task",
            },
        }

    def assessment(self):
        return {
            "schema_version": 1,
            "ticket": "PROJ-1",
            "verdict": "decompose",
            "complexity_score": 80,
            "reasons": ["two release boundaries"],
            "slices": [
                {
                    "id": "foundation",
                    "summary": "Foundation",
                    "behavior": "create the additive foundation",
                    "acceptance_criteria": ["foundation is independently testable"],
                    "depends_on": [],
                },
                {
                    "id": "cutover",
                    "summary": "Cutover",
                    "behavior": "activate the foundation",
                    "acceptance_criteria": ["cutover preserves compatibility"],
                    "depends_on": ["foundation"],
                },
            ],
        }

    def test_accepts_bounded_acyclic_slices(self):
        project, parent, slices, feature = decomposition.validated_input(
            self.config(), self.assessment()
        )
        self.assertEqual((project, parent), ("PROJ", "PROJ-1"))
        self.assertEqual([item["id"] for item in slices], ["foundation", "cutover"])
        self.assertEqual(feature["max_auto_slices"], 4)

    def test_rejects_cycles(self):
        value = self.assessment()
        value["slices"][0]["depends_on"] = ["cutover"]
        with self.assertRaisesRegex(decomposition.DecompositionError, "cycle"):
            decomposition.validated_input(self.config(), value)

    def test_requires_explicit_repository_opt_in(self):
        config = self.config()
        config["sprint_decomposition"]["auto_decompose_large_tickets"] = False
        with self.assertRaisesRegex(decomposition.DecompositionError, "not enabled"):
            decomposition.validated_input(config, self.assessment())

    def test_adf_preserves_behavior_and_acceptance_criteria(self):
        value = decomposition.adf(self.assessment()["slices"][0], "PROJ-1")
        text = [block["content"][0]["text"] for block in value["content"]]
        self.assertIn("Automatically decomposed from PROJ-1.", text)
        self.assertIn("- foundation is independently testable", text)

    def test_dependency_idempotency_requires_the_configured_direction(self):
        jira = object.__new__(decomposition.Jira)
        calls = []
        jira.issue_links = lambda _key: [
            {
                "type": {"name": "Blocks"},
                "outwardIssue": {"key": "PROJ-2"},
                "inwardIssue": {"key": "PROJ-1"},
            }
        ]
        jira.request = lambda method, path, body=None: calls.append((method, path, body)) or {}
        self.assertFalse(
            jira.ensure_dependency(
                blocked="PROJ-1",
                prerequisite="PROJ-2",
                link_type="Blocks",
                blocked_side="inward",
            )
        )
        self.assertEqual(calls, [])

        self.assertTrue(
            jira.ensure_dependency(
                blocked="PROJ-1",
                prerequisite="PROJ-2",
                link_type="Blocks",
                blocked_side="outward",
            )
        )
        self.assertEqual(calls[0][0:2], ("POST", "rest/api/3/issueLink"))


if __name__ == "__main__":
    unittest.main()
