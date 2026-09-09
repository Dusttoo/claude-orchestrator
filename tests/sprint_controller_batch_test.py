import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "sprint_controller", ROOT / "scripts" / "sprint-controller.py"
)
controller = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(controller)


class SprintControllerBatchTests(unittest.TestCase):
    def config(self, root):
        orchestration = root / ".orchestration"
        state_dir = orchestration / ".sprint-state"
        state_dir.mkdir(parents=True)
        config = orchestration / "config.yaml"
        shutil.copy(ROOT / "templates" / "config.yaml", config)
        return {"state_dir": state_dir, "shared_root": root, "config": config}

    def test_v1_batch_marker_migrates_to_fenced_operator_path(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self.config(Path(temp))
            marker = cfg["state_dir"] / "batch-old.state.json"
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "batch_id": "old",
                        "provider": "openai",
                        "status": "submitted",
                        "provider_batch_id": "batch_old",
                        "jobs": [{"reservation_id": "resv_old"}],
                    }
                )
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                controller.inspect_batch(argparse.Namespace(batch="old"), cfg)
            inspected = json.loads(output.getvalue())
            self.assertEqual(inspected["status"], "legacy_uncertain")
            self.assertTrue(inspected["reservations_fenced"])
            with contextlib.redirect_stdout(io.StringIO()):
                controller.recover_legacy_batch(
                    argparse.Namespace(batch="old", reason="provider checked manually"),
                    cfg,
                )
            migrated = json.loads(marker.read_text())
            self.assertEqual(migrated["status"], "legacy_operator_action")
            self.assertFalse(migrated["reservations_released"])
            self.assertFalse(
                (Path(temp) / ".orchestration/.llm-usage/usage.jsonl").exists()
            )

    def test_partial_terminal_bundle_settles_success_and_keeps_ambiguity_reserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root)
            batch_id = "partial"
            request = cfg["state_dir"] / "batch-partial.jsonl"
            request.write_text("{}\n")
            request_sha = hashlib.sha256(request.read_bytes()).hexdigest()
            raw_value = {"id": "batch_remote", "object": "batch"}
            raw_sha = controller.hashlib.sha256(
                json.dumps(raw_value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            raw_path = cfg["state_dir"] / "batch-raw" / f"sha256-{raw_sha}.json"
            raw_path.parent.mkdir()
            raw_path.write_text(json.dumps(raw_value))
            receipt = {
                "provider": "openai",
                "provider_batch_id": "batch_remote",
                "request_sha256": request_sha,
                "raw": {"path": str(raw_path), "sha256": raw_sha},
            }
            receipt["sha256"] = hashlib.sha256(
                json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            jobs = [
                {
                    "custom_id": "job-a",
                    "ticket": "PROJ-1",
                    "reservation_id": "resv-a",
                    "run_id": "run-a",
                    "run_ref": "worker-a",
                },
                {
                    "custom_id": "job-b",
                    "ticket": "PROJ-2",
                    "reservation_id": "resv-b",
                    "run_id": "run-b",
                    "run_ref": "worker-b",
                },
            ]
            marker_path = cfg["state_dir"] / f"batch-{batch_id}.state.json"
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "batch_id": batch_id,
                        "sprint_id": "7",
                        "provider": "openai",
                        "status": "submitted",
                        "request_file": str(request),
                        "request_sha256": request_sha,
                        "provider_batch_id": "batch_remote",
                        "acceptance_receipt": receipt,
                        "jobs": jobs,
                    }
                )
            )
            state = {
                "schema_version": 2,
                "sprint": {"id": "7"},
                "tickets": {
                    "PROJ-1": {"state": "running", "run_ref": "worker-a"},
                    "PROJ-2": {"state": "running", "run_ref": "worker-b"},
                },
            }
            controller.write_json(controller.state_path(cfg["state_dir"], "7"), state)
            ledger = root / ".orchestration/.llm-usage/usage.jsonl"
            ledger.parent.mkdir()
            ledger.write_text(
                "".join(
                    json.dumps(
                        {
                            "kind": "reservation",
                            "reservation_id": f"resv-{suffix}",
                            "run_id": f"run-{suffix}",
                            "ticket": f"PROJ-{index}",
                            "sprint": "7",
                            "provider": "openai",
                            "model": "gpt-5.6-sol",
                            "projected_cost_usd": "1",
                        }
                    )
                    + "\n"
                    for suffix, index in (("a", 1), ("b", 2))
                )
            )
            results = [
                {
                    "custom_id": "job-a",
                    "outcome": "completed",
                    "response_id": "resp-a",
                    "usage": {
                        "input_tokens": 1,
                        "cache_write_tokens": 0,
                        "cache_read_tokens": 0,
                        "output_tokens": 1,
                        "reasoning_tokens": 0,
                    },
                }
            ]
            evidence = {
                "schema_version": 2,
                "adapter": "openai-batch",
                "authority": "provider-network",
                "approved_origin": "https://api.openai.com",
                "batch_id": batch_id,
                "provider_batch_id": "batch_remote",
                "request_sha256": request_sha,
                "acceptance_receipt": receipt,
                "acceptance_receipt_sha256": receipt["sha256"],
                "status": "cancelled",
                "job_ids": ["job-a", "job-b"],
                "raw_pages": [],
                "results": results,
                "unresolved_job_ids": ["job-b"],
                "results_sha256": hashlib.sha256(
                    json.dumps(results, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            evidence_sha = hashlib.sha256(
                json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            evidence_path = (
                cfg["state_dir"]
                / f"batch-{batch_id}.terminal.sha256-{evidence_sha}.json"
            )
            evidence_path.write_text(json.dumps(evidence))

            def runner(action, marker, config):
                return {"path": str(evidence_path), "sha256": evidence_sha}

            args = argparse.Namespace(
                batch=batch_id,
                outcome="failed",
                provider_evidence=None,
                results=None,
                provider_batch_id=None,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                controller.reconcile_batch(args, cfg, runner)
            events = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertTrue(
                any(
                    x.get("kind") == "usage" and x.get("reservation_id") == "resv-a"
                    for x in events
                )
            )
            self.assertFalse(
                any(
                    x.get("reservation_id") == "resv-b"
                    and x.get("kind") in {"usage", "release"}
                    for x in events
                )
            )
            self.assertEqual(
                json.loads(marker_path.read_text())["status"],
                "completed_with_uncertainty",
            )


if __name__ == "__main__":
    unittest.main()
