"""Bounded installed-client compatibility probe against a loopback mock provider.

No real model endpoint or credentials are used. Codex must execute a local tool,
compact through metered Responses, and finish; all generations must settle.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import sys


def check(provider, model, executable=None):
    from native_gateway import (
        NativeGateway,
        claude_child_environment,
        claude_launch_arguments,
    )
    from codex_gateway import CodexGateway, child_environment, launch_arguments

    expected = {"anthropic": "claude", "openai": "codex"}.get(provider)
    executable = executable or (shutil.which(expected) if expected else None)
    if not executable:
        raise RuntimeError("configured native executable is unavailable")

    class Mock:
        def __init__(self):
            self.calls = 0
            self.tool_result = False

        def request(self, provider, path, payload, **kwargs):
            if path.endswith(("input_tokens", "count_tokens")):
                return {"input_tokens": 100}
            self.calls += 1
            if self.calls > 5:
                raise RuntimeError("compatibility probe exceeded five generations")
            if provider == "anthropic":
                return dict(
                    id="mock",
                    type="message",
                    role="assistant",
                    model=payload["model"],
                    stop_reason="end_turn",
                    stop_sequence=None,
                    content=[dict(type="text", text="ORKA_SMOKE_OK")],
                    usage=dict(input_tokens=100, output_tokens=5),
                )
            self.tool_result |= any(
                isinstance(i, dict)
                and i.get("type") == "function_call_output"
                and "ORKA_TOOL_OK" in str(i.get("output"))
                for i in payload.get("input", [])
                if isinstance(payload.get("input"), list)
            )
            output = [
                dict(
                    id="msg",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        dict(type="output_text", text="ORKA_SMOKE_OK", annotations=[])
                    ],
                )
            ]
            if self.calls == 1:
                output = [
                    dict(
                        id="tool",
                        type="function_call",
                        call_id="tool1",
                        name="exec_command",
                        status="completed",
                        arguments=json.dumps(
                            dict(cmd="printf ORKA_TOOL_OK", max_output_tokens=50)
                        ),
                    )
                ]
            return dict(
                id="mock-" + str(self.calls),
                object="response",
                created_at=1,
                model=payload["model"],
                status="completed",
                output=output,
                usage=dict(
                    input_tokens=100,
                    output_tokens=5,
                    total_tokens=105,
                    input_tokens_details=dict(cached_tokens=0),
                    output_tokens_details=dict(reasoning_tokens=0),
                ),
            )

    with tempfile.TemporaryDirectory(prefix="orka-client-check-") as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", directory], check=True)
        prices = dict(
            input_per_mtok=1,
            output_per_mtok=1,
            cache_read_per_mtok=1,
            cache_write_per_mtok=1,
        )
        config = {
            "llm": {
                "pricing": {
                    name: prices for name in {model, "claude-haiku-4-5-20251001"}
                }
            }
        }
        mock = Mock()
        gateway = (CodexGateway if provider == "openai" else NativeGateway)(
            root, config, "SMOKE-1", "health", "compatibility", mock
        )
        endpoint = gateway.start()
        env = (child_environment if provider == "openai" else claude_child_environment)(
            os.environ, gateway.token, endpoint
        )
        # Force auxiliary traffic into the rejecting loopback gateway too.
        env.update(
            HTTP_PROXY=endpoint,
            HTTPS_PROXY=endpoint,
            ALL_PROXY=endpoint,
            http_proxy=endpoint,
            https_proxy=endpoint,
            all_proxy=endpoint,
            NO_PROXY="localhost,127.0.0.1",
            no_proxy="localhost,127.0.0.1",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        )
        if provider == "openai":
            command = launch_arguments(
                [
                    executable,
                    "exec",
                    "--ignore-user-config",
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--json",
                    "--model",
                    model,
                    "-c",
                    "model_auto_compact_token_limit=1",
                    "Run the local tool, then reply ORKA_SMOKE_OK.",
                ],
                endpoint,
            )
        else:
            command = claude_launch_arguments(
                [
                    executable,
                    "-p",
                    "Reply ORKA_SMOKE_OK",
                    "--model",
                    model,
                    "--output-format",
                    "json",
                    "--setting-sources",
                    "",
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                ],
                gateway.token,
                endpoint,
            )
        child = None
        try:
            with (root / "output").open("w+") as output:
                child = subprocess.Popen(
                    command,
                    cwd=root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    start_new_session=True,
                )
                code = child.wait(timeout=35)
                output.seek(0)
                text = output.read()
            if code or "ORKA_SMOKE_OK" not in text or gateway.reason:
                raise RuntimeError(
                    "installed client failed bounded compatibility probe"
                )
            usage = [e for e in gateway.ledger.snapshot() if e.get("kind") == "usage"]
            if len(usage) != mock.calls or (
                provider == "openai" and (not mock.tool_result or mock.calls < 3)
            ):
                raise RuntimeError(
                    "installed client did not complete metered tool/compaction sequence"
                )
            return {
                "status": "pass",
                "client": expected,
                "generations": mock.calls,
                "compaction": "metered-responses"
                if provider == "openai"
                else "not_exercised",
            }
        finally:
            if child is not None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
            gateway.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["openai", "anthropic"], required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.provider, args.model)))
    except Exception as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        sys.exit(2)
