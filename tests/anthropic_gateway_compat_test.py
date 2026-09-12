"""Regressions for native context editing and misleading stopped-gateway 402s."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from api_agent import HttpTransport, AgentError, ProviderHTTPError
from native_gateway import NativeGateway


class CompatibilityTests(unittest.TestCase):
    def test_context_editing_header_is_derived_without_dropping_body(self):
        payload = dict(
            model="test",
            max_tokens=10,
            messages=[],
            context_management={
                "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]
            },
        )
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"{}"
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-only"}),
            patch("api_agent.urllib.request.urlopen", return_value=response) as send,
        ):
            HttpTransport().request("anthropic", "/messages", payload)
            request = send.call_args.args[0]
            self.assertEqual(send.call_args.kwargs["timeout"], 900)
            self.assertEqual(
                request.get_header("Anthropic-beta"), "context-management-2025-06-27"
            )
            self.assertEqual(json.loads(request.data), payload)
            HttpTransport().request("anthropic", "/messages", {"model": "test"})
            self.assertIsNone(send.call_args.args[0].get_header("Anthropic-beta"))

    def test_unmetered_compaction_is_rejected_before_network(self):
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-only"}),
            patch("api_agent.urllib.request.urlopen") as send,
        ):
            with self.assertRaisesRegex(AgentError, "compaction"):
                HttpTransport().request(
                    "anthropic",
                    "/messages",
                    {"context_management": {"edits": [{"type": "compact_20260112"}]}},
                )
            send.assert_not_called()

    def test_provider_400_remains_400_and_blocks_new_tickets(self):
        with tempfile.TemporaryDirectory() as temp:
            transport = Mock()

            def request(provider, path, payload, **kwargs):
                if path.endswith("count_tokens"):
                    return {"input_tokens": 1}
                raise ProviderHTTPError(
                    400, "context_management: Extra inputs are not permitted"
                )

            transport.request.side_effect = request
            config = {
                "llm": {
                    "pricing": {
                        "test": dict(
                            input_per_mtok=1,
                            output_per_mtok=1,
                            cache_read_per_mtok=1,
                            cache_write_per_mtok=1,
                        )
                    }
                }
            }
            gateway = NativeGateway(Path(temp), config, "T-1", "1", "test", transport)
            endpoint = gateway.start()
            try:
                for _ in range(2):
                    req = urllib.request.Request(
                        endpoint + "/v1/messages",
                        data=json.dumps(
                            dict(model="test", messages=[], max_tokens=10)
                        ).encode(),
                        headers={"x-api-key": gateway.token},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as result:
                        urllib.request.urlopen(req, timeout=3)
                    self.assertEqual(result.exception.code, 400)
                    self.assertEqual(
                        json.loads(result.exception.read())["error"]["type"],
                        "api_error",
                    )
                    result.exception.close()
                self.assertEqual(
                    transport.request.call_count, 2
                )  # One count, one rejected generation.
                self.assertEqual(
                    gateway.health.status("anthropic")["state"], "incompatible"
                )
                other = NativeGateway(
                    Path(temp), config, "T-2", "1", "other", transport
                )
                with self.assertRaisesRegex(AgentError, "admission held"):
                    other.request(
                        "/v1/messages", dict(model="test", messages=[], max_tokens=10)
                    )
                self.assertEqual(transport.request.call_count, 2)
                events = gateway.ledger.snapshot()
                self.assertTrue(any(e["kind"] == "release" for e in events))
                self.assertFalse(any(e["kind"] == "usage" for e in events))
            finally:
                gateway.close()

    def test_first_stop_cause_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            gateway = NativeGateway(Path(temp), {}, "T-1", "1", "test", Mock())
            gateway.stop("first", AgentError("first"))
            gateway.stop("second", ProviderHTTPError(401, "second"))
            with self.assertRaisesRegex(AgentError, "^first$"):
                gateway.raise_if_stopped()
            self.assertEqual(gateway.reason, "first")


if __name__ == "__main__":
    unittest.main()
