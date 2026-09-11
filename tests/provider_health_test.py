"""Shared failures block admission across tickets, without granting ticket capacity."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from provider_health import ProviderHealth, validate_native_command, HealthError


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.health = ProviderHealth(self.root)

    def test_outage_shared_and_probe_serialized(self):
        self.health.failure("openai", "rate_limited", retry_after=90)
        self.assertEqual(self.health.status("openai")["state"], "rate_limited")
        self.assertEqual(self.health.status("anthropic")["state"], "unverified")
        self.assertIsNone(self.health.claim_probe("openai"))
        with patch("provider_health.time.time", return_value=10**10):
            token = self.health.claim_probe("openai")
            self.assertTrue(token)
            self.assertIsNone(self.health.claim_probe("openai"))
            self.health.complete_probe("openai", token, "healthy", route="r")
            self.assertEqual(
                self.health.status("openai", route="r")["state"], "healthy"
            )

    def test_auth_never_cleared_by_normal_success_or_expiry(self):
        self.health.failure("anthropic", "authentication")
        with patch("provider_health.time.time", return_value=10**10):
            self.assertEqual(self.health.status("anthropic")["state"], "authentication")
        self.assertIsNone(self.health.claim_probe("anthropic"))
        token = self.health.claim_probe("anthropic", repair=True)
        self.health.complete_probe("anthropic", token, "healthy", route="r")
        self.assertEqual(
            self.health.status("anthropic", route="other")["state"], "unverified"
        )

    def test_old_probe_cannot_clear_new_incident(self):
        token = self.health.claim_probe("openai")
        self.health.failure("openai", "authentication")
        self.assertFalse(
            self.health.complete_probe("openai", token, "healthy", route="r")
        )
        self.assertEqual(self.health.status("openai")["state"], "authentication")

    def test_role_provider_model_and_overrides(self):
        route = dict(
            provider="anthropic",
            model="claude-test",
            execution="desktop",
            effort="high",
        )
        with self.assertRaises(HealthError):
            validate_native_command(["codex", "exec", "--model", "claude-test"], route)
        with self.assertRaises(HealthError):
            validate_native_command(["claude", "-p", "--model", "wrong"], route)
        with self.assertRaises(HealthError):
            validate_native_command(
                ["claude", "-p", "--model", "claude-test", "--settings", "evil.json"],
                route,
            )
        self.assertEqual(
            validate_native_command(["claude", "-p", "--model", "claude-test"], route)[
                -2:
            ],
            ["--effort", "high"],
        )

    def test_absent_token_never_clears_incident(self):
        self.health.failure("openai", "authentication")
        self.assertFalse(self.health.complete_probe("openai", None, "healthy"))
        self.assertEqual(self.health.status("openai")["state"], "authentication")

    def test_three_failed_probes_require_repair(self):
        for number in range(3):
            with patch("provider_health.time.time", return_value=1000 + number * 100):
                token = self.health.claim_probe("openai")
                self.assertTrue(token)
                self.health.complete_probe("openai", token, "transport")
        with patch("provider_health.time.time", return_value=2000):
            self.assertIsNone(self.health.claim_probe("openai"))
            self.assertTrue(self.health.claim_probe("openai", repair=True))

    def test_corrupt_evidence_fails_closed(self):
        self.health.directory.mkdir(parents=True)
        (self.health.directory / "openai.json").write_text("[]")
        with self.assertRaises(HealthError):
            self.health.status("openai")

    def test_authenticated_probe_honors_retry_after(self):
        from provider_health import probe
        from api_agent import ProviderHTTPError

        config = self.root / "config.yaml"
        config.write_text(
            "llm:\n  execution: api\n  provider: openai\n  model: gpt-test\n"
        )

        class Transport:
            def request(self, *args, **kwargs):
                raise ProviderHTTPError(429, "limited", retry_after_seconds=120)

        with patch("provider_health.time.time", return_value=1000):
            state = probe(self.root, config, transport=Transport())
        self.assertEqual(state["state"], "rate_limited")
        self.assertEqual(state["retry_at"], 1120)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        import subprocess

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        config = self.root / ".orchestration/config.yaml"
        config.parent.mkdir()
        config.write_text(
            "llm:\n  execution: desktop\n  provider: anthropic\n  model: claude-test\n  roles:\n    sprint-worker:\n      provider: openai\n      model: gpt-test\n"
        )
        spec = importlib.util.spec_from_file_location(
            "admission_controller",
            Path(__file__).resolve().parents[1] / "scripts/sprint-controller.py",
        )
        self.c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.c)
        from argparse import Namespace

        self.N = Namespace
        with patch.object(self.c, "project_root", return_value=self.root):
            self.cfg = self.c.settings(Namespace(config=str(config), state_dir=None))
        self.path = self.c.state_path(self.cfg["state_dir"], "1")
        self.ticket = dict(
            key="T-1",
            state="pending",
            raw_status="Ready",
            reason="",
            attempts=0,
            history=[],
            dependencies=[],
            subtasks=[],
            scope_assessment={"verdict": "ready"},
        )
        self.state = dict(
            schema_version=2,
            sprint={"id": "1"},
            tickets={"T-1": self.ticket},
            dependency_status={},
        )
        self.c.save(self.path, self.state)
        # Tests isolate absence of real root grants; no host authority is contacted.
        for name in ["authorized_restart_grant", "authorized_relaunch_ceiling"]:
            p = patch.object(self.c, name, return_value=None)
            p.start()
            self.addCleanup(p.stop)

    def healthy(self, role="sprint-worker"):
        from context_pipeline import llm_route_from_config
        from provider_health import route_identity

        route = llm_route_from_config(self.cfg["config"], role)
        health = ProviderHealth(self.root)
        token = health.claim_probe(route["provider"])
        self.assertTrue(token)
        health.complete_probe(
            route["provider"], token, "healthy", route_identity(route)
        )

    def reserve(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            self.c.reserve(
                self.N(
                    sprint="1",
                    ticket="T-1",
                    run_ref="first",
                    run_id="first",
                    role="implementer",
                    worker_ref="first",
                ),
                self.cfg,
            )

    def test_provider_hold_before_any_attempt_and_role_override(self):
        with self.assertRaisesRegex(self.c.SprintError, "provider admission held"):
            self.reserve()
        self.assertEqual(self.c.load(self.path)["tickets"]["T-1"]["attempts"], 0)
        self.healthy(
            "ticket-scoper"
        )  # Global Anthropic route cannot authorize the OpenAI override.
        with self.assertRaises(self.c.SprintError):
            self.reserve()
        self.healthy()
        self.reserve()
        stored = self.c.load(self.path)["tickets"]["T-1"]
        self.assertEqual(stored["reserved_route"]["provider"], "openai")
        self.assertEqual(stored["attempts"], 1)

    def test_wrong_launcher_preserves_unused_capability(self):
        self.healthy()
        self.reserve()
        ticket = self.c.load(self.path)["tickets"]["T-1"]
        args = self.N(
            sprint="1",
            ticket="T-1",
            command=["claude", "-p", "--model", "gpt-test"],
            output=str(self.root / "out"),
            stdin_file=None,
            attach_capability=ticket["attach_capability"],
        )
        with self.assertRaises(HealthError):
            self.c.launch_local(args, self.cfg)
        after = self.c.load(self.path)["tickets"]["T-1"]
        self.assertEqual(after["attach_capability"], ticket["attach_capability"])
        self.assertFalse(after["launch_evidence"])

    def test_scope_required_with_decomposition_disabled(self):
        self.healthy()
        self.healthy("ticket-scoper")
        self.ticket["scope_assessment"] = {}
        self.c.save(self.path, self.state)
        plan = self.c.plan_value(self.state, self.cfg)
        self.assertEqual(plan["scope"], ["T-1"])
        self.assertEqual(plan["launch"], [])
        with self.assertRaisesRegex(self.c.SprintError, "scoping"):
            self.reserve()

    def test_tracking_parent_binds_existing_children_without_attempt(self):
        import json
        import contextlib
        import io

        self.ticket["subtasks"] = ["T-2"]
        self.state["tickets"]["T-2"] = {**self.ticket, "key": "T-2", "subtasks": []}
        self.c.save(self.path, self.state)
        p = self.root / "assessment.json"
        p.write_text(
            json.dumps(
                dict(
                    schema_version=1,
                    ticket="T-1",
                    verdict="tracking_parent",
                    complexity_score=1,
                    reasons=["existing chain"],
                    slices=[],
                    children=["T-2"],
                )
            )
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.c.record_scope(
                self.N(sprint="1", ticket="T-1", assessment=str(p)), self.cfg
            )
        after = self.c.load(self.path)["tickets"]["T-1"]
        self.assertEqual(after["state"], "decomposed")
        self.assertEqual(after["attempts"], 0)
        self.assertEqual(after["decomposition_children"], ["T-2"])
        self.assertFalse(
            self.c.dependency_complete(self.c.load(self.path), "T-1", self.cfg)
        )

    def test_shared_auth_hold_is_one_provider_problem_no_launch(self):
        self.healthy()
        ProviderHealth(self.root).failure("openai", "authentication")
        result = self.c.plan_value(self.state, self.cfg)
        self.assertFalse(result["launch"])
        self.assertTrue(
            any(h["state"] == "authentication" for h in result["provider_holds"])
        )
        self.assertFalse(
            any(h["provider"] == "openai" for h in result["health_probes"])
        )

    def test_completed_sprint_does_not_schedule_health_work(self):
        self.ticket["state"] = "completed"
        result = self.c.plan_value(self.state, self.cfg)
        self.assertFalse(result["health_probes"])
        self.assertFalse(result["autonomous_work_remaining"])

    def test_batch_provider_mismatch_consumes_no_attempt(self):
        import json

        jobs = self.root / "jobs.json"
        jobs.write_text(
            json.dumps({"provider": "anthropic", "jobs": [{"ticket": "T-1"}]})
        )
        with self.assertRaisesRegex(self.c.SprintError, "batch provider"):
            self.c.prepare_batch(self.N(sprint="1", jobs=str(jobs)), self.cfg)
        self.assertEqual(self.c.load(self.path)["tickets"]["T-1"]["attempts"], 0)

    def test_missing_prerequisite_stops_before_implementation(self):
        import json
        import contextlib
        import io

        assessment = self.root / "assessment.json"
        assessment.write_text(
            json.dumps(
                dict(
                    schema_version=1,
                    ticket="T-1",
                    verdict="ready",
                    complexity_score=0,
                    reasons=["requires another ticket"],
                    prerequisites=["T-2"],
                    slices=[],
                )
            )
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.c.record_scope(
                self.N(sprint="1", ticket="T-1", assessment=str(assessment)), self.cfg
            )
        ticket = self.c.load(self.path)["tickets"]["T-1"]
        self.assertEqual(ticket["state"], "operator_decision")
        self.assertEqual(ticket["attempts"], 0)
        self.assertIn("T-2", ticket["reason"])

    def test_authenticated_link_repair_only_clears_dependency_decision(self):
        import contextlib
        import io
        import copy

        inventory = self.root / "inventory.json"
        inventory.write_text("{}")
        for decision, expected in [
            ("product", "operator_decision"),
            ("dependency_reconciliation", "pending"),
        ]:
            self.ticket.update(
                state="operator_decision",
                scope_assessment={
                    "verdict": "operator_decision",
                    "decision_kind": decision,
                    "missing_dependencies": ["T-2"],
                },
            )
            self.c.save(self.path, self.state)
            fresh = copy.deepcopy(self.c.load(self.path)["tickets"]["T-1"])
            fresh.update(state="pending", dependencies=["T-2"], scope_assessment={})
            incoming = dict(
                project="T",
                sprint={"id": "1"},
                source_query="parents",
                subtask_source_query="children",
                subtask_keys=[],
                dependency_status={"T-2": "Done"},
                tickets={"T-1": fresh},
            )
            with (
                patch.object(self.c, "normalized_inventory", return_value=incoming),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.c.sync(
                    self.N(inventory=str(inventory), inventory_template=None), self.cfg
                )
            result = self.c.load(self.path)["tickets"]["T-1"]
            self.assertEqual(result["state"], expected)
            self.assertEqual(result["attempts"], 0)
            if expected == "pending":
                self.assertFalse(result["scope_assessment"])

    def test_batch_wrong_model_is_rejected_before_budget_reservation(self):
        import json

        config = self.cfg["config"]
        config.write_text(
            "llm:\n  execution: api\n  provider: openai\n  model: gpt-test\n"
        )
        self.healthy()
        jobs = self.root / "jobs.json"
        jobs.write_text(
            json.dumps(
                dict(
                    provider="openai",
                    jobs=[
                        dict(
                            ticket="T-1",
                            background=True,
                            interactive=False,
                            params=dict(
                                model="wrong-model",
                                max_output_tokens=10,
                                input=[dict(role="user", content="test")],
                            ),
                        )
                    ],
                )
            )
        )
        with self.assertRaisesRegex(self.c.SprintError, "batch model"):
            self.c.prepare_batch(self.N(sprint="1", jobs=str(jobs)), self.cfg)
        self.assertEqual(self.c.load(self.path)["tickets"]["T-1"]["attempts"], 0)

    def test_api_attempt_rejects_a_changed_route(self):
        from attempt_capability import validate, AttemptCapabilityError

        self.healthy()
        self.reserve()
        item = self.c.load(self.path)["tickets"]["T-1"]
        args = dict(
            state_dir=self.cfg["state_dir"],
            token=item["attempt_capability"]["token"],
            repository=str(self.root),
            sprint="1",
            ticket="T-1",
            role="implementer",
            run_id="first",
            worker="first",
        )
        validate(**args, route=item["reserved_route"])
        with self.assertRaisesRegex(AttemptCapabilityError, "route"):
            validate(**args, route={**item["reserved_route"], "model": "wrong-model"})


class DependencyTests(unittest.TestCase):
    def test_explicit_sections_only(self):
        from ticket_dependencies import declared_dependencies

        self.assertEqual(
            declared_dependencies(
                "Related: T-99\nPrerequisites:\n- T-1 must merge\n- T-2\n\nNotes: T-88"
            ),
            ["T-1", "T-2"],
        )
        self.assertEqual(
            declared_dependencies(
                "Depends on: T-3, T-4\nImplementation references T-55"
            ),
            ["T-3", "T-4"],
        )
        self.assertEqual(
            declared_dependencies("Does not depend on T-1\nRelated changes: T-2"), []
        )


class InstalledClientTests(unittest.TestCase):
    @unittest.skipUnless(__import__("shutil").which("codex"), "Codex is not installed")
    def test_forced_compaction_and_tool_use_are_metered(self):
        from runtime_smoke import check

        result = check("openai", "gpt-5.5")
        self.assertEqual(result["compaction"], "metered-responses")
        self.assertGreaterEqual(result["generations"], 3)


if __name__ == "__main__":
    unittest.main()
