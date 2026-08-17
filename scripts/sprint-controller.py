#!/usr/bin/env python3
"""Host-neutral, resumable state machine for sprint orchestration.

Claude and Codex adapters query Jira and launch ticket workflows. This script
owns the shared safety-critical parts: dependency normalization, bounded lane
reservation, atomic checkpoints, restart reconciliation, and exact summaries.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
TERMINAL = {"completed", "blocked", "user_action"}
OUTCOMES = TERMINAL
DEFAULT_DONE = ["done", "closed", "resolved"]
DEFAULT_BLOCKED = ["blocked"]
DEFAULT_READY = ["ready", "to do", "open", "selected for development"]


class SprintError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def project_root() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def config_scalar(path: Path, key: str, default: str) -> str:
    if not path.exists():
        return default
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.*?)\s*(?:#.*)?$")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match and match.group(1):
            return unquote(match.group(1))
    return default


def config_list(path: Path, key: str, default: list[str]) -> list[str]:
    if not path.exists():
        return default
    lines = path.read_text(encoding="utf-8").splitlines()
    start = re.compile(rf"^{re.escape(key)}:\s*(?:#.*)?$")
    item = re.compile(r"^\s+-\s+(.*?)\s*(?:#.*)?$")
    in_block = False
    values: list[str] = []
    for line in lines:
        if start.match(line):
            in_block = True
            continue
        if not in_block:
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = item.match(line)
        if match:
            values.append(unquote(match.group(1)))
            continue
        break
    return values or default


def settings(args: argparse.Namespace) -> dict[str, Any]:
    root = project_root()
    config = Path(args.config).resolve() if args.config else root / ".orchestration/config.yaml"
    try:
        concurrency = int(config_scalar(config, "concurrency_max", "2"))
    except ValueError as exc:
        raise SprintError("concurrency_max must be an integer") from exc
    if concurrency < 1:
        raise SprintError("concurrency_max must be at least 1")
    configured_dir = Path(config_scalar(config, "sprint_checkpoint_dir", ".orchestration/.sprint-state"))
    requested_dir = Path(args.state_dir) if args.state_dir else configured_dir
    if requested_dir.is_absolute():
        raise SprintError("sprint checkpoint directory must be repository-relative")
    state_dir = (root / requested_dir).resolve()
    if state_dir != root and root not in state_dir.parents:
        raise SprintError("sprint checkpoint directory escapes the repository")
    return {
        "config": config,
        "concurrency_max": concurrency,
        "state_dir": state_dir,
        "ready": {x.casefold() for x in config_list(config, "sprint_ready_statuses", DEFAULT_READY)},
        "done": {x.casefold() for x in config_list(config, "sprint_done_statuses", DEFAULT_DONE)},
        "blocked": {x.casefold() for x in config_list(config, "sprint_blocked_statuses", DEFAULT_BLOCKED)},
    }


def normalize_key(value: Any) -> str:
    key = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key):
        raise SprintError(f"invalid Jira ticket key: {value!r}")
    return key


def sprint_identity(inventory: dict[str, Any]) -> tuple[str, str]:
    sprint = inventory.get("sprint")
    if not isinstance(sprint, dict):
        raise SprintError("inventory.sprint must be an object with id and name")
    sprint_id = str(sprint.get("id", "")).strip()
    if not sprint_id:
        raise SprintError("inventory.sprint.id is required; resolve 'active' to the Jira sprint id")
    return sprint_id, str(sprint.get("name", sprint_id)).strip() or sprint_id


def state_path(state_dir: Path, sprint_id: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", sprint_id).strip("-.")[:48] or "sprint"
    digest = hashlib.sha256(sprint_id.encode("utf-8")).hexdigest()[:10]
    return state_dir / f"{slug}-{digest}.json"


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SprintError(f"no sprint checkpoint at {path}; run sync first")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SprintError(f"cannot read checkpoint {path}: {exc}") from exc
    if value.get("schema_version") != SCHEMA_VERSION:
        raise SprintError(f"unsupported sprint checkpoint schema in {path}")
    return value


def save(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def initial_state(raw_status: str, cfg: dict[str, Any]) -> tuple[str, str]:
    folded = raw_status.casefold()
    if folded in cfg["done"]:
        return "completed", f"already {raw_status} in Jira"
    if folded in cfg["blocked"]:
        return "blocked", f"Jira status is {raw_status}"
    if folded in cfg["ready"]:
        return "pending", ""
    return "user_action", f"Jira status {raw_status!r} is not configured as ready, done, or blocked"


def normalized_inventory(raw: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    sprint_id, sprint_name = sprint_identity(raw)
    project = str(raw.get("project", "")).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", project):
        raise SprintError("inventory.project must be a Jira project key")
    source_query = str(raw.get("source_query", "")).strip()
    if not source_query:
        raise SprintError("inventory.source_query is required for auditability")
    raw_tickets = raw.get("tickets")
    if not isinstance(raw_tickets, list):
        raise SprintError("inventory.tickets must be an array")
    tickets: dict[str, dict[str, Any]] = {}
    for item in raw_tickets:
        if not isinstance(item, dict):
            raise SprintError("each inventory ticket must be an object")
        key = normalize_key(item.get("key"))
        if not key.startswith(f"{project}-"):
            raise SprintError(f"sprint ticket {key} is outside configured project {project}")
        if key in tickets:
            raise SprintError(f"duplicate ticket in inventory: {key}")
        dependencies: list[str] = []
        raw_dependencies = item.get("dependencies", [])
        if not isinstance(raw_dependencies, list):
            raise SprintError(f"ticket {key} dependencies must be an array")
        for dependency in raw_dependencies:
            normalized = normalize_key(dependency)
            if normalized not in dependencies:
                dependencies.append(normalized)
        raw_status = str(item.get("status", "")).strip()
        state, reason = initial_state(raw_status, cfg)
        tickets[key] = {
            "key": key,
            "summary": str(item.get("summary", "")).strip(),
            "url": str(item.get("url", "")).strip(),
            "raw_status": raw_status,
            "dependencies": sorted(dependencies),
            "state": state,
            "reason": reason,
            "run_ref": "",
            "branch": "",
            "pr": "",
            "attempts": 0,
            "history": [],
        }
    external: dict[str, str] = {}
    raw_external = raw.get("dependency_status", {})
    if not isinstance(raw_external, dict):
        raise SprintError("inventory.dependency_status must be an object when present")
    for key, status in raw_external.items():
        external[normalize_key(key)] = str(status).strip()
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "sprint": {"id": sprint_id, "name": sprint_name},
        "source_query": source_query,
        "tickets": tickets,
        "dependency_status": external,
        "created_at": now(),
        "updated_at": now(),
    }


def find_cycles(tickets: dict[str, dict[str, Any]]) -> dict[str, str]:
    visiting: list[str] = []
    visited: set[str] = set()
    cycle_reason: dict[str, str] = {}

    def visit(key: str) -> None:
        if key in visited:
            return
        if key in visiting:
            start = visiting.index(key)
            cycle = visiting[start:] + [key]
            reason = "dependency cycle: " + " -> ".join(cycle)
            for member in cycle[:-1]:
                cycle_reason[member] = reason
            return
        visiting.append(key)
        for dependency in tickets[key]["dependencies"]:
            if dependency in tickets:
                visit(dependency)
        visiting.pop()
        visited.add(key)

    for ticket_key in sorted(tickets):
        visit(ticket_key)
    return cycle_reason


def blockers(state: dict[str, Any], key: str, cfg: dict[str, Any]) -> list[str]:
    ticket = state["tickets"][key]
    reasons: list[str] = []
    cycles = find_cycles(state["tickets"])
    if key in cycles:
        reasons.append(cycles[key])
    for dependency in ticket["dependencies"]:
        if dependency == key:
            reasons.append(f"self dependency: {key}")
            continue
        internal = state["tickets"].get(dependency)
        if internal:
            dep_state = internal["state"]
            if dep_state == "completed":
                continue
            if dep_state in {"blocked", "user_action"}:
                reasons.append(f"dependency {dependency} ended {dep_state}")
            else:
                reasons.append(f"dependency {dependency} is {dep_state}")
            continue
        raw_status = state["dependency_status"].get(dependency)
        if raw_status is None:
            reasons.append(f"dependency {dependency} is outside the sprint and has no fetched status")
        elif raw_status.casefold() not in cfg["done"]:
            reasons.append(f"external dependency {dependency} is {raw_status}")
    return sorted(set(reasons))


def sync(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    inventory_path = Path(args.inventory)
    try:
        raw = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SprintError(f"cannot read inventory {inventory_path}: {exc}") from exc
    incoming = normalized_inventory(raw, cfg)
    path = state_path(cfg["state_dir"], incoming["sprint"]["id"])
    with locked(path):
        if path.exists():
            current = load(path)
            if current["sprint"]["id"] != incoming["sprint"]["id"]:
                raise SprintError("checkpoint sprint identity mismatch")
            incoming_keys = set(incoming["tickets"])
            for key, previous in current["tickets"].items():
                if key not in incoming_keys and previous["state"] == "pending":
                    previous["state"] = "user_action"
                    previous["reason"] = "ticket disappeared from the refreshed Jira sprint query"
                    previous["history"].append({"at": now(), "event": "removed-from-query"})
            for key, fresh in incoming["tickets"].items():
                previous = current["tickets"].get(key)
                if previous and previous["state"] in TERMINAL | {"running"}:
                    for field in ("state", "reason", "run_ref", "branch", "pr", "attempts", "history"):
                        fresh[field] = previous[field]
                current["tickets"][key] = fresh
            current["project"] = incoming["project"]
            current["sprint"] = incoming["sprint"]
            current["source_query"] = incoming["source_query"]
            current["dependency_status"] = incoming["dependency_status"]
            state = current
        else:
            state = incoming
        save(path, state)
    emit({"checkpoint": str(path), "sprint": state["sprint"], "tickets": len(state["tickets"])})


def get_state(args: argparse.Namespace, cfg: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = state_path(cfg["state_dir"], str(args.sprint))
    return path, load(path)


def plan_value(state: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    running = sorted(key for key, ticket in state["tickets"].items() if ticket["state"] == "running")
    ready = sorted(
        key for key, ticket in state["tickets"].items()
        if ticket["state"] == "pending" and not blockers(state, key, cfg)
    )
    available = max(0, cfg["concurrency_max"] - len(running))
    waiting = [
        {"key": key, "reasons": blockers(state, key, cfg)}
        for key, ticket in sorted(state["tickets"].items())
        if ticket["state"] == "pending" and blockers(state, key, cfg)
    ]
    launch = ready[:available]
    return {
        "sprint": state["sprint"],
        "concurrency_max": cfg["concurrency_max"],
        "running": running,
        "needs_reconcile": running,
        "launch": launch,
        "waiting": waiting,
        "autonomous_work_remaining": bool(running or launch),
        "over_capacity": max(0, len(running) - cfg["concurrency_max"]),
    }


def plan(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    with locked(path):
        state = load(path)
        emit(plan_value(state, cfg))


def reserve(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    if not args.run_ref.strip():
        raise SprintError("run reference must not be empty")
    with locked(path):
        state = load(path)
        if key not in state["tickets"]:
            raise SprintError(f"ticket {key} is not in the sprint checkpoint")
        ticket = state["tickets"][key]
        if ticket["state"] != "pending":
            raise SprintError(f"ticket {key} cannot be reserved from state {ticket['state']}")
        reasons = blockers(state, key, cfg)
        if reasons:
            raise SprintError(f"ticket {key} is blocked: {'; '.join(reasons)}")
        running = sum(1 for value in state["tickets"].values() if value["state"] == "running")
        if running >= cfg["concurrency_max"]:
            raise SprintError(f"concurrency_max={cfg['concurrency_max']} is already reached")
        ticket["state"] = "running"
        ticket["reason"] = ""
        ticket["run_ref"] = args.run_ref
        ticket["attempts"] += 1
        ticket["history"].append({"at": now(), "event": "reserved", "run_ref": args.run_ref})
        save(path, state)
    emit({"ticket": key, "state": "running", "run_ref": args.run_ref})


def attach(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    if not args.run_ref.strip():
        raise SprintError("run reference must not be empty")
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] != "running":
            raise SprintError(f"ticket {key} is not running")
        ticket["run_ref"] = args.run_ref
        ticket["history"].append({"at": now(), "event": "attached", "run_ref": args.run_ref})
        save(path, state)
    emit({"ticket": key, "state": "running", "run_ref": args.run_ref})


def finish(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    if not args.summary.strip():
        raise SprintError("finish summary must not be empty")
    if args.outcome == "completed" and (not args.pr.strip() or not args.branch.strip()):
        raise SprintError("completed outcome requires both PR and branch identity")
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] != "running":
            raise SprintError(f"ticket {key} is not running")
        ticket["state"] = args.outcome
        ticket["reason"] = args.summary.strip()
        ticket["branch"] = args.branch.strip()
        ticket["pr"] = args.pr.strip()
        ticket["history"].append({"at": now(), "event": "finished", "outcome": args.outcome})
        save(path, state)
    emit({"ticket": key, "state": args.outcome})


def requeue(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    if not args.reason.strip():
        raise SprintError("requeue reason must not be empty")
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] == "completed" or ticket["state"] == "pending":
            current = ticket["state"] if ticket else "missing"
            raise SprintError(f"ticket {key} cannot be requeued from state {current}")
        ticket["state"] = "pending"
        ticket["reason"] = args.reason.strip()
        ticket["run_ref"] = ""
        ticket["branch"] = ""
        ticket["pr"] = ""
        ticket["history"].append({"at": now(), "event": "requeued", "reason": args.reason.strip()})
        save(path, state)
    emit({"ticket": key, "state": "pending"})


def summary_value(state: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sprint": state["sprint"],
        "completed": [],
        "blocked": [],
        "user_action": [],
        "running": [],
    }
    for key, ticket in sorted(state["tickets"].items()):
        item = {
            "key": key,
            "summary": ticket["summary"],
            "reason": ticket["reason"],
            "pr": ticket["pr"],
            "branch": ticket["branch"],
            "run_ref": ticket["run_ref"],
        }
        if ticket["state"] == "completed":
            result["completed"].append(item)
        elif ticket["state"] == "user_action":
            result["user_action"].append(item)
        elif ticket["state"] == "blocked":
            result["blocked"].append(item)
        elif ticket["state"] == "running":
            result["running"].append(item)
        else:
            reasons = blockers(state, key, cfg)
            if reasons:
                item["reason"] = "; ".join(reasons)
                result["blocked"].append(item)
            else:
                item["reason"] = "ready but not launched"
                result["user_action"].append(item)
    result["finished"] = not result["running"] and not any(
        ticket["state"] == "pending" and not blockers(state, key, cfg)
        for key, ticket in state["tickets"].items()
    )
    return result


def summary(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    with locked(path):
        emit(summary_value(load(path), cfg))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", help="repo orchestration config (default: .orchestration/config.yaml)")
    result.add_argument("--state-dir", help="checkpoint directory override")
    commands = result.add_subparsers(dest="command", required=True)
    sync_parser = commands.add_parser("sync", help="normalize Jira inventory into a durable checkpoint")
    sync_parser.add_argument("--inventory", required=True)
    sync_parser.set_defaults(func=sync)
    for name, func in (("plan", plan), ("summary", summary)):
        command = commands.add_parser(name)
        command.add_argument("--sprint", required=True)
        command.set_defaults(func=func)
    reserve_parser = commands.add_parser("reserve")
    reserve_parser.add_argument("--sprint", required=True)
    reserve_parser.add_argument("--ticket", required=True)
    reserve_parser.add_argument("--run-ref", required=True)
    reserve_parser.set_defaults(func=reserve)
    attach_parser = commands.add_parser("attach")
    attach_parser.add_argument("--sprint", required=True)
    attach_parser.add_argument("--ticket", required=True)
    attach_parser.add_argument("--run-ref", required=True)
    attach_parser.set_defaults(func=attach)
    finish_parser = commands.add_parser("finish")
    finish_parser.add_argument("--sprint", required=True)
    finish_parser.add_argument("--ticket", required=True)
    finish_parser.add_argument("--outcome", required=True, choices=sorted(OUTCOMES))
    finish_parser.add_argument("--summary", required=True)
    finish_parser.add_argument("--branch", default="")
    finish_parser.add_argument("--pr", default="")
    finish_parser.set_defaults(func=finish)
    requeue_parser = commands.add_parser("requeue")
    requeue_parser.add_argument("--sprint", required=True)
    requeue_parser.add_argument("--ticket", required=True)
    requeue_parser.add_argument("--reason", required=True)
    requeue_parser.set_defaults(func=requeue)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        cfg = settings(args)
        args.func(args, cfg)
        return 0
    except SprintError as exc:
        print(f"sprint-controller: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
