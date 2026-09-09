import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "provider_batch_adapter", ROOT / "scripts" / "provider_batch_adapter.py"
)
adapter = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(adapter)


class ProviderBatchAdapterTests(unittest.TestCase):
    def fixture(self, name):
        return json.loads((ROOT / "tests" / "fixtures" / name).read_text())

    def test_normalizes_authentic_anthropic_terminal_rows(self):
        rows = adapter.normalize_results(
            "anthropic",
            self.fixture("anthropic-batch-results.json"),
            {"job-a", "job-b"},
        )
        self.assertEqual(rows[0]["response_id"], "msg_01")
        self.assertEqual(
            rows[0]["usage"],
            {
                "input_tokens": 11,
                "cache_write_tokens": 3,
                "cache_read_tokens": 5,
                "output_tokens": 7,
                "reasoning_tokens": 0,
            },
        )
        self.assertEqual(rows[1]["outcome"], "failed")
        self.assertEqual(rows[1]["error"]["type"], "invalid_request")

    def test_normalizes_authentic_openai_nested_response_and_usage(self):
        rows = adapter.normalize_results(
            "openai",
            self.fixture("openai-batch-output.json")
            + self.fixture("openai-batch-errors.json"),
            {"job-a", "job-b"},
        )
        self.assertEqual(rows[0]["response_id"], "resp_01")
        self.assertEqual(rows[0]["usage"]["cache_read_tokens"], 4)
        self.assertEqual(rows[0]["usage"]["reasoning_tokens"], 2)
        self.assertEqual(rows[1]["outcome"], "failed")

    def test_rejects_duplicate_missing_and_unknown_custom_ids(self):
        valid = self.fixture("anthropic-batch-results.json")
        for rows, pattern in (
            ([valid[0], valid[0]], "duplicate"),
            ([valid[0]], "missing"),
            (
                valid + [{"custom_id": "job-x", "result": {"type": "canceled"}}],
                "unknown",
            ),
        ):
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(ValueError, pattern):
                    adapter.normalize_results("anthropic", rows, {"job-a", "job-b"})

    def test_terminal_bundle_is_content_addressed_and_binds_request_and_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = root / "request.json"
            request.write_text('{"requests":[]}\n')
            acceptance = {"id": "msgbatch_01", "type": "message_batch"}
            acceptance_digest = adapter.canonical_sha256(acceptance)
            acceptance_path = root / f"sha256-{acceptance_digest}.json"
            acceptance_path.write_text(json.dumps(acceptance))
            receipt = {
                "provider": "anthropic",
                "provider_batch_id": "msgbatch_01",
                "request_sha256": hashlib.sha256(request.read_bytes()).hexdigest(),
                "raw": {"path": str(acceptance_path), "sha256": acceptance_digest},
            }
            receipt["sha256"] = adapter.canonical_sha256(receipt)
            marker = {
                "schema_version": 2,
                "batch_id": "local",
                "provider": "anthropic",
                "request_file": str(request),
                "request_sha256": hashlib.sha256(request.read_bytes()).hexdigest(),
                "provider_batch_id": "msgbatch_01",
                "acceptance_receipt": receipt,
                "jobs": [{"custom_id": "job-a"}, {"custom_id": "job-b"}],
            }
            transport = {
                "status": self.fixture("anthropic-batch-ended.json"),
                "result_pages": [self.fixture("anthropic-batch-results.json")],
            }
            bundle = adapter.acquire_terminal_bundle(marker, transport, root)
            self.assertEqual(bundle["request_sha256"], marker["request_sha256"])
            self.assertEqual(bundle["acceptance_receipt_sha256"], receipt["sha256"])
            self.assertEqual(bundle["acceptance_receipt"], receipt)
            self.assertEqual(bundle["provider_batch_id"], "msgbatch_01")
            self.assertEqual(len(bundle["raw_pages"]), 2)
            self.assertEqual(
                bundle["results_sha256"], adapter.canonical_sha256(bundle["results"])
            )

    def test_modified_request_after_prepare_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            request = Path(temp) / "request.json"
            request.write_text("original")
            marker = {
                "request_file": str(request),
                "request_sha256": hashlib.sha256(b"original").hexdigest(),
            }
            request.write_text("changed")
            with self.assertRaisesRegex(ValueError, "request digest"):
                adapter.verify_request(marker)

    def test_cross_origin_redirect_is_rejected_without_second_request(self):
        request = adapter.authenticated_request(
            "anthropic", "https://api.anthropic.com/v1", "messages/batches", "secret"
        )
        handler = adapter.NoCredentialRedirect("https://api.anthropic.com")
        with self.assertRaisesRegex(ValueError, "redirect"):
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://evil.example/steal"
            )

    def test_ambiguous_submission_is_fenced_and_not_retried(self):
        marker = {"status": "submitting", "provider_batch_id": ""}
        with self.assertRaisesRegex(ValueError, "uncertain"):
            adapter.assert_submission_retry_safe(marker)


if __name__ == "__main__":
    unittest.main()
