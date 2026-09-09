#!/usr/bin/env python3
"""Host-neutral, resumable state machine for sprint orchestration.

Claude and Codex adapters query Jira and launch ticket workflows. This script
owns the shared safety-critical parts: dependency normalization, priority-ordered
readiness, bounded lane reservation, atomic checkpoints, restart reconciliation,
and exact summaries.
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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from api_agent import AgentError, Pricing, UsageLedger, budgets_from_config, load_yaml

from runtime_state import (
    RuntimeStateError,
    canonical_config_path,
    migrate_legacy_runtime_dir,
    shared_repository_root,
    working_repository_root,
)


SCHEMA_VERSION = 2
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
    return working_repository_root(Path.cwd())


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


def config_scalar_any_depth(path: Path, key: str, default: str) -> str:
    if not path.exists():
        return default
    pattern = re.compile(rf"^\s*{re.escape(key)}:\s*(.*?)\s*(?:#.*)?$")
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
    shared_root = shared_repository_root(root)
    try:
        config = canonical_config_path(root, args.config)
    except RuntimeStateError as exc:
        raise SprintError(str(exc)) from exc
    try:
        concurrency = int(config_scalar(config, "concurrency_max", "2"))
    except ValueError as exc:
        raise SprintError("concurrency_max must be an integer") from exc
    if concurrency < 1:
        raise SprintError("concurrency_max must be at least 1")
    configured_dir = Path(config_scalar(config, "sprint_checkpoint_dir", ".orchestration/.sprint-state"))
    if args.state_dir:
        raise SprintError("--state-dir overrides are not allowed; use the canonical repository config")
    requested_dir = configured_dir
    if requested_dir.is_absolute():
        raise SprintError("sprint checkpoint directory must be repository-relative")
    try:
        state_dir = migrate_legacy_runtime_dir(root, requested_dir)
        migrate_legacy_runtime_dir(root, ".orchestration/.llm-usage")
    except RuntimeStateError as exc:
        raise SprintError(str(exc)) from exc
    if state_dir != shared_root and shared_root not in state_dir.parents:
        raise SprintError("sprint checkpoint directory escapes the repository")
    try:
        max_lane_relaunches = int(config_scalar(config, "max_lane_relaunches", "2"))
    except ValueError as exc:
        raise SprintError("max_lane_relaunches must be an integer") from exc
    if max_lane_relaunches < 0:
        raise SprintError("max_lane_relaunches must be at least 0")
    try:
        warning_budget = min(float(config_scalar_any_depth(config, "warn_usd_per_ticket", "10")) or 10, 10)
        pause_budget = min(float(config_scalar_any_depth(config, "pause_usd_per_ticket", "20")) or 20, 20)
        max_model_runs = min(
            int(config_scalar_any_depth(config, "max_model_runs_per_ticket", "12")) or 12, 12
        )
        max_reviewer_runs = min(
            int(config_scalar_any_depth(config, "max_reviewer_runs_per_ticket", "6")) or 6, 6
        )
    except ValueError as exc:
        raise SprintError("ticket budgets and run limits must be numbers") from exc
    if max_model_runs < 1 or max_reviewer_runs < 1:
        raise SprintError("model and reviewer run limits must be positive")
    return {
        "config": config,
        "concurrency_max": concurrency,
        "state_dir": state_dir,
        "shared_root": shared_root,
        "max_lane_relaunches": max_lane_relaunches,
        "warn_usd_per_ticket": warning_budget,
        "pause_usd_per_ticket": pause_budget,
        "max_model_runs_per_ticket": max_model_runs,
        "max_reviewer_runs_per_ticket": max_reviewer_runs,
        "ready": {x.casefold() for x in config_list(config, "sprint_ready_statuses", DEFAULT_READY)},
        "done": {x.casefold() for x in config_list(config, "sprint_done_statuses", DEFAULT_DONE)},
        "blocked": {x.casefold() for x in config_list(config, "sprint_blocked_statuses", DEFAULT_BLOCKED)},
    }


def normalize_key(value: Any) -> str:
    key = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key):
        raise SprintError(f"invalid Jira ticket key: {value!r}")
    return key


def normalize_priority(value: Any, key: str) -> int | None:
    """Return an explicit integer rank, or None when the ticket carries none.

    Lower sorts first, matching Jira's own convention that priority 1 is the most
    urgent. Priority is optional per ticket: an inventory that omits it entirely
    schedules exactly as before.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SprintError(f"ticket {key} priority must be an integer or omitted")
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise SprintError(f"ticket {key} priority must be an integer or omitted") from exc


def order_key(ticket: dict[str, Any]) -> tuple[int, int, str]:
    """Sort tickets on (priority, key), unprioritized last.

    Unranked tickets cannot be compared against integers, and treating them as
    most urgent would let missing Jira data outrank an explicit decision, so they
    sort after every explicitly prioritized ticket and then by key.
    """
    priority = ticket.get("priority")
    if priority is None:
        return (1, 0, ticket["key"])
    return (0, priority, ticket["key"])


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
    if value.get("schema_version") == 1:
        value["schema_version"] = SCHEMA_VERSION
        for ticket in value.get("tickets", {}).values():
            if ticket.get("state") == "running":
                ticket["state"] = "user_action"
                ticket["reason"] = (
                    "legacy running lane requires explicit recovery; verify the old worker is stopped, "
                    "then run recover-legacy"
                )
                ticket.setdefault("history", []).append({"at": now(), "event": "legacy-running-fenced"})
                ticket["legacy_recovery_pending"] = True
            ticket["attempt_token"] = ""
            ticket["attempt_capability"] = {}
            ticket.setdefault("subtasks", [])
    if value.get("schema_version") != SCHEMA_VERSION:
        raise SprintError(f"unsupported sprint checkpoint schema in {path}")
    return value


def save(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    write_json(path, state)


def write_json(path: Path, value: Any) -> None:
    """Atomically persist JSON without changing the serialized API payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
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


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    """Atomically persist an OpenAI Batch input file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for value in values:
                handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
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
    subtask_source_query = str(raw.get("subtask_source_query", "")).strip()
    if not subtask_source_query:
        raise SprintError(
            "inventory.subtask_source_query is required; fetch sprint children independently"
        )
    raw_subtask_keys = raw.get("subtask_keys")
    if not isinstance(raw_subtask_keys, list):
        raise SprintError("inventory.subtask_keys must be the complete result of subtask_source_query")
    discovered_subtasks = {normalize_key(key) for key in raw_subtask_keys}
    raw_tickets = raw.get("tickets")
    if not isinstance(raw_tickets, list):
        raise SprintError("inventory.tickets must be an array")
    receipt = raw.get("fetch_receipt")
    if not isinstance(receipt, dict):
        raise SprintError(
            "inventory.fetch_receipt from the Jira fetch adapter is required; caller assertions are insufficient"
        )
    parent_keys = sorted(normalize_key(item.get("key")) for item in raw_tickets if isinstance(item, dict))
    receipt_payload = {
        "source": receipt.get("source"),
        "source_query": source_query,
        "subtask_source_query": subtask_source_query,
        "parent_keys": parent_keys,
        "child_keys": sorted(discovered_subtasks),
        "pages": receipt.get("pages"),
    }
    if receipt_payload["source"] != "jira-client" or not isinstance(receipt_payload["pages"], list) or not receipt_payload["pages"]:
        raise SprintError("Jira fetch receipt requires source=jira-client and complete pagination metadata")
    if any(
        not isinstance(page, dict) or not isinstance(page.get("start_at"), int)
        or not isinstance(page.get("count"), int) or page.get("count", -1) < 0
        for page in receipt_payload["pages"]
    ):
        raise SprintError("Jira fetch receipt pagination metadata is invalid")
    digest = hashlib.sha256(
        json.dumps(receipt_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if receipt.get("sha256") != digest:
        raise SprintError("Jira fetch receipt does not bind the query, pagination, parents, and children")
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
        subtasks: list[str] = []
        if "subtasks" not in item:
            raise SprintError(
                f"ticket {key} omits subtasks; Jira inventory must explicitly include an empty or complete array"
            )
        raw_subtasks = item["subtasks"]
        if not isinstance(raw_subtasks, list):
            raise SprintError(f"ticket {key} subtasks must be an array")
        for subtask in raw_subtasks:
            normalized = normalize_key(subtask.get("key") if isinstance(subtask, dict) else subtask)
            if normalized not in subtasks:
                subtasks.append(normalized)
        raw_status = str(item.get("status", "")).strip()
        state, reason = initial_state(raw_status, cfg)
        tickets[key] = {
            "key": key,
            "summary": str(item.get("summary", "")).strip(),
            "url": str(item.get("url", "")).strip(),
            "raw_status": raw_status,
            "priority": normalize_priority(item.get("priority"), key),
            "dependencies": sorted(dependencies),
            "subtasks": sorted(subtasks),
            "state": state,
            "reason": reason,
            "run_ref": "",
            "branch": "",
            "pr": "",
            "attempts": 0,
            "attempt_token": "",
            "history": [],
        }
    missing_subtasks = sorted(
        {subtask for ticket in tickets.values() for subtask in ticket["subtasks"] if subtask not in tickets}
    )
    if missing_subtasks:
        raise SprintError(
            "Jira inventory is incomplete; fetch every referenced subtask explicitly: "
            + ", ".join(missing_subtasks)
        )
    declared_subtasks = {subtask for ticket in tickets.values() for subtask in ticket["subtasks"]}
    if declared_subtasks != discovered_subtasks:
        missing_from_parents = sorted(discovered_subtasks - declared_subtasks)
        missing_from_query = sorted(declared_subtasks - discovered_subtasks)
        raise SprintError(
            "Jira subtask inventory disagrees with the independent child query; "
            f"unlinked query results={missing_from_parents}, absent query results={missing_from_query}"
        )
    absent_children = sorted(discovered_subtasks - set(tickets))
    if absent_children:
        raise SprintError("Jira child query results are absent from tickets: " + ", ".join(absent_children))
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
        "subtask_source_query": subtask_source_query,
        "subtask_keys": sorted(discovered_subtasks),
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


def usage_snapshots(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    path = cfg["shared_root"] / ".orchestration/.llm-usage/usage.jsonl"
    result: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return result
    open_reservations: dict[str, dict[str, Any]] = {}
    pause_events: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        ticket = str(event.get("ticket") or "")
        kind = event.get("kind")
        if kind == "reservation":
            open_reservations[str(event["reservation_id"])] = event
            if ticket:
                item = result.setdefault(
                    ticket, {"spent_usd": 0.0, "reserved_usd": 0.0, "run_ids": set(), "reviewer_run_ids": set()}
                )
                if event.get("run_id"):
                    item["run_ids"].add(str(event["run_id"]))
                    if event.get("role") in {"design-reviewer", "code-reviewer", "security-reviewer"}:
                        item["reviewer_run_ids"].add(str(event["run_id"]))
        elif kind in {"usage", "release"}:
            open_reservations.pop(str(event.get("reservation_id") or ""), None)
        if kind == "ticket_budget_pause" and ticket:
            pause_events[ticket] = max(
                pause_events.get(ticket, 0), float(event.get("projected_total_usd", 0))
            )
        if kind == "usage" and ticket:
            item = result.setdefault(ticket, {"spent_usd": 0.0, "reserved_usd": 0.0, "run_ids": set(), "reviewer_run_ids": set()})
            item["spent_usd"] += float(event.get("cost_usd", 0))
            if event.get("run_id"):
                item["run_ids"].add(str(event["run_id"]))
                if event.get("role") in {"design-reviewer", "code-reviewer", "security-reviewer"}:
                    item["reviewer_run_ids"].add(str(event["run_id"]))
    for event in open_reservations.values():
        ticket = str(event.get("ticket") or "")
        if ticket:
            item = result.setdefault(ticket, {"spent_usd": 0.0, "reserved_usd": 0.0, "run_ids": set(), "reviewer_run_ids": set()})
            item["reserved_usd"] += float(event.get("projected_cost_usd", 0))
            if event.get("run_id"):
                item["run_ids"].add(str(event["run_id"]))
                if event.get("role") in {"design-reviewer", "code-reviewer", "security-reviewer"}:
                    item["reviewer_run_ids"].add(str(event["run_id"]))
    for ticket, item in result.items():
        item["run_count"] = len(item.pop("run_ids"))
        item["reviewer_run_count"] = len(item.pop("reviewer_run_ids"))
        total = item["spent_usd"] + item["reserved_usd"]
        item["projected_total_usd"] = round(total, 6)
        pause = cfg["pause_usd_per_ticket"]
        warning = cfg["warn_usd_per_ticket"]
        item["state"] = (
            "operator_action" if (
                ticket in pause_events or (pause and total > pause)
                or item["run_count"] >= cfg["max_model_runs_per_ticket"]
                or item["reviewer_run_count"] >= cfg["max_reviewer_runs_per_ticket"]
            )
            else "warning" if warning and total > warning else "ok"
        )
    return result


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
                if previous:
                    for field in (
                        "state", "reason", "run_ref", "branch", "pr", "attempts",
                        "attempt_token", "history",
                        "attempt_capability", "legacy_recovery_pending",
                    ):
                        if field in previous:
                            fresh[field] = previous[field]
                current["tickets"][key] = fresh
            current["project"] = incoming["project"]
            current["sprint"] = incoming["sprint"]
            current["source_query"] = incoming["source_query"]
            current["subtask_source_query"] = incoming["subtask_source_query"]
            current["subtask_keys"] = incoming["subtask_keys"]
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
    spend = usage_snapshots(cfg)
    running = sorted(key for key, ticket in state["tickets"].items() if ticket["state"] == "running")
    ordered = sorted(state["tickets"].values(), key=order_key)
    ready = [
        ticket["key"] for ticket in ordered
        if ticket["state"] == "pending" and not blockers(state, ticket["key"], cfg)
        and spend.get(ticket["key"], {}).get("state") != "operator_action"
    ]
    available = max(0, cfg["concurrency_max"] - len(running))
    waiting = [
        {
            "key": ticket["key"],
            "priority": ticket.get("priority"),
            "reasons": blockers(state, ticket["key"], cfg),
        }
        for ticket in ordered
        if ticket["state"] == "pending" and blockers(state, ticket["key"], cfg)
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
        "spend": spend,
    }


def plan(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    with locked(path):
        state = load(path)
        emit(plan_value(state, cfg))


def prepare_batch(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    """Reserve background lanes and serialize an Anthropic Message Batch.

    Submission remains a host operation so this controller never handles API
    credentials. The request and marker make the asynchronous handoff durable.
    """
    path = state_path(cfg["state_dir"], str(args.sprint))
    try:
        source = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SprintError(f"cannot read batch jobs {args.jobs}: {exc}") from exc
    raw_jobs = source.get("jobs") if isinstance(source, dict) else None
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise SprintError("batch jobs must be a non-empty object with a jobs array")
    provider = str(source.get("provider", "anthropic")).strip().casefold()
    if provider not in {"anthropic", "openai"}:
        raise SprintError("batch provider must be anthropic or openai")

    jobs: dict[str, dict[str, Any]] = {}
    for job in raw_jobs:
        if not isinstance(job, dict):
            raise SprintError("each batch job must be an object")
        key = normalize_key(job.get("ticket"))
        if key in jobs:
            raise SprintError(f"duplicate batch ticket: {key}")
        if job.get("background") is not True or job.get("interactive") is not False:
            raise SprintError(f"ticket {key} is not a non-interactive background job")
        params = job.get("params")
        if not isinstance(params, dict):
            raise SprintError(f"ticket {key} batch params must be an object")
        required = ("model", "max_tokens", "messages") if provider == "anthropic" else (
            "model", "max_output_tokens", "input"
        )
        missing = [name for name in required if name not in params]
        if missing:
            raise SprintError(f"ticket {key} batch params missing: {', '.join(missing)}")
        if params.get("stream"):
            raise SprintError(f"ticket {key} batch params cannot enable streaming")
        input_key = "messages" if provider == "anthropic" else "input"
        if not isinstance(params.get(input_key), list) or not params[input_key]:
            raise SprintError(f"ticket {key} batch {input_key} must be a non-empty array")
        jobs[key] = params

    batch_id = uuid.uuid4().hex[:16]
    extension = "json" if provider == "anthropic" else "jsonl"
    request_path = cfg["state_dir"] / f"batch-{batch_id}.request.{extension}"
    marker_path = cfg["state_dir"] / f"batch-{batch_id}.state.json"
    with locked(path):
        state = load(path)
        current_plan = plan_value(state, cfg)
        launch_order = current_plan["launch"]
        unexpected = sorted(set(jobs) - set(launch_order))
        if unexpected:
            raise SprintError(
                "batch may contain only currently launchable tickets: " + ", ".join(unexpected)
            )
        ordered_keys = [key for key in launch_order if key in jobs]
        requests = []
        marker_jobs = []
        config = load_yaml(cfg["config"])
        limits = budgets_from_config(config)
        usage = UsageLedger(cfg["shared_root"])
        reservations: list[tuple[str, str]] = []
        prepared = []
        for key in ordered_keys:
            if state["tickets"][key]["attempts"] > cfg["max_lane_relaunches"]:
                raise SprintError(
                    f"ticket {key} exceeded max_lane_relaunches; background batches cannot supply human approval"
                )
            custom_id = f"ticket_{key.replace('-', '_')}_{batch_id}"
            run_ref = f"{provider}-batch:{batch_id}:{custom_id}"
            run_id = f"batch-{batch_id}-{key}"
            params = jobs[key]
            output_cap = int(params["max_tokens"] if provider == "anthropic" else params["max_output_tokens"])
            input_tokens = max(1, len(json.dumps(params, separators=(",", ":")).encode("utf-8")))
            projected = Pricing.from_config(config, str(params["model"])).worst_case(
                input_tokens, output_cap
            )
            prepared.append((key, custom_id, run_ref, run_id, projected))
        try:
            for key, custom_id, run_ref, run_id, projected in prepared:
                reservation_id = usage.reserve(
                    projected=projected, limits=limits, run_id=run_id, ticket=key,
                    sprint=str(args.sprint), provider=provider, model=str(jobs[key]["model"]),
                    role="sprint-worker",
                )
                reservations.append((reservation_id, run_id))
                if provider == "anthropic":
                    requests.append({"custom_id": custom_id, "params": jobs[key]})
                else:
                    requests.append(
                        {"custom_id": custom_id, "method": "POST", "url": "/v1/responses", "body": jobs[key]}
                    )
                marker_jobs.append({
                    "ticket": key, "custom_id": custom_id, "run_ref": run_ref,
                    "run_id": run_id, "reservation_id": reservation_id,
                    "projected_cost_usd": str(projected),
                })
        except (AgentError, ValueError) as exc:
            for reservation_id, run_id in reservations:
                usage.release(reservation_id, run_id, "batch preparation failed")
            raise SprintError(f"batch budget reservation failed: {exc}") from exc
        for marker_job in marker_jobs:
            key = marker_job["ticket"]
            custom_id = marker_job["custom_id"]
            run_ref = marker_job["run_ref"]
            run_id = marker_job["run_id"]
            ticket = state["tickets"][key]
            ticket["state"] = "running"
            ticket["reason"] = ""
            ticket["run_ref"] = run_ref
            ticket["attempts"] += 1
            ticket["attempt_token"] = "attempt_" + uuid.uuid4().hex
            ticket["attempt_capability"] = {
                "token": "attemptcap_" + uuid.uuid4().hex,
                "repository": str(cfg["shared_root"]), "sprint": str(args.sprint),
                "ticket": key, "role": "sprint-worker", "run_id": run_id,
                "worker": run_ref, "attempt": ticket["attempts"], "issued_at": now(),
            }
            marker_job["attempt_token"] = ticket["attempt_token"]
            marker_job["attempt_capability"] = ticket["attempt_capability"]["token"]
            ticket["history"].append(
                {"at": now(), "event": "batch-reserved", "batch_id": batch_id, "custom_id": custom_id}
            )
        if not requests:
            raise SprintError("none of the supplied batch jobs are currently launchable")
        request = {"requests": requests}
        endpoint = "/v1/messages/batches" if provider == "anthropic" else "/v1/batches"
        marker = {
            "schema_version": 1,
            "batch_id": batch_id,
            "sprint_id": state["sprint"]["id"],
            "provider": provider,
            "status": "pending_submission" if provider == "anthropic" else "pending_upload",
            "endpoint": endpoint,
            "request_file": str(request_path),
            "provider_batch_id": "",
            "jobs": marker_jobs,
            "created_at": now(),
            "updated_at": now(),
        }
        if provider == "anthropic":
            write_json(request_path, request)
        else:
            write_jsonl(request_path, requests)
        state.setdefault("batches", {})[batch_id] = marker
        save(path, state)
        write_json(marker_path, marker)
    emit(
        {
            "batch_id": batch_id,
            "provider": provider,
            "status": marker["status"],
            "request": str(request_path),
            "marker": str(marker_path),
            "tickets": ordered_keys,
        }
    )


def reconcile_batch(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    """Settle or release every reservation owned by one durable batch marker."""
    marker_path = cfg["state_dir"] / f"batch-{args.batch}.state.json"
    if not marker_path.is_file():
        raise SprintError(f"batch marker not found: {marker_path}")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("status") not in {"pending_submission", "pending_upload", "submitted"}:
        raise SprintError("batch is not awaiting reconciliation")
    usage_ledger = UsageLedger(cfg["shared_root"])
    results: dict[str, dict[str, Any]] = {}
    if args.outcome == "completed":
        if not args.results:
            raise SprintError("completed batch reconciliation requires --results")
        source = json.loads(Path(args.results).read_text(encoding="utf-8"))
        rows = source.get("jobs") if isinstance(source, dict) else None
        if not isinstance(rows, list):
            raise SprintError("batch results require a jobs array")
        results = {normalize_key(row.get("ticket")): row for row in rows if isinstance(row, dict)}
        if set(results) != {item["ticket"] for item in marker["jobs"]}:
            raise SprintError("batch results must cover every reserved ticket exactly once")
    config = load_yaml(cfg["config"])
    settlements: dict[str, tuple[dict[str, int], str, str]] = {}
    if args.outcome == "completed":
        reservation_events = {
            str(event.get("reservation_id")): event for event in usage_ledger._events()
            if event.get("kind") == "reservation"
        }
        for item in marker["jobs"]:
            row = results[item["ticket"]]
            raw_usage = row.get("usage")
            if not isinstance(raw_usage, dict):
                raise SprintError(f"batch result for {item['ticket']} requires usage")
            normalized = {
                key: int(raw_usage.get(key, 0))
                for key in ("input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens")
            }
            normalized["reasoning_tokens"] = int(raw_usage.get("reasoning_tokens", 0))
            response_id = str(row.get("response_id") or "")
            event = reservation_events.get(str(item["reservation_id"]))
            if sum(normalized.values()) <= 0 or not response_id or not event:
                raise SprintError(
                    f"batch result for {item['ticket']} requires an open reservation, response_id, and nonzero usage"
                )
            settlements[item["ticket"]] = (normalized, response_id, str(event["model"]))
    for item in marker["jobs"]:
        if args.outcome == "failed":
            usage_ledger.release(item["reservation_id"], item["run_id"], "provider batch failed")
            continue
        normalized, response_id, model = settlements[item["ticket"]]
        usage_ledger.settle(
            item["reservation_id"], run_id=item["run_id"], ticket=item["ticket"],
            sprint=str(marker["sprint_id"]), provider=str(marker["provider"]), model=str(model),
            response_id=response_id, usage=normalized,
            cost=Pricing.from_config(config, str(model)).actual_cost(normalized), role="sprint-worker",
        )
    marker["status"] = "completed" if args.outcome == "completed" else "failed"
    marker["updated_at"] = now()
    write_json(marker_path, marker)
    if args.outcome == "failed":
        checkpoint = state_path(cfg["state_dir"], str(marker["sprint_id"]))
        with locked(checkpoint):
            state = load(checkpoint)
            for item in marker["jobs"]:
                ticket = state["tickets"].get(item["ticket"])
                if ticket and ticket.get("state") == "running" and ticket.get("run_ref") == item["run_ref"]:
                    ticket.update({
                        "state": "pending", "reason": "provider batch failed before worker output",
                        "run_ref": "", "attempt_token": "", "attempt_capability": {},
                    })
                    ticket["history"].append({"at": now(), "event": "batch-failed-requeued"})
            save(checkpoint, state)
    emit({"batch_id": args.batch, "status": marker["status"]})


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
        if ticket["attempts"] > cfg["max_lane_relaunches"]:
            raise SprintError(
                f"ticket {key} exceeded max_lane_relaunches={cfg['max_lane_relaunches']}; "
                "operator policy change is required"
            )
        ticket["state"] = "running"
        ticket["reason"] = ""
        ticket["run_ref"] = args.run_ref
        ticket["attempts"] += 1
        ticket["attempt_token"] = "attempt_" + uuid.uuid4().hex
        capability_run_id = args.run_id or args.run_ref
        capability = {
            "token": "attemptcap_" + uuid.uuid4().hex,
            "repository": str(cfg["shared_root"]),
            "sprint": str(args.sprint),
            "ticket": key,
            "attempt": ticket["attempts"],
            "role": args.role,
            "run_id": capability_run_id,
            "worker": args.worker_ref or args.run_ref,
            "issued_at": now(),
        }
        ticket["attempt_capability"] = capability
        event = {"at": now(), "event": "reserved", "run_ref": args.run_ref}
        ticket["history"].append(event)
        save(path, state)
    emit({
        "ticket": key, "state": "running", "run_ref": args.run_ref,
        "attempt_token": ticket["attempt_token"], "attempt_capability": capability["token"],
        "attempt": ticket["attempts"],
    })


def require_attempt(ticket: dict[str, Any], supplied: str) -> None:
    expected = str(ticket.get("attempt_token") or "")
    if not expected or supplied != expected:
        raise SprintError("attempt token is missing or stale; refusing cross-attempt state mutation")


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
        require_attempt(ticket, args.attempt_token)
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
        require_attempt(ticket, args.attempt_token)
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
        require_worker_stopped(ticket, args.operator_capability, cfg)
        require_attempt(ticket, args.attempt_token)
        ticket["state"] = "pending"
        ticket["reason"] = args.reason.strip()
        ticket["run_ref"] = ""
        ticket["branch"] = ""
        ticket["pr"] = ""
        ticket["attempt_token"] = ""
        ticket["attempt_capability"] = {}
        ticket["history"].append({"at": now(), "event": "requeued", "reason": args.reason.strip()})
        save(path, state)
    emit({"ticket": key, "state": "pending"})


def recover_legacy(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    """Requeue a fenced schema-v1 lane after external process verification."""
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    if not args.reason.strip():
        raise SprintError("legacy recovery reason must not be empty")
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if (
            not ticket or ticket.get("state") != "user_action"
            or not ticket.get("legacy_recovery_pending")
        ):
            raise SprintError(f"ticket {key} is not a fenced legacy running lane")
        require_worker_stopped(ticket, args.operator_capability, cfg)
        ticket["state"] = "pending"
        ticket["reason"] = args.reason.strip()
        ticket["run_ref"] = ""
        ticket["attempt_token"] = ""
        ticket["attempt_capability"] = {}
        ticket["legacy_recovery_pending"] = False
        ticket["history"].append({"at": now(), "event": "legacy-recovered", "reason": args.reason.strip()})
        save(path, state)
    emit({"ticket": key, "state": "pending", "recovered": True})


def require_worker_stopped(ticket: dict[str, Any], operator_token: str, cfg: dict[str, Any]) -> None:
    """Use process liveness or consume a separately provisioned operator token."""
    run_ref = str(ticket.get("run_ref") or "")
    match = re.fullmatch(r"(?:pid|workspace-lease-pid):(\d+)", run_ref)
    if match:
        try:
            os.kill(int(match.group(1)), 0)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise SprintError("cannot verify prior worker process ownership") from exc
        raise SprintError(f"prior worker process {match.group(1)} is still alive")
    capability_path = cfg["shared_root"] / ".orchestration/operator-recovery.cap"
    if not operator_token or not capability_path.is_file():
        raise SprintError(
            "worker liveness is not mechanically verifiable; provide an out-of-band single-use operator capability"
        )
    expected = capability_path.read_text(encoding="utf-8").strip()
    if not expected or operator_token != expected:
        raise SprintError("operator recovery capability is invalid")
    capability_path.unlink()


def summary_value(state: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    spend = usage_snapshots(cfg)
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
            "priority": ticket.get("priority"),
            "summary": ticket["summary"],
            "reason": ticket["reason"],
            "pr": ticket["pr"],
            "branch": ticket["branch"],
            "run_ref": ticket["run_ref"],
            "attempts": ticket.get("attempts", 0),
            "spend": spend.get(key, {"spent_usd": 0.0, "reserved_usd": 0.0, "run_count": 0, "state": "ok"}),
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
                item["reason"] = (
                    "ticket spend pause requires durable human approval"
                    if item["spend"].get("state") == "operator_action"
                    else "ready but not launched"
                )
                result["user_action"].append(item)
    result["finished"] = not plan_value(state, cfg)["autonomous_work_remaining"]
    result["spend"] = spend
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
    batch_parser = commands.add_parser(
        "prepare-batch", help="serialize and reserve non-interactive background Message Batch jobs"
    )
    batch_parser.add_argument("--sprint", required=True)
    batch_parser.add_argument("--jobs", required=True)
    batch_parser.set_defaults(func=prepare_batch)
    reconcile_batch_parser = commands.add_parser("reconcile-batch")
    reconcile_batch_parser.add_argument("--batch", required=True)
    reconcile_batch_parser.add_argument("--outcome", required=True, choices=("completed", "failed"))
    reconcile_batch_parser.add_argument("--results")
    reconcile_batch_parser.set_defaults(func=reconcile_batch)
    reserve_parser = commands.add_parser("reserve")
    reserve_parser.add_argument("--sprint", required=True)
    reserve_parser.add_argument("--ticket", required=True)
    reserve_parser.add_argument("--run-ref", required=True)
    reserve_parser.add_argument("--run-id")
    reserve_parser.add_argument("--role", default="sprint-worker", choices=("implementer", "sprint-worker"))
    reserve_parser.add_argument("--worker-ref", default="")
    reserve_parser.set_defaults(func=reserve)
    attach_parser = commands.add_parser("attach")
    attach_parser.add_argument("--sprint", required=True)
    attach_parser.add_argument("--ticket", required=True)
    attach_parser.add_argument("--run-ref", required=True)
    attach_parser.add_argument("--attempt-token", required=True)
    attach_parser.set_defaults(func=attach)
    finish_parser = commands.add_parser("finish")
    finish_parser.add_argument("--sprint", required=True)
    finish_parser.add_argument("--ticket", required=True)
    finish_parser.add_argument("--outcome", required=True, choices=sorted(OUTCOMES))
    finish_parser.add_argument("--summary", required=True)
    finish_parser.add_argument("--branch", default="")
    finish_parser.add_argument("--pr", default="")
    finish_parser.add_argument("--attempt-token", required=True)
    finish_parser.set_defaults(func=finish)
    requeue_parser = commands.add_parser("requeue")
    requeue_parser.add_argument("--sprint", required=True)
    requeue_parser.add_argument("--ticket", required=True)
    requeue_parser.add_argument("--reason", required=True)
    requeue_parser.add_argument("--attempt-token", required=True)
    requeue_parser.add_argument("--operator-capability", default="")
    requeue_parser.add_argument("--worker-stopped", action="store_true", help=argparse.SUPPRESS)
    requeue_parser.set_defaults(func=requeue)
    recover_parser = commands.add_parser("recover-legacy")
    recover_parser.add_argument("--sprint", required=True)
    recover_parser.add_argument("--ticket", required=True)
    recover_parser.add_argument("--reason", required=True)
    recover_parser.add_argument("--operator-capability", default="")
    recover_parser.set_defaults(func=recover_legacy)
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
