"""Repository-wide provider admission, independent of ticket attempts and budgets.

Cooperative-worker runtime state. Only controller-owned probes clear incidents;
normal worker successes cannot clear a concurrent provider/authentication failure.
"""

from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
from pathlib import Path
import time
import uuid


class HealthError(RuntimeError):
    pass


class ProviderHealth:
    def __init__(self, root):
        from runtime_state import shared_repository_root

        self.directory = (
            shared_repository_root(Path(root)) / ".orchestration/.provider-health"
        )

    @contextlib.contextmanager
    def locked(self, provider):
        if provider not in {
            "openai",
            "anthropic",
            "azure_adm",
            "bedrock",
            "bedrock_mantle",
        }:
            raise HealthError("unsupported provider")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (provider + ".json")
        with (self.directory / (provider + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(path.read_text()) if path.exists() else {}
                if not isinstance(state, dict):
                    raise ValueError("expected object")
            except (ValueError, OSError) as exc:
                raise HealthError(
                    "provider health evidence is unreadable; repair is required"
                ) from exc
            yield state
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
            try:
                with temporary.open("x") as out:
                    json.dump(state, out, sort_keys=True)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def status(self, provider, route=None):
        with self.locked(provider) as state:
            result = dict(state)
        result.setdefault("state", "unverified")
        if result["state"] == "healthy" and (
            result.get("valid_until", 0) < time.time()
            or route is not None
            and route not in result.get("routes", [])
        ):
            result["state"] = "unverified"
        result.pop("probe_token", None)
        return result

    def failure(self, provider, reason, retry_after=30):
        if reason not in {
            "rate_limited",
            "authentication",
            "incompatible",
            "transport",
        }:
            raise HealthError("invalid provider incident")
        with self.locked(provider) as state:
            # A later transient failure must not erase an authentication hold.
            if state.get("state") in {"authentication", "incompatible"}:
                return
            count = state.get("probe_count", 0)
            failures = state.get("failures", 0) + 1
            delay = max(
                30,
                min(3600, float(retry_after or 30)),
                min(900, 30 * 2 ** min(failures - 1, 5)),
            )
            state.clear()
            state.update(
                state=reason,
                failures=failures,
                probe_count=count,
                at=time.time(),
                retry_at=time.time() + delay,
                incident=uuid.uuid4().hex,
            )

    def claim_probe(self, provider, repair=False):
        with self.locked(provider) as state:
            if state.get("probe_until", 0) > time.time() or (
                not repair and state.get("retry_at", 0) > time.time()
            ):
                return None
            if state.get("state") in {"authentication", "incompatible"} and not repair:
                return None
            if state.get("probe_count", 0) >= 3 and not repair:
                return None
            token = uuid.uuid4().hex
            state.update(
                probe_token=token,
                probe_until=time.time() + 60,
                probe_count=1 if repair else state.get("probe_count", 0) + 1,
            )
            return token

    def complete_probe(self, provider, token, outcome, route="", retry_after=30):
        if not token or outcome not in {
            "healthy",
            "authentication",
            "rate_limited",
            "incompatible",
            "transport",
        }:
            return False
        with self.locked(provider) as state:
            if state.get("probe_token") != token:
                return False
            if outcome == "healthy":
                routes = (
                    set(state.get("routes", []))
                    if state.get("state") == "healthy"
                    else set()
                )
                routes.add(route)
                state.clear()
                state.update(
                    state="healthy",
                    routes=sorted(routes),
                    valid_until=time.time() + 300,
                )
            else:
                count = state.get("probe_count", 1)
                state.clear()
                state.update(
                    state=outcome,
                    at=time.time(),
                    retry_at=time.time() + max(30, min(3600, float(retry_after or 30))),
                    probe_count=count,
                    incident=uuid.uuid4().hex,
                )
            return True


def route_identity(route):
    # No credentials or credential digests are persisted.
    identity = {k: v for k, v in route.items() if k != "role"}
    if route.get("execution") == "desktop":
        client = {"openai": "codex", "anthropic": "claude"}.get(route.get("provider"))
        executable = shutil.which(client) if client else None
        identity["executable"] = executable
        if executable:
            resolved = Path(executable).resolve()
            stat = resolved.stat()
            identity["client_revision"] = [
                str(resolved),
                stat.st_size,
                stat.st_mtime_ns,
            ]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def validate_native_command(command, route):
    """Enforce explicit model and prevent profiles/config from replacing routing."""
    command = list(command)
    expected = {"anthropic": "claude", "openai": "codex"}.get(route.get("provider"))
    if route.get("execution") != "desktop" or not expected or not route.get("model"):
        raise HealthError(
            "native launch requires an explicit supported desktop worker route"
        )
    if not command or Path(command[0]).name != expected:
        raise HealthError(
            "worker executable does not match resolved sprint-worker provider"
        )
    if expected == "codex" and (len(command) < 2 or command[1] != "exec"):
        raise HealthError("native Codex requires direct codex exec")
    resolved = shutil.which(command[0])
    expected_path = shutil.which(expected)
    if (
        not resolved
        or not expected_path
        or Path(resolved).resolve() != Path(expected_path).resolve()
    ):
        raise HealthError(
            "worker executable differs from the installed client checked on PATH"
        )
    command[0] = resolved
    values = []
    efforts = []
    for i, arg in enumerate(command[1:], 1):
        if arg == "--":
            break
        if (
            expected == "codex"
            and arg.startswith("-p")
            or arg.startswith("--local-provider=")
        ):
            raise HealthError(
                "worker provider/profile override is not controller-authorized"
            )
        if arg in {"--model", "-m"}:
            values.append(command[i + 1] if i + 1 < len(command) else "")
        elif arg.startswith("--model="):
            values.append(arg.split("=", 1)[1])
        elif arg.startswith("-m") and arg != "-m":
            values.append(arg[2:])
        if arg == "--effort":
            efforts.append(command[i + 1] if i + 1 < len(command) else "")
        elif arg.startswith("--effort="):
            efforts.append(arg.split("=", 1)[1])
        if arg in {
            "--profile",
            "-p" if expected == "codex" else "--settings",
            "--setting-sources",
            "--agent",
            "--agents",
            "--fallback-model",
            "--oss",
            "--local-provider",
        } or any(
            arg.startswith(x + "=")
            for x in [
                "--profile",
                "--settings",
                "--setting-sources",
                "--agent",
                "--agents",
                "--fallback-model",
            ]
        ):
            raise HealthError(
                "worker settings/profile override is not controller-authorized"
            )
        value = ""
        if arg in {"-c", "--config"}:
            value = command[i + 1] if i + 1 < len(command) else ""
        elif arg.startswith("--config="):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-c") and arg != "-c":
            value = arg[2:]
        if value:
            key = value.split("=", 1)[0].strip()
            if key not in {"sandbox_mode", "approval_policy"}:
                raise HealthError("worker config override is not controller-authorized")
    if values != [route["model"]]:
        raise HealthError("worker must specify the resolved model exactly once")
    effort = route.get("effort")
    if efforts and efforts != [effort]:
        raise HealthError("worker effort differs from resolved route")
    if effort and not efforts:
        if expected == "claude":
            command.extend(["--effort", effort])
        else:
            command[2:2] = ["-c", "model_reasoning_effort=" + json.dumps(effort)]
    return command


def probe(root, config, role="sprint-worker", repair=False, transport=None):
    """One bounded, serialized token-count probe; no ticket reservation or model generation."""
    from context_pipeline import llm_route_from_config
    from api_agent import (
        AgentError,
        HttpTransport,
        ProviderHTTPError,
        load_orchestration_env,
    )

    route = llm_route_from_config(Path(config), role)
    provider = route["provider"]
    identity = route_identity(route)
    health = ProviderHealth(root)
    if not route.get("model"):
        raise HealthError("configured route has no explicit model")
    existing = health.status(provider, identity)
    if existing["state"] == "healthy" and not repair:
        return existing
    token = health.claim_probe(provider, repair=repair)
    if token is None:
        return health.status(provider, identity)
    transport = transport or HttpTransport(timeout=15)
    retry_after = 30
    try:
        load_orchestration_env(Path(config))
        if route["execution"] == "desktop":
            from runtime_smoke import check

            try:
                check(provider, route["model"])
            except Exception as exc:
                raise HealthError("installed client is incompatible") from exc
        if provider == "anthropic":
            reply = transport.request(
                provider,
                "/messages/count_tokens",
                {
                    "model": route["model"],
                    "messages": [{"role": "user", "content": "health"}],
                },
            )
        elif provider == "openai":
            reply = transport.request(
                provider,
                "responses/input_tokens",
                {"model": route["model"], "input": "health"},
            )
        else:
            raise HealthError(
                "this provider requires a supported health-probe adapter before admission"
            )
        if (
            not isinstance(reply.get("input_tokens"), int)
            or isinstance(reply["input_tokens"], bool)
            or reply["input_tokens"] < 1
        ):
            raise HealthError("health probe returned invalid token-count evidence")
        outcome = "healthy"
    except ProviderHTTPError as exc:
        retry_after = exc.retry_after_seconds or 30
        outcome = (
            "authentication"
            if exc.status in {401, 403}
            else "rate_limited"
            if exc.status in {429, 529}
            else "incompatible"
        )
    except HealthError:
        outcome = "incompatible"
    except AgentError as exc:
        outcome = "authentication" if "API_KEY is required" in str(exc) else "transport"
    except Exception:
        outcome = "transport"
    health.complete_probe(provider, token, outcome, identity, retry_after)
    return health.status(provider, identity)


class ProviderTransport:
    """Apply shared incident admission to API roles as well as native workers."""

    def __init__(self, root, transport):
        self.health = ProviderHealth(root)
        self.transport = transport
        self.retry_owner = None
        self.retry_incident = None

    def __getattr__(self, name):
        return getattr(self.transport, name)

    def request(self, provider, path, payload, **kwargs):
        from api_agent import ProviderAdmissionError, ProviderHTTPError

        state = self.health.status(provider)
        own_retry = (
            state["state"] == "rate_limited"
            and kwargs.get("idempotency_key") is not None
            and kwargs.get("idempotency_key") == self.retry_owner
            and state.get("incident") == self.retry_incident
        )
        if (
            state["state"]
            in {"rate_limited", "authentication", "incompatible", "transport"}
            and not own_retry
        ):
            raise ProviderAdmissionError("provider admission held: " + state["state"])
        try:
            return self.transport.request(provider, path, payload, **kwargs)
        except ProviderHTTPError as exc:
            if exc.status in {401, 403}:
                self.health.failure(provider, "authentication")
            elif exc.status in {429, 529}:
                self.health.failure(provider, "rate_limited", exc.retry_after_seconds)
                self.retry_owner = kwargs.get("idempotency_key")
                self.retry_incident = self.health.status(provider).get("incident")
            raise
