import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from urllib.request import HTTPSHandler


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
        self.assertEqual(rows[1]["outcome"], "ambiguous")
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
        self.assertEqual(rows[1]["outcome"], "ambiguous")

    def test_releases_only_provider_documented_pre_execution_results(self):
        anthropic_rows = []
        for index, result_type in enumerate(("canceled", "expired", "errored")):
            anthropic_rows.append(
                {
                    "custom_id": f"anthropic-{index}",
                    "result": {"type": result_type, "error": {"type": "api_error"}},
                }
            )
        normalized = adapter.normalize_results(
            "anthropic",
            anthropic_rows,
            {row["custom_id"] for row in anthropic_rows},
        )
        by_id = {row["custom_id"]: row for row in normalized}
        for custom_id in ("anthropic-0", "anthropic-1"):
            self.assertEqual(by_id[custom_id]["outcome"], "failed")
            self.assertIs(by_id[custom_id]["provider_proven_nonexecuted"], True)
        self.assertEqual(by_id["anthropic-2"]["outcome"], "ambiguous")
        self.assertNotIn("provider_proven_nonexecuted", by_id["anthropic-2"])

        openai_rows = []
        for index, code in enumerate(
            ("batch_cancelled", "batch_expired", "request_timeout", "api_error")
        ):
            openai_rows.append(
                {
                    "id": f"batch_req_{index}",
                    "custom_id": f"openai-{index}",
                    "response": None,
                    "error": {"code": code, "message": code},
                }
            )
        normalized = adapter.normalize_results(
            "openai", openai_rows, {row["custom_id"] for row in openai_rows}
        )
        by_id = {row["custom_id"]: row for row in normalized}
        for custom_id in ("openai-0", "openai-1"):
            self.assertEqual(by_id[custom_id]["outcome"], "failed")
            self.assertIs(by_id[custom_id]["provider_proven_nonexecuted"], True)
        for custom_id in ("openai-2", "openai-3"):
            self.assertEqual(by_id[custom_id]["outcome"], "ambiguous")
            self.assertNotIn("provider_proven_nonexecuted", by_id[custom_id])

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
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cert = root / "cert.pem"
            key = root / "key.pem"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                ],
                check=True,
                capture_output=True,
            )
            stolen = []
            source_seen = []

            class Sink(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    stolen.append(self.headers.get("Authorization"))
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")

                def log_message(self, *args):
                    pass

            sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(str(cert), str(key))
            sink.socket = server_context.wrap_socket(sink.socket, server_side=True)

            class Redirect(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    source_seen.append(self.headers.get("Authorization"))
                    self.send_response(302)
                    self.send_header(
                        "Location", f"https://127.0.0.1:{sink.server_port}/steal"
                    )
                    self.end_headers()

                def log_message(self, *args):
                    pass

            source = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
            source.socket = server_context.wrap_socket(source.socket, server_side=True)
            threads = [
                threading.Thread(target=x.serve_forever, daemon=True)
                for x in (sink, source)
            ]
            for thread in threads:
                thread.start()
            context = ssl._create_unverified_context()
            opener = adapter.build_opener(
                adapter.NoCredentialRedirect(f"https://127.0.0.1:{source.server_port}"),
                HTTPSHandler(context=context),
            )
            transport = adapter.NetworkTransport(
                "openai",
                base=f"https://127.0.0.1:{source.server_port}/v1",
                key="secret",
                opener=opener,
            )
            try:
                with self.assertRaisesRegex(ValueError, "redirect"):
                    transport.request("batches/test")
                self.assertEqual(source_seen, ["Bearer secret"])
                self.assertEqual(stolen, [])
            finally:
                source.shutdown()
                sink.shutdown()
                source.server_close()
                sink.server_close()

    def test_caller_base_url_is_ignored_without_operator_policy(self):
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "secret", "OPENAI_BASE_URL": "https://evil.example/v1"},
            clear=False,
        ):
            transport = adapter.NetworkTransport("openai")
        self.assertEqual(transport.base, "https://api.openai.com/v1")

    def test_operator_policy_maps_credential_name_to_gateway(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}, clear=False),
        ):
            policy = Path(temp) / "provider-origins.json"
            policy.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "credentials": {"OPENAI_API_KEY": "https://gateway.example/v1"},
                    }
                )
            )
            transport = adapter.NetworkTransport("openai", policy_path=policy)
        self.assertEqual(transport.base, "https://gateway.example/v1")
        self.assertNotIn("secret", json.dumps(transport.policy_receipt))

    def test_strict_provider_envelopes_bind_openai_batch(self):
        rows = self.fixture("openai-batch-output.json")
        for mutation, pattern in (
            (lambda row: row.update(id="wrong"), "object"),
            (lambda row: row.update(error={"code": "also_error"}), "exclusive"),
            (
                lambda row: row["response"]["body"].update(object="chat.completion"),
                "response object",
            ),
            (
                lambda row: row["response"]["body"]["usage"].update(input_tokens="13"),
                "invalid input_tokens",
            ),
        ):
            changed = json.loads(json.dumps(rows))
            mutation(changed[0])
            with (
                self.subTest(pattern=pattern),
                self.assertRaisesRegex(ValueError, pattern),
            ):
                adapter.normalize_results(
                    "openai", changed, {"job-a"}, require_complete=True
                )

    def test_terminal_partial_rows_preserve_missing_as_ambiguous(self):
        rows = adapter.normalize_results(
            "openai",
            self.fixture("openai-batch-output.json"),
            {"job-a", "job-b"},
            require_complete=False,
        )
        self.assertEqual([row["custom_id"] for row in rows], ["job-a"])

    def test_cancelled_bundle_keeps_successes_and_marks_only_missing_ambiguous(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = root / "request.json"
            request.write_text('{"requests":[]}\n')
            acceptance = {"id": "msgbatch_01", "type": "message_batch"}
            digest = adapter.canonical_sha256(acceptance)
            raw = root / f"sha256-{digest}.json"
            raw.write_text(json.dumps(acceptance))
            receipt = {
                "provider": "anthropic",
                "provider_batch_id": "msgbatch_01",
                "request_sha256": hashlib.sha256(request.read_bytes()).hexdigest(),
                "raw": {"path": str(raw), "sha256": digest},
            }
            receipt["sha256"] = adapter.canonical_sha256(receipt)
            marker = {
                "schema_version": 2,
                "batch_id": "local",
                "provider": "anthropic",
                "request_file": str(request),
                "request_sha256": receipt["request_sha256"],
                "provider_batch_id": "msgbatch_01",
                "acceptance_receipt": receipt,
                "jobs": [{"custom_id": "job-a"}, {"custom_id": "job-b"}],
            }
            status = self.fixture("anthropic-batch-ended.json")
            bundle = adapter.acquire_terminal_bundle(
                marker,
                {
                    "status": status,
                    "result_pages": [[self.fixture("anthropic-batch-results.json")[0]]],
                },
                root,
            )
            self.assertEqual(bundle["status"], "ended")
            self.assertEqual([x["custom_id"] for x in bundle["results"]], ["job-a"])
            self.assertEqual(bundle["unresolved_job_ids"], ["job-b"])

    def test_openai_cancelled_batch_acquires_all_available_terminal_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = root / "request.jsonl"
            request.write_text("{}\n")
            acceptance = {
                "id": "batch_01",
                "object": "batch",
                "input_file_id": "file_in",
                "endpoint": "/v1/responses",
            }
            digest = adapter.canonical_sha256(acceptance)
            raw = root / f"sha256-{digest}.json"
            raw.write_text(json.dumps(acceptance))
            receipt = {
                "provider": "openai",
                "provider_batch_id": "batch_01",
                "request_sha256": hashlib.sha256(request.read_bytes()).hexdigest(),
                "raw": {"path": str(raw), "sha256": digest},
            }
            receipt["sha256"] = adapter.canonical_sha256(receipt)
            marker = {
                "schema_version": 2,
                "batch_id": "local",
                "provider": "openai",
                "request_file": str(request),
                "request_sha256": receipt["request_sha256"],
                "input_file_id": "file_in",
                "provider_batch_id": "batch_01",
                "acceptance_receipt": receipt,
                "jobs": [
                    {"custom_id": "job-a"},
                    {"custom_id": "job-b"},
                    {"custom_id": "job-c"},
                ],
            }
            status = {
                **acceptance,
                "status": "cancelled",
                "output_file_id": "file_out",
                "error_file_id": "file_error",
            }
            bundle = adapter.acquire_terminal_bundle(
                marker,
                {
                    "status": status,
                    "result_pages": [
                        self.fixture("openai-batch-output.json"),
                        self.fixture("openai-batch-errors.json"),
                    ],
                },
                root,
            )
            self.assertEqual(
                [row["custom_id"] for row in bundle["results"]], ["job-a", "job-b"]
            )
            self.assertEqual(bundle["unresolved_job_ids"], ["job-c"])

    def test_openai_network_submission_carries_stable_idempotency_key(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        class Opener:
            request = None

            def open(self, request, timeout):
                self.request = request
                return Response()

        opener = Opener()
        transport = adapter.NetworkTransport(
            "openai", base="https://api.openai.com/v1", key="secret", opener=opener
        )
        transport.request(
            "batches", payload={}, method="POST", idempotency_key="batch-create-digest"
        )
        self.assertEqual(
            opener.request.get_header("Idempotency-key"), "batch-create-digest"
        )

    def test_submission_lock_prevents_duplicate_provider_creation(self):
        class CountingTransport(dict):
            def __init__(self):
                super().__init__(
                    submit={
                        "id": "msgbatch_01",
                        "type": "message_batch",
                        "processing_status": "in_progress",
                    }
                )
                self.reads = 0

            def __contains__(self, key):
                if key == "submit":
                    self.reads += 1
                return super().__contains__(key)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = root / "request.json"
            request.write_text('{"requests":[]}\n')
            marker_path = root / "marker.json"
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "batch_id": "local",
                        "provider": "anthropic",
                        "status": "pending_submission",
                        "request_file": str(request),
                        "request_sha256": hashlib.sha256(
                            request.read_bytes()
                        ).hexdigest(),
                        "provider_batch_id": "",
                        "jobs": [],
                    }
                )
            )
            transport = CountingTransport()
            receipts = []
            threads = [
                threading.Thread(
                    target=lambda: receipts.append(
                        adapter.submit(marker_path, transport)
                    )
                )
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(receipts), 2)
            self.assertEqual(receipts[0], receipts[1])
            self.assertEqual(transport.reads, 1)

    def test_ambiguous_submission_is_fenced_and_not_retried(self):
        marker = {"status": "submitting", "provider_batch_id": ""}
        with self.assertRaisesRegex(ValueError, "uncertain"):
            adapter.assert_submission_retry_safe(marker)


if __name__ == "__main__":
    unittest.main()
