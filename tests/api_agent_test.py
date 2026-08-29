#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("api_agent", ROOT / "scripts" / "api_agent.py")
assert SPEC and SPEC.loader
api_agent = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api_agent
SPEC.loader.exec_module(api_agent)

CLEAN_REVIEW = json.dumps(
    {
        "schema_version": 1,
        "gate": "code-review",
        "verdict": "PASS",
        "checks": [{"name": "diff", "status": "pass"}],
        "findings": [],
    },
    separators=(",", ":"),
)


class FakeTransport:
    def __init__(self, responses, count=100):
        self.responses = list(responses)
        self.count = count
        self.calls = []

    def request(self, provider, path, payload, idempotency_key=None):
        self.calls.append((provider, path, payload, idempotency_key))
        if path.endswith("count_tokens") or path.endswith("input_tokens"):
            return {"input_tokens": self.count}
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ApiAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".orchestration").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def config(self, provider="anthropic", model="test-model", extra=""):
        path = self.root / ".orchestration" / "config.yaml"
        roles = extra or "    code-reviewer:\n      allowed_tools: [read_file, search, git_diff, git_status, run_check]"
        path.write_text(
            f"""schema_version: 1
llm:
  execution: api
  provider: {provider}
  model: {model}
  fallback: none
  budgets:
    max_usd_per_run: 1.00
    max_usd_per_ticket: 2.00
    max_usd_per_sprint: 5.00
    max_output_tokens_per_turn: 100
    max_tool_rounds: 3
    max_tool_output_chars: 2000
    tool_timeout_seconds: 10
    max_pre_ack_retries: 2
    retry_backoff_seconds: 0
  pricing:
    {model}:
      input_per_mtok: 1
      cache_write_per_mtok: 2
      cache_read_per_mtok: 0.1
      output_per_mtok: 10
  roles:
{roles}
self_check:
  - name: smoke
    run: git status --short
""",
            encoding="utf-8",
        )
        return path

    def agent(self, transport, provider="anthropic", role="code-reviewer", run_id="test-run"):
        return api_agent.ApiAgent(
            root=self.root,
            config_path=self.config(provider=provider),
            role=role,
            ticket="PROJ-1",
            sprint="SPRINT-1",
            run_id=run_id,
            transport=transport,
        )

    def test_repository_env_loads_provider_credentials_without_overriding_container(self):
        config = self.config()
        (config.parent / ".env").write_text(
            "# local orchestration secrets\n"
            "export ANTHROPIC_API_KEY='repo-key'\n"
            "ANTHROPIC_BASE_URL=https://proxy.example/v1 # optional proxy\n"
            "OPENAI_API_KEY=repo-openai\n"
            "AZURE_ADM_API_KEY=repo-azure\n"
            "AZURE_ADM_BASE_URL=https://resource.openai.azure.com/openai/v1\n"
            "PATH=/untrusted/path\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "container-key"}, clear=False):
            os.environ.pop("ANTHROPIC_BASE_URL", None)
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("AZURE_ADM_API_KEY", None)
            os.environ.pop("AZURE_ADM_BASE_URL", None)
            loaded = api_agent.load_orchestration_env(config)
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "container-key")
            self.assertEqual(os.environ["ANTHROPIC_BASE_URL"], "https://proxy.example/v1")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "repo-openai")
            self.assertEqual(os.environ["AZURE_ADM_API_KEY"], "repo-azure")
            self.assertEqual(
                os.environ["AZURE_ADM_BASE_URL"],
                "https://resource.openai.azure.com/openai/v1",
            )
            self.assertNotEqual(os.environ.get("PATH"), "/untrusted/path")
            self.assertEqual(
                loaded,
                [
                    "ANTHROPIC_BASE_URL",
                    "OPENAI_API_KEY",
                    "AZURE_ADM_API_KEY",
                    "AZURE_ADM_BASE_URL",
                ],
            )

    def test_repository_env_rejects_shell_syntax(self):
        config = self.config()
        (config.parent / ".env").write_text("source ../secrets\n", encoding="utf-8")
        with self.assertRaisesRegex(api_agent.AgentError, "expected KEY=value"):
            api_agent.load_orchestration_env(config)

    def test_anthropic_tool_loop_and_usage(self):
        transport = FakeTransport(
            [
                {
                    "id": "msg_1",
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                    "content": [{"type": "tool_use", "id": "tool_1", "name": "git_status", "input": {}}],
                },
                {
                    "id": "msg_2",
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 20,
                        "cache_read_input_tokens": 80,
                        "output_tokens": 5,
                    },
                    "content": [{"type": "text", "text": CLEAN_REVIEW}],
                },
            ]
        )
        agent = self.agent(transport)
        result = agent.run(
            {"model": "test-model", "max_tokens": 500, "system": [], "messages": [{"role": "user", "content": "review"}]}
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["review"]["verdict"], "PASS")
        message_calls = [call for call in transport.calls if call[1] == "messages"]
        self.assertEqual(len(message_calls), 2)
        self.assertEqual(message_calls[0][2]["max_tokens"], 100)
        tool_names = {tool["name"] for tool in message_calls[0][2]["tools"]}
        self.assertNotIn("apply_patch", tool_names)
        self.assertEqual(message_calls[1][2]["messages"][-1]["content"][0]["type"], "tool_result")
        summary = agent.ledger.summary()
        self.assertEqual(summary["input_tokens"], 120)
        self.assertEqual(summary["cache_read_tokens"], 80)
        self.assertEqual(summary["output_tokens"], 15)
        self.assertEqual(summary["open_reservations"], [])

    def test_openai_tool_loop_uses_previous_response_id(self):
        transport = FakeTransport(
            [
                {
                    "id": "resp_1",
                    "status": "completed",
                    "usage": {"input_tokens": 100, "output_tokens": 8},
                    "output": [
                        {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "git_status",
                            "arguments": "{}",
                        }
                    ],
                },
                {
                    "id": "resp_2",
                    "status": "completed",
                    "usage": {
                        "input_tokens": 90,
                        "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 10},
                        "output_tokens": 4,
                    },
                    "output_text": "done",
                    "output": [],
                },
            ]
        )
        config = self.config(
            provider="openai",
            extra="    implementer:\n      allowed_tools: [read_file, search, git_diff, git_status, run_check, apply_patch]",
        )
        agent = api_agent.ApiAgent(
            root=self.root,
            config_path=config,
            role="implementer",
            ticket="PROJ-2",
            sprint=None,
            run_id="openai-run",
            transport=transport,
        )
        result = agent.run(
            {
                "model": "test-model", "max_output_tokens": 100,
                "input": [{"role": "user", "content": "work"}],
                "text": {"verbosity": "low"},
            }
        )
        response_calls = [call for call in transport.calls if call[1] == "responses"]
        self.assertEqual(result["output_text"], "done")
        self.assertEqual(response_calls[1][2]["previous_response_id"], "resp_1")
        self.assertEqual(response_calls[1][2]["input"][0]["type"], "function_call_output")
        self.assertEqual(response_calls[1][2]["text"], {"verbosity": "low"})
        self.assertIn("apply_patch", {tool["name"] for tool in response_calls[0][2]["tools"]})

    def test_azure_adm_chat_completion_tool_loop(self):
        transport = FakeTransport(
            [
                {
                    "id": "chat_1",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "git_status", "arguments": "{}"},
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 80, "completion_tokens": 8},
                },
                {
                    "id": "chat_2",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "done"},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 70,
                        "prompt_tokens_details": {"cached_tokens": 40},
                        "completion_tokens": 4,
                        "completion_tokens_details": {"reasoning_tokens": 2},
                    },
                },
            ]
        )
        config = self.config(
            provider="azure_adm",
            extra="    implementer:\n      allowed_tools: [read_file, search, git_diff, git_status, run_check, apply_patch]",
        )
        agent = api_agent.ApiAgent(
            root=self.root,
            config_path=config,
            role="implementer",
            ticket="PROJ-3",
            sprint=None,
            run_id="azure-adm-run",
            transport=transport,
        )
        result = agent.run(
            {
                "model": "test-model",
                "max_completion_tokens": 100,
                "messages": [{"role": "user", "content": "work"}],
            }
        )
        chat_calls = [call for call in transport.calls if call[1] == "chat/completions"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output_text"], "done")
        self.assertEqual(len(chat_calls), 2)
        self.assertFalse(any(call[1].endswith("input_tokens") for call in transport.calls))
        self.assertIn(
            "apply_patch",
            {tool["function"]["name"] for tool in chat_calls[0][2]["tools"]},
        )
        self.assertEqual(chat_calls[1][2]["messages"][-1]["role"], "tool")
        self.assertEqual(chat_calls[1][2]["messages"][-1]["tool_call_id"], "call_1")
        summary = agent.ledger.summary()
        self.assertEqual(summary["input_tokens"], 110)
        self.assertEqual(summary["cache_read_tokens"], 40)
        self.assertEqual(summary["output_tokens"], 12)

    def test_budget_blocks_before_provider_submission(self):
        transport = FakeTransport([], count=2_000_000)
        agent = self.agent(transport, run_id="budget-run")
        with self.assertRaises(api_agent.BudgetError):
            agent.run(
                {"model": "test-model", "max_tokens": 100, "system": [], "messages": [{"role": "user", "content": "review"}]}
            )
        self.assertFalse(any(call[1] == "messages" for call in transport.calls))
        self.assertEqual(agent.state["status"], "budget_blocked")

    def test_explicit_rate_limit_retries_with_same_reservation(self):
        transport = FakeTransport(
            [
                api_agent.ProviderHTTPError(429, "rate limited"),
                {
                    "id": "msg_after_retry",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 2},
                    "content": [{"type": "text", "text": CLEAN_REVIEW}],
                },
            ]
        )
        agent = self.agent(transport, run_id="retry-run")
        with mock.patch.object(api_agent.time, "sleep") as sleep:
            result = agent.run(
                {"model": "test-model", "max_tokens": 100, "system": [], "messages": [{"role": "user", "content": "review"}]}
            )
        message_calls = [call for call in transport.calls if call[1] == "messages"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(message_calls), 2)
        self.assertEqual(message_calls[0][3], message_calls[1][3])
        self.assertEqual(agent.state["retry_count"], 1)
        sleep.assert_called_once_with(0)

    def test_rate_limit_honors_provider_retry_after(self):
        transport = FakeTransport(
            [
                api_agent.ProviderHTTPError(429, "rate limited", retry_after_seconds=17.5),
                {
                    "id": "msg_after_retry_after",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 2},
                    "content": [{"type": "text", "text": CLEAN_REVIEW}],
                },
            ]
        )
        agent = self.agent(transport, run_id="retry-after-run")
        with mock.patch.object(api_agent.time, "sleep") as sleep:
            result = agent.run(
                {"model": "test-model", "max_tokens": 100, "system": [], "messages": [{"role": "user", "content": "review"}]}
            )
        self.assertEqual(result["status"], "completed")
        sleep.assert_called_once_with(17.5)
        self.assertEqual(agent.state["total_rate_limit_wait_seconds"], 17.5)

    def test_reviewer_output_fails_closed_when_not_structured(self):
        transport = FakeTransport(
            [{
                "id": "msg_invalid", "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 2},
                "content": [{"type": "text", "text": "VERDICT: PASS"}],
            }]
        )
        agent = self.agent(transport, run_id="invalid-review")
        with self.assertRaisesRegex(api_agent.AgentError, "invalid structured output"):
            agent.run({"model": "test-model", "max_tokens": 100, "system": [], "messages": [{"role": "user", "content": "review"}]})
        self.assertEqual(agent.state["status"], "invalid_output")

    def test_ambiguous_submission_keeps_reservation_for_reconciliation(self):
        transport = FakeTransport([api_agent.ProviderAmbiguous("timeout")])
        agent = self.agent(transport, run_id="ambiguous-run")
        with self.assertRaises(api_agent.ProviderAmbiguous):
            agent.run(
                {"model": "test-model", "max_tokens": 100, "system": [], "messages": [{"role": "user", "content": "review"}]}
            )
        self.assertEqual(agent.state["status"], "needs_reconcile")
        self.assertEqual(len(agent.ledger.summary()["open_reservations"]), 1)

        args = type(
            "Args",
            (),
            {
                "run_id": "ambiguous-run",
                "outcome": "not-found",
                "evidence": "provider dashboard search at 2026-08-26T12:00Z",
                "response_id": None,
                "input_tokens": 0,
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
                "output_tokens": 0,
                "config": str(self.root / ".orchestration" / "config.yaml"),
            },
        )()
        reconciled = api_agent.reconcile_run(args, self.root)
        self.assertEqual(reconciled["status"], "reconciled_not_found")
        self.assertEqual(reconciled["usage"]["open_reservations"], [])

    def test_reviewer_cannot_add_write_tool(self):
        config = self.config(extra="    code-reviewer:\n      allowed_tools: [read_file, apply_patch]")
        with self.assertRaisesRegex(api_agent.AgentError, "may not receive"):
            api_agent.ApiAgent(
                root=self.root,
                config_path=config,
                role="code-reviewer",
                ticket=None,
                sprint=None,
                run_id="forbidden-tool",
                transport=FakeTransport([]),
            )

    def test_tool_paths_cannot_escape_repository(self):
        executor = api_agent.ToolExecutor(self.root, {}, 1000, 5)
        with self.assertRaisesRegex(api_agent.AgentError, "escapes repository"):
            executor.execute("read_file", {"path": "../secret"})

    def test_missing_price_fails_closed(self):
        config = self.config()
        text = config.read_text(encoding="utf-8").replace("    test-model:\n", "    another-model:\n")
        config.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(api_agent.AgentError, "pricing.test-model"):
            api_agent.ApiAgent(
                root=self.root,
                config_path=config,
                role="code-reviewer",
                ticket=None,
                sprint=None,
                run_id="missing-price",
                transport=FakeTransport([]),
            )

    def test_response_without_usage_remains_reserved(self):
        transport = FakeTransport(
            [{"id": "msg_no_usage", "stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}]
        )
        agent = self.agent(transport, run_id="missing-usage")
        with self.assertRaises(api_agent.ProviderAmbiguous):
            agent.run(
                {"model": "test-model", "max_tokens": 100, "system": [], "messages": [{"role": "user", "content": "review"}]}
            )
        self.assertEqual(agent.state["status"], "needs_reconcile")
        self.assertEqual(len(agent.ledger.summary()["open_reservations"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
