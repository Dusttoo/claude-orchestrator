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


class FakeBedrockClient:
    def __init__(self, *, response=None, error=None):
        self.response = response or {}
        self.error = error
        self.calls = []

    def count_tokens(self, **payload):
        self.calls.append(("count_tokens", payload))
        if self.error:
            raise self.error
        return self.response

    def converse(self, **payload):
        self.calls.append(("converse", payload))
        if self.error:
            raise self.error
        return self.response


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

    @unittest.skipUnless(importlib.util.find_spec("botocore"), "optional Bedrock SDK absent")
    def test_bedrock_transport_uses_request_metadata_and_aws_request_id(self):
        client = FakeBedrockClient(
            response={
                "ResponseMetadata": {"RequestId": "aws-request-123"},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            }
        )
        transport = api_agent.HttpTransport(bedrock_client=client)
        response = transport.request(
            "bedrock",
            "converse",
            {
                "modelId": "global.anthropic.claude-sonnet-5",
                "messages": [{"role": "user", "content": [{"text": "hello"}]}],
                "inferenceConfig": {"maxTokens": 100},
            },
            idempotency_key="resv_123",
        )
        self.assertEqual(response["id"], "aws-request-123")
        self.assertEqual(
            client.calls[0][1]["requestMetadata"]["orchestrationReservation"],
            "resv_123",
        )

    @unittest.skipUnless(importlib.util.find_spec("botocore"), "optional Bedrock SDK absent")
    def test_bedrock_transport_retries_only_explicit_rejections(self):
        from botocore.exceptions import ClientError

        throttled = ClientError(
            {
                "Error": {"Code": "ThrottlingException", "Message": "slow down"},
                "ResponseMetadata": {"HTTPStatusCode": 429},
            },
            "Converse",
        )
        with self.assertRaisesRegex(api_agent.ProviderHTTPError, "HTTP 429"):
            api_agent.HttpTransport(bedrock_client=FakeBedrockClient(error=throttled)).request(
                "bedrock", "converse", {}
            )
        uncertain = ClientError(
            {
                "Error": {"Code": "InternalServerException", "Message": "unknown"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            },
            "Converse",
        )
        with self.assertRaises(api_agent.ProviderAmbiguous):
            api_agent.HttpTransport(bedrock_client=FakeBedrockClient(error=uncertain)).request(
                "bedrock", "converse", {}
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

    def test_rolling_cache_breakpoint_moves_to_the_conversation_end(self):
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
                    "usage": {"input_tokens": 20, "output_tokens": 5},
                    "content": [{"type": "text", "text": CLEAN_REVIEW}],
                },
            ]
        )
        agent = self.agent(transport)
        agent.run(
            {"model": "test-model", "max_tokens": 500, "system": [], "messages": [{"role": "user", "content": "review"}]}
        )
        resubmitted = [call for call in transport.calls if call[1] == "messages"][1][2]["messages"]
        marked = [
            (index, block)
            for index, message in enumerate(resubmitted)
            for block in message["content"]
            if isinstance(block, dict) and "cache_control" in block
        ]
        # Exactly one breakpoint, on the newest block: system and tools already
        # spend two of Anthropic's four, and adding one per round would exceed it.
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0][0], len(resubmitted) - 1)
        self.assertEqual(resubmitted[-1]["content"][-1]["cache_control"], {"type": "ephemeral"})

    def test_rolling_cache_breakpoint_does_not_mutate_provider_content(self):
        assistant_content = [{"type": "text", "text": "prior"}]
        messages = [
            {"role": "user", "content": "review"},
            {"role": "assistant", "content": assistant_content},
        ]
        rolled = api_agent.roll_conversation_cache_breakpoint(messages)
        self.assertEqual(rolled[-1]["content"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", assistant_content[-1])

    def _commit_initial(self):
        git = [
            "git", "-C", str(self.root),
            "-c", "user.email=test@example.com",
            "-c", "user.name=test",
        ]
        (self.root / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(git + ["add", "README.md"], check=True, capture_output=True)
        subprocess.run(git + ["commit", "-qm", "seed"], check=True, capture_output=True)
        return git

    @staticmethod
    def _completed_transport():
        return FakeTransport(
            [
                {
                    "id": "msg_done",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 40, "output_tokens": 5},
                    "content": [{"type": "text", "text": CLEAN_REVIEW}],
                }
            ]
        )

    def test_worktree_lanes_share_one_usage_ledger(self):
        git = self._commit_initial()
        worktree = self.root / ".claude" / "worktrees" / "agent-1"
        subprocess.run(
            git + ["worktree", "add", "-q", str(worktree), "-b", "lane-1"],
            check=True,
            capture_output=True,
        )
        config = self.config()
        body = {
            "model": "test-model",
            "max_tokens": 500,
            "system": [],
            "messages": [{"role": "user", "content": "review"}],
        }

        main_agent = self.agent(self._completed_transport(), run_id="main-run")
        main_agent.run(dict(body))

        lane = api_agent.ApiAgent(
            root=worktree,
            config_path=config,
            role="code-reviewer",
            ticket="PROJ-1",
            sprint="SPRINT-1",
            run_id="lane-run",
            transport=self._completed_transport(),
        )
        lane.run(dict(body))

        shared = (self.root / ".orchestration" / ".llm-usage").resolve()
        self.assertEqual(lane.ledger.directory, shared)
        self.assertFalse((worktree / ".orchestration" / ".llm-usage").exists())
        # Both lanes counted against one ceiling instead of one ledger each.
        self.assertEqual(lane.ledger.summary()["input_tokens"], 80)
        self.assertEqual(lane.ledger.summary()["output_tokens"], 10)
        # Tool sandboxing still resolves to the lane's own checkout.
        self.assertEqual(lane.tool_executor.root, worktree.resolve())

    def test_usage_root_override_redirects_the_ledger(self):
        override = Path(self.temp.name) / "elsewhere"
        override.mkdir()
        with mock.patch.dict(os.environ, {"ORCHESTRATION_USAGE_ROOT": str(override)}):
            self.assertEqual(api_agent.shared_repository_root(self.root), override.resolve())

    def test_shared_root_falls_back_outside_a_repository(self):
        plain = Path(self.temp.name) / "plain"
        plain.mkdir()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORCHESTRATION_USAGE_ROOT", None)
            with mock.patch.object(
                api_agent.subprocess, "run", side_effect=OSError("git missing")
            ):
                self.assertEqual(api_agent.shared_repository_root(plain), plain.resolve())

    def test_usage_events_record_role_and_request_latency(self):
        transport = FakeTransport(
            [
                {
                    "id": "msg_1",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 30, "cache_read_input_tokens": 70, "output_tokens": 5},
                    "content": [{"type": "text", "text": CLEAN_REVIEW}],
                }
            ]
        )
        agent = self.agent(transport)
        agent.run(
            {"model": "test-model", "max_tokens": 500, "system": [], "messages": [{"role": "user", "content": "review"}]}
        )
        events = agent.ledger._events()
        usage = [event for event in events if event["kind"] == "usage"]
        reservation = [event for event in events if event["kind"] == "reservation"]
        self.assertEqual(usage[0]["role"], "code-reviewer")
        self.assertEqual(reservation[0]["role"], "code-reviewer")
        self.assertIsInstance(usage[0]["latency_ms"], int)
        self.assertGreaterEqual(usage[0]["latency_ms"], 0)
        self.assertEqual(usage[0]["tool_round"], 0)

    def _report_args(self, **overrides):
        defaults = {
            "group_by": "role",
            "since": None,
            "until": None,
            "role": None,
            "model": None,
            "provider": None,
            "ticket": None,
            "sprint": None,
            "top": None,
            "format": "json",
        }
        defaults.update(overrides)
        return api_agent.argparse.Namespace(**defaults)

    def _write_ledger(self, events):
        directory = self.root / ".orchestration" / ".llm-usage"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "usage.jsonl").write_text(
            "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
            encoding="utf-8",
        )

    @staticmethod
    def _usage_event(role, cost, *, cache_read=900, fresh=100, latency=1000, age_hours=1, **extra):
        moment = api_agent.dt.datetime.now(api_agent.dt.timezone.utc) - api_agent.dt.timedelta(
            hours=age_hours
        )
        event = {
            "kind": "usage",
            "timestamp": moment.isoformat(),
            "reservation_id": f"resv_{role}_{age_hours}_{cost}",
            "run_id": f"run_{role}",
            "role": role,
            "ticket": "PROD-1",
            "sprint": "SPRINT-1",
            "provider": "anthropic",
            "model": "test-model",
            "response_id": f"msg_{role}_{age_hours}",
            "input_tokens": fresh,
            "cache_write_tokens": 0,
            "cache_read_tokens": cache_read,
            "output_tokens": 10,
            "reasoning_tokens": 0,
            "latency_ms": latency,
            "cost_usd": cost,
        }
        event.update(extra)
        return event

    def test_report_groups_by_role_with_cache_hit_rate(self):
        self._write_ledger(
            [
                self._usage_event("implementer", "0.500000", cache_read=900, fresh=100),
                self._usage_event("implementer", "0.250000", cache_read=900, fresh=100, age_hours=2),
                self._usage_event("code-reviewer", "0.100000", cache_read=500, fresh=500, age_hours=3),
            ]
        )
        report = api_agent.build_report(self.root, self._report_args())
        keys = [group["key"] for group in report["groups"]]
        self.assertEqual(keys, ["implementer", "code-reviewer"])
        implementer = report["groups"][0]
        self.assertEqual(implementer["requests"], 2)
        self.assertEqual(implementer["cache_hit_rate"], 0.9)
        self.assertEqual(report["groups"][1]["cache_hit_rate"], 0.5)
        self.assertEqual(report["totals"]["requests"], 3)
        self.assertEqual(report["totals"]["cost_usd"], "0.850000")

    def test_report_window_and_role_filter_narrow_the_ledger(self):
        self._write_ledger(
            [
                self._usage_event("implementer", "1.000000", age_hours=1),
                self._usage_event("implementer", "2.000000", age_hours=200),
                self._usage_event("code-reviewer", "4.000000", age_hours=1),
            ]
        )
        recent = api_agent.build_report(self.root, self._report_args(since="24h"))
        self.assertEqual(recent["totals"]["requests"], 2)
        self.assertEqual(recent["totals"]["cost_usd"], "5.000000")
        scoped = api_agent.build_report(self.root, self._report_args(role="code-reviewer"))
        self.assertEqual(scoped["totals"]["cost_usd"], "4.000000")
        self.assertEqual(scoped["filters"], {"role": "code-reviewer"})

    def test_report_tolerates_ledger_entries_without_performance_fields(self):
        legacy = self._usage_event("implementer", "0.100000")
        del legacy["latency_ms"]
        del legacy["role"]
        self._write_ledger([legacy])
        report = api_agent.build_report(self.root, self._report_args())
        self.assertEqual(report["groups"][0]["key"], "unknown")
        self.assertIsNone(report["totals"]["latency_p50_ms"])
        self.assertEqual(report["totals"]["latency_samples"], 0)
        self.assertIn("latency recorded for 0 of 1 requests", api_agent.format_report(report))

    def test_report_top_reports_what_it_hid(self):
        self._write_ledger(
            [self._usage_event(f"role-{index}", f"{index}.000000", age_hours=index + 1) for index in range(1, 5)]
        )
        report = api_agent.build_report(self.root, self._report_args(top=2))
        self.assertEqual(len(report["groups"]), 2)
        self.assertEqual(report["groups_hidden_by_top"], 2)
        self.assertEqual(report["totals"]["requests"], 4)
        self.assertIn("2 further groups hidden", api_agent.format_report(report))

    def test_parse_window_accepts_relative_and_absolute_forms(self):
        now = api_agent.dt.datetime.now(api_agent.dt.timezone.utc)
        self.assertLess(abs((now - api_agent.parse_window("30m")).total_seconds() - 1800), 5)
        self.assertLess(abs((now - api_agent.parse_window("2w")).total_seconds() - 1209600), 5)
        absolute = api_agent.parse_window("2026-08-01T00:00:00+00:00")
        self.assertEqual(absolute.year, 2026)
        # A naive timestamp is read as UTC rather than silently taking local time.
        self.assertEqual(api_agent.parse_window("2026-08-01").tzinfo, api_agent.dt.timezone.utc)
        with self.assertRaises(api_agent.AgentError):
            api_agent.parse_window("last tuesday")

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

    def test_bedrock_converse_tool_loop_and_usage(self):
        transport = FakeTransport(
            [
                {
                    "id": "aws-request-1",
                    "stopReason": "tool_use",
                    "usage": {
                        "inputTokens": 40,
                        "cacheReadInputTokens": 60,
                        "cacheWriteInputTokens": 10,
                        "outputTokens": 8,
                    },
                    "output": {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "toolUse": {
                                        "toolUseId": "tool-1",
                                        "name": "git_status",
                                        "input": {},
                                    }
                                }
                            ],
                        }
                    },
                },
                {
                    "id": "aws-request-2",
                    "stopReason": "end_turn",
                    "usage": {
                        "inputTokens": 30,
                        "cacheReadInputTokens": 70,
                        "outputTokens": 4,
                    },
                    "output": {
                        "message": {
                            "role": "assistant",
                            "content": [{"text": "done"}],
                        }
                    },
                },
            ]
        )
        config = self.config(
            provider="bedrock",
            extra="    implementer:\n      allowed_tools: [read_file, search, git_diff, git_status, run_check, apply_patch]",
        )
        agent = api_agent.ApiAgent(
            root=self.root,
            config_path=config,
            role="implementer",
            ticket="PROJ-4",
            sprint=None,
            run_id="bedrock-run",
            transport=transport,
        )
        result = agent.run(
            {
                "modelId": "test-model",
                "inferenceConfig": {"maxTokens": 100},
                "system": [{"text": "system"}],
                "messages": [{"role": "user", "content": [{"text": "work"}]}],
            }
        )
        converse_calls = [call for call in transport.calls if call[1] == "converse"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output_text"], "done")
        self.assertEqual(len(converse_calls), 2)
        self.assertFalse(any(call[1] == "count_tokens" for call in transport.calls))
        self.assertIn(
            "apply_patch",
            {
                tool["toolSpec"]["name"]
                for tool in converse_calls[0][2]["toolConfig"]["tools"]
                if "toolSpec" in tool
            },
        )
        tool_result = converse_calls[1][2]["messages"][-1]["content"][0]["toolResult"]
        self.assertEqual(tool_result["toolUseId"], "tool-1")
        self.assertEqual(tool_result["status"], "success")
        summary = agent.ledger.summary()
        self.assertEqual(summary["input_tokens"], 70)
        self.assertEqual(summary["cache_read_tokens"], 130)
        self.assertEqual(summary["cache_write_tokens"], 10)
        self.assertEqual(summary["output_tokens"], 12)

    def test_bedrock_cache_breakpoint_rolls_without_mutating_response(self):
        original = [
            {"role": "assistant", "content": [{"text": "before"}]},
            {"role": "user", "content": [{"text": "after"}]},
        ]
        rolled = api_agent.roll_bedrock_cache_breakpoint(original)
        self.assertEqual(original[-1]["content"], [{"text": "after"}])
        self.assertEqual(
            rolled[-1]["content"][-1],
            {"cachePoint": {"type": "default", "ttl": "1h"}},
        )

    def test_bedrock_openai_uses_local_count_and_no_claude_cache_points(self):
        model = "global.openai.gpt-5.6-sol"
        transport = FakeTransport(
            [
                {
                    "id": "aws-openai-1",
                    "stopReason": "end_turn",
                    "usage": {"inputTokens": 20, "outputTokens": 4},
                    "output": {
                        "message": {
                            "role": "assistant",
                            "content": [{"text": CLEAN_REVIEW}],
                        }
                    },
                }
            ]
        )
        agent = api_agent.ApiAgent(
            root=self.root,
            config_path=self.config(provider="bedrock", model=model),
            role="code-reviewer",
            ticket="PROJ-5",
            sprint=None,
            run_id="bedrock-openai-run",
            transport=transport,
        )
        result = agent.run(
            {
                "modelId": model,
                "inferenceConfig": {"maxTokens": 100},
                "system": [{"text": "system"}],
                "messages": [{"role": "user", "content": [{"text": "review"}]}],
                "additionalModelRequestFields": {"reasoning_effort": "xhigh"},
            }
        )
        converse = next(call for call in transport.calls if call[1] == "converse")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(any(call[1] == "count_tokens" for call in transport.calls))
        self.assertTrue(all("cachePoint" not in block for block in converse[2]["system"]))
        self.assertTrue(
            all("cachePoint" not in tool for tool in converse[2]["toolConfig"]["tools"])
        )

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
