#!/usr/bin/env python3
"""Durable cross-gate, cross-round review ledger for one PR.

The orchestrator is a lossy relay with a context window that compacts, so a
ledger it "maintains" in conversation is forgotten on long tickets -- and
failed repairs lose their history. This script owns that
state on disk instead: normalized component keys, strike counts across every
gate and round, the blocking/advisory split, the scope-freeze mode for each
round, and the hard round cap that ends an unconverged loop at a human.

Findings are keyed by `<path>:<symbol>`, normalized here so that two reviewers
naming the same defect differently still land on one key and accumulate strikes.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import context_pipeline


SCHEMA_VERSION = 1
DEFAULT_MAX_REPAIR_CYCLES = 2
DEFAULT_MAX_DESIGN_ROUNDS = 5
DEFAULT_LEDGER_DIR = ".orchestration/.review-ledger"
SEVERITIES = ("blocking", "advisory")

# Round 1 sweeps the whole diff with full authority to block. Later rounds still
# sweep the whole diff, but only ledger findings and regressions in the delta may
# block -- that is what makes the blocking set shrink monotonically.
FULL = "full-authority"
FROZEN = "scope-frozen"

ACTION_REVIEW = "review"
ACTION_REDESIGN = "redesign"
ACTION_ESCALATE = "escalate-human"
ACTION_CLEAR = "gates-clear"


class LedgerError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


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


# --- component keys -----------------------------------------------------------

_COMPONENT_WRAPPER = re.compile(r"^\[?\s*component\s*:\s*(.*?)\s*\]?$", re.IGNORECASE)


def normalize_key(raw: str) -> str:
    """Reduce a reviewer-supplied component key to a stable `<path>:<symbol>`.

    Reviewers run fresh every round with no memory of prior keys, so free-text
    subsystem names drift ("auth/sessionStore" then "session-refresh") and the
    same defect never accumulates a second strike. Anchoring the key to the file
    path plus the enclosing symbol makes it mechanically derivable from the diff
    instead of invented, and line numbers -- which drift on every rebase -- are
    discarded rather than treated as identity.
    """
    value = raw.strip()
    if not value:
        raise LedgerError("component key is empty")
    match = _COMPONENT_WRAPPER.match(value)
    if match:
        value = match.group(1).strip()
    value = value.strip("[]").strip()
    if not value:
        raise LedgerError(f"component key is empty after normalization: {raw!r}")

    parts = [segment.strip() for segment in value.split(":") if segment.strip()]
    if not parts:
        raise LedgerError(f"component key is empty after normalization: {raw!r}")
    # A trailing all-digits segment is a line number, not identity.
    while len(parts) > 1 and parts[-1].isdigit():
        parts.pop()

    path = parts[0].lower().lstrip("./")
    path = re.sub(r"/{2,}", "/", path).strip("/")
    symbol = ""
    if len(parts) > 1:
        symbol = parts[-1].lower()
        symbol = symbol.replace("()", "")
        symbol = re.sub(r"[^a-z0-9_.\-/]+", "-", symbol).strip("-")
    if not path:
        raise LedgerError(f"component key has no path segment: {raw!r}")
    return f"{path}:{symbol}" if symbol else path


# --- state --------------------------------------------------------------------


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


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise LedgerError(f"no review ledger at {path}; run `open` first")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot read review ledger {path}: {exc}") from exc
    if value.get("schema_version") != SCHEMA_VERSION:
        raise LedgerError(f"unsupported review ledger schema in {path}")
    # v0.7 counted failed gate passes because it had no explicit repair artifact.
    # Preserve that spent budget as synthetic attempts instead of silently
    # resetting a live PR when v0.8 first writes it.
    if "repair_attempts" not in value:
        legacy_failures = [
            entry for entry in value.get("rounds", []) if entry.get("effective_verdict") == "FAIL"
        ]
        value["repair_attempts"] = [
            {
                "attempt": index,
                "recorded_at": entry.get("recorded_at", value.get("updated_at", now())),
                "completed_at": entry.get("recorded_at", value.get("updated_at", now())),
                "head": "legacy-unknown",
                "findings": [],
                "open_before": entry.get("blocking", []),
                "open_after": entry.get("blocking", []),
                "closed": [],
                "required_gates": [entry.get("gate", "code-review")],
                "reviewed_gates": [entry.get("gate", "code-review")],
                "legacy": True,
            }
            for index, entry in enumerate(legacy_failures, start=1)
        ]
        value["repair_pending_review"] = False
    value.setdefault("repair_pending_review", False)
    value.setdefault(
        "design",
        {"max_rounds": DEFAULT_MAX_DESIGN_ROUNDS, "rounds": [], "escalated": False},
    )
    return value


def ledger_path(args: argparse.Namespace) -> Path:
    root = project_root()
    if args.ledger_dir:
        directory = Path(args.ledger_dir)
    else:
        cfg = Path(args.config) if args.config else root / ".orchestration/config.yaml"
        directory = Path(config_scalar(cfg, "review_ledger_dir", DEFAULT_LEDGER_DIR))
    if not directory.is_absolute():
        directory = root / directory
    pr = re.sub(r"[^A-Za-z0-9_.-]", "-", str(args.pr)).strip("-")
    if not pr:
        raise LedgerError(f"invalid pr identifier: {args.pr!r}")
    return directory / f"pr-{pr}.json"


def positive_config_int(args: argparse.Namespace, cli_name: str, key: str, default: int) -> int:
    override = getattr(args, cli_name, None)
    cfg = Path(args.config) if args.config else project_root() / ".orchestration/config.yaml"
    value = str(override) if override is not None else config_scalar(cfg, key, str(default))
    try:
        result = int(value)
    except ValueError as exc:
        raise LedgerError(f"{key} must be an integer, got {value!r}") from exc
    if result < 1:
        raise LedgerError(f"{key} must be >= 1, got {result}")
    return result


def max_rounds_for(args: argparse.Namespace) -> int:
    if getattr(args, "max_rounds", None):
        value = str(args.max_rounds)
    else:
        cfg = Path(args.config) if args.config else project_root() / ".orchestration/config.yaml"
        # max_review_rounds is the v0.7 compatibility alias. New repositories
        # distinguish pre-code design rounds from post-code repair attempts.
        value = config_scalar(cfg, "max_repair_cycles", "")
        if not value:
            value = config_scalar(cfg, "max_review_rounds", str(DEFAULT_MAX_REPAIR_CYCLES))
    try:
        rounds = int(value)
    except ValueError as exc:
        raise LedgerError(f"max_repair_cycles must be an integer, got {value!r}") from exc
    if rounds < 1:
        raise LedgerError(f"max_repair_cycles must be >= 1, got {rounds}")
    return rounds


def new_state(pr: str, max_rounds: int) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "pr": str(pr),
        "created_at": now(),
        "updated_at": now(),
        "max_rounds": max_rounds,
        "repair_attempts": [],
        "repair_pending_review": False,
        "design": {
            "max_rounds": DEFAULT_MAX_DESIGN_ROUNDS,
            "rounds": [],
            "escalated": False,
        },
        "rounds": [],
        "components": {},
        "escalated": False,
    }


def open_components(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in state["components"].values() if c["status"] == "open"]


def redesign_pending(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        c
        for c in open_components(state)
        if c.get("repair_failures", 0) >= 1
        and c.get("repair_failures", 0) > c.get("redesigned_at_repair_failure", 0)
    ]


def decide(state: dict[str, Any]) -> dict[str, Any]:
    """Derive the loop's next action. Precedence: clear > escalate > redesign > review."""
    recorded = len(state["rounds"])
    next_round = recorded + 1
    max_rounds = state["max_rounds"]
    # New ledgers count explicit completed repairs. Old v0.7 ledgers did not
    # record them, so retain their historical failed-pass count on load.
    if "repair_attempts" in state:
        fix_cycles = len(state["repair_attempts"])
    else:
        fix_cycles = sum(1 for entry in state["rounds"] if entry["effective_verdict"] == "FAIL")
    blocking = sorted(c["key"] for c in open_components(state))
    pending = sorted(c["key"] for c in redesign_pending(state))

    gate_verdicts = {}
    for entry in state["rounds"]:
        gate_verdicts[entry["gate"]] = entry["effective_verdict"]
    pending_review = bool(state.get("repair_pending_review"))
    gates_clear = not pending_review and bool(gate_verdicts) and not blocking and all(
        verdict == "PASS" for verdict in gate_verdicts.values()
    )

    cap_reached = not pending_review and fix_cycles >= max_rounds and bool(blocking)
    if gates_clear:
        action = ACTION_CLEAR
    elif state.get("escalated") or cap_reached:
        action = ACTION_ESCALATE
    elif pending:
        action = ACTION_REDESIGN
    else:
        action = ACTION_REVIEW

    return {
        "pr": state["pr"],
        "rounds_recorded": recorded,
        "next_round": next_round,
        "max_rounds": max_rounds,
        "fix_cycles": fix_cycles,
        "fix_cycles_remaining": max(0, max_rounds - fix_cycles),
        "next_scope_mode": FULL if next_round == 1 else FROZEN,
        "uncertainty_rule": "block-on-doubt" if fix_cycles == 0 else "advisory-on-doubt",
        "open_blocking": blocking,
        "redesign_required": pending,
        "gate_verdicts": gate_verdicts,
        "cap_reached": cap_reached,
        "repair_pending_review": pending_review,
        "next_action": action,
    }


# --- commands -----------------------------------------------------------------


def cmd_open(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    rounds = max_rounds_for(args)
    with locked(path):
        if path.exists():
            state = load(path)
            if getattr(args, "max_rounds", None):
                state["max_rounds"] = rounds
            if getattr(args, "max_design_rounds", None):
                _design_state(
                    state,
                    positive_config_int(
                        args, "max_design_rounds", "max_design_rounds", DEFAULT_MAX_DESIGN_ROUNDS
                    ),
                )
            save(path, state)
        else:
            state = new_state(args.pr, rounds)
            state["design"]["max_rounds"] = positive_config_int(
                args, "max_design_rounds", "max_design_rounds", DEFAULT_MAX_DESIGN_ROUNDS
            )
            save(path, state)
        emit({"ledger": str(path), **decide(state)})


def _component(state: dict[str, Any], key: str, raw: str, round_no: int) -> dict[str, Any]:
    component = state["components"].get(key)
    if component is None:
        component = {
            "key": key,
            "display": raw.strip(),
            "strikes": 0,
            "status": "open",
            "first_round": round_no,
            "rounds": [],
            "gates": [],
            "redesigned_at_strike": 0,
            "repair_failures": 0,
            "redesigned_at_repair_failure": 0,
        }
        state["components"][key] = component
    return component


def cmd_record(args: argparse.Namespace) -> None:
    finding_details: dict[str, dict[str, Any]] = {}
    if args.result:
        if args.verdict or args.blocking or args.advisory or args.regression:
            raise LedgerError("--result cannot be combined with manual verdict or finding flags")
        try:
            structured = json.loads(Path(args.result).read_text(encoding="utf-8"))
            context_pipeline.validate_review_output(structured, args.gate)
        except (OSError, json.JSONDecodeError, context_pipeline.ContextError) as exc:
            raise LedgerError(f"invalid structured review result: {exc}") from exc
        args.verdict = structured["verdict"]
        args.blocking = [
            item["component"] for item in structured["findings"]
            if item["disposition"] == "blocking"
        ]
        args.advisory = [
            item["component"] for item in structured["findings"]
            if item["disposition"] == "advisory"
        ]
        args.regression = [
            item["component"] for item in structured["findings"] if item["regression"]
        ]
        finding_details = {
            normalize_key(item["component"]): item for item in structured["findings"]
        }
    elif not args.verdict:
        raise LedgerError("record requires either --result or --verdict")
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        plan = decide(state)
        if plan["next_action"] == ACTION_ESCALATE:
            raise LedgerError(
                f"PR {state['pr']} spent its {state['max_rounds']} fix cycles with "
                f"{len(plan['open_blocking'])} blocking component(s) still open. Hand it to a "
                f"human (`handoff {state['pr']}`), or raise the cap deliberately with "
                f"`open {state['pr']} --max-rounds N`."
            )
        if args.verdict == "PASS" and args.blocking:
            raise LedgerError(
                f"verdict PASS contradicts {len(args.blocking)} blocking finding(s): "
                f"{', '.join(args.blocking)}"
            )
        round_no = len(state["rounds"]) + 1
        scope = FULL if round_no == 1 else FROZEN
        # The security gate never loses blocking authority to the scope freeze: a
        # data leak found late is not a process nit.
        exempt = args.gate == "security-review"
        regressions = {normalize_key(k) for k in args.regression}

        accepted: list[tuple[str, str]] = []
        demoted: list[tuple[str, str]] = []
        for raw in args.blocking:
            key = normalize_key(raw)
            known = key in state["components"]
            if scope == FULL or known or key in regressions or exempt:
                accepted.append((key, raw))
            else:
                demoted.append((key, raw))

        accepted_keys = {key for key, _ in accepted}
        for key, raw in accepted:
            component = _component(state, key, raw, round_no)
            component["strikes"] += 1
            component["status"] = "open"
            component["display"] = raw.strip()
            component["last_round"] = round_no
            component["rounds"].append(round_no)
            if key in finding_details:
                component["finding"] = finding_details[key]
            if args.gate not in component["gates"]:
                component["gates"].append(args.gate)

        # A completed re-run of the same gate that no longer reports an open
        # component is the evidence that it was fixed. This is what shrinks the
        # blocking set round over round.
        resolved: list[str] = []
        for key, component in state["components"].items():
            if (
                component["status"] == "open"
                and args.gate in component["gates"]
                and key not in accepted_keys
            ):
                component["status"] = "resolved"
                component["resolved_round"] = round_no
                component["resolved_by_gate"] = args.gate
                resolved.append(key)

        advisories = [
            {
                "key": normalize_key(raw), "display": raw.strip(),
                "reason": "reported-advisory",
                **({"finding": finding_details[normalize_key(raw)]} if normalize_key(raw) in finding_details else {}),
            }
            for raw in args.advisory
        ] + [
            {
                "key": key, "display": raw.strip(),
                "reason": "out-of-scope-in-frozen-round",
                **({"finding": finding_details[key]} if key in finding_details else {}),
            }
            for key, raw in demoted
        ]
        state.setdefault("advisories", []).extend(
            {**item, "round": round_no, "gate": args.gate} for item in advisories
        )

        effective = "FAIL" if accepted else "PASS"

        state["rounds"].append(
            {
                "round": round_no,
                "gate": args.gate,
                "scope_mode": scope,
                "claimed_verdict": args.verdict,
                "effective_verdict": effective,
                "recorded_at": now(),
                "blocking": sorted(accepted_keys),
                "advisory": sorted({item["key"] for item in advisories}),
                "resolved": sorted(resolved),
            }
        )
        if state.get("repair_pending_review") and state.get("repair_attempts"):
            attempt = state["repair_attempts"][-1]
            if not args.head:
                raise LedgerError("recording a repaired-head review requires --head")
            if args.head.lower() != attempt["head"]:
                raise LedgerError(
                    f"review head {args.head.lower()} does not match repaired head {attempt['head']}"
                )
            attempt.setdefault("reviewed_gates", [])
            if args.gate not in attempt["reviewed_gates"]:
                attempt["reviewed_gates"].append(args.gate)
        save(path, state)

        result = {
            "ledger": str(path),
            "round": round_no,
            "gate": args.gate,
            "scope_mode": scope,
            "claimed_verdict": args.verdict,
            "effective_verdict": effective,
            "accepted_blocking": sorted(accepted_keys),
            "demoted_to_advisory": sorted(key for key, _ in demoted),
            "resolved_this_round": sorted(resolved),
            **decide(state),
        }
        emit(result)


def _load_repair_report(path: str) -> dict[str, Any]:
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot read repair report: {exc}") from exc
    if set(report) != {"schema_version", "head", "findings"} or report.get("schema_version") != 1:
        raise LedgerError("repair report requires exactly schema_version=1, head, and findings")
    if not isinstance(report["head"], str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", report["head"]):
        raise LedgerError("repair report head must be a 7-64 character hexadecimal commit id")
    if not isinstance(report["findings"], list) or not report["findings"]:
        raise LedgerError("repair report findings must be a non-empty array")
    required = {"component", "status", "root_cause", "change", "verification"}
    for item in report["findings"]:
        if not isinstance(item, dict) or set(item) != required:
            raise LedgerError(f"each repair finding requires exactly: {', '.join(sorted(required))}")
        if item["status"] not in {"closed", "unresolved"}:
            raise LedgerError("repair finding status must be closed or unresolved")
        for key in ("component", "root_cause", "change", "verification"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise LedgerError(f"repair finding {key} must be non-empty text")
    return report


def cmd_repair_brief(args: argparse.Namespace) -> None:
    state = load(ledger_path(args))
    plan = decide(state)
    if not plan["open_blocking"]:
        raise LedgerError("cannot build a repair brief without open blocking findings")
    lines = [
        f"REPAIR ATTEMPT {plan['fix_cycles'] + 1} OF {plan['max_rounds']} FOR PR {state['pr']}",
        "",
        "Treat every component below as a stable finding ID. Before editing, map each ID",
        "to its root cause, planned change, affected boundaries, verification, and objective",
        "closure condition. After editing, write one repair-report JSON object and record it",
        "with review-ledger.py record-repair before any reviewer is launched.",
        "",
        "OPEN BLOCKING FINDINGS:",
    ]
    for component in sorted(open_components(state), key=lambda item: item["key"]):
        lines.append(f"- [component: {component['key']}]")
        finding = component.get("finding")
        if finding:
            lines.append(f"  {finding['title']}: {finding['explanation']}")
        lines.append("  Closure must be demonstrated by a named regression test or equivalent evidence.")
    lines += [
        "",
        "The report schema is:",
        '{"schema_version":1,"head":"<commit>","findings":[{"component":"<stable ID>",',
        '"status":"closed|unresolved","root_cause":"...","change":"...","verification":"..."}]}',
        "Every open ID must appear exactly once. Reviewer re-execution, not the implementer's",
        "self-assessment, determines whether a finding is actually closed.",
    ]
    print("\n".join(lines))


def cmd_record_repair(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    report = _load_repair_report(args.report)
    with locked(path):
        state = load(path)
        plan = decide(state)
        if state.get("repair_pending_review"):
            raise LedgerError("the previous repair is still waiting for its complete review set")
        if plan["next_action"] == ACTION_ESCALATE or plan["fix_cycles"] >= plan["max_rounds"]:
            raise LedgerError("the repair budget is spent; render handoff instead of starting another repair")
        open_keys = set(plan["open_blocking"])
        report_keys = [normalize_key(item["component"]) for item in report["findings"]]
        if len(report_keys) != len(set(report_keys)):
            raise LedgerError("repair report contains duplicate component IDs")
        if set(report_keys) != open_keys:
            missing = sorted(open_keys - set(report_keys))
            extra = sorted(set(report_keys) - open_keys)
            raise LedgerError(f"repair report must cover the exact open set; missing={missing}, extra={extra}")
        gate_verdicts = decide(state)["gate_verdicts"]
        attempt = {
            "attempt": len(state.setdefault("repair_attempts", [])) + 1,
            "recorded_at": now(),
            "head": report["head"].lower(),
            "findings": [
                {**item, "component": normalize_key(item["component"])}
                for item in report["findings"]
            ],
            "open_before": sorted(open_keys),
            "required_gates": sorted(gate_verdicts) or ["code-review"],
            "reviewed_gates": [],
        }
        state["repair_attempts"].append(attempt)
        state["repair_pending_review"] = True
        save(path, state)
        emit({"repair_recorded": attempt["attempt"], "head": attempt["head"], **decide(state)})


def cmd_complete_repair_review(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        if not state.get("repair_pending_review") or not state.get("repair_attempts"):
            raise LedgerError("no repaired head is awaiting review completion")
        attempt = state["repair_attempts"][-1]
        missing = sorted(set(attempt["required_gates"]) - set(attempt["reviewed_gates"]))
        if missing:
            raise LedgerError(f"repair review is incomplete; missing gates: {', '.join(missing)}")
        remaining = {item["key"] for item in open_components(state)}
        attempt["open_after"] = sorted(remaining)
        attempt["closed"] = sorted(set(attempt["open_before"]) - remaining)
        attempt["completed_at"] = now()
        for key in remaining.intersection(attempt["open_before"]):
            state["components"][key]["repair_failures"] = (
                state["components"][key].get("repair_failures", 0) + 1
            )
        state["repair_pending_review"] = False
        save(path, state)
        emit({"repair_review_completed": attempt["attempt"], **decide(state)})


def cmd_metrics(args: argparse.Namespace) -> None:
    state = load(ledger_path(args))
    attempts = state.get("repair_attempts", [])
    total_initial = len(attempts[0]["open_before"]) if attempts else 0
    first_closed = len(attempts[0].get("closed", [])) if attempts else 0
    all_closed = len(set().union(*(set(item.get("closed", [])) for item in attempts))) if attempts else 0
    def elapsed(start: str | None, end: str | None) -> float | None:
        if not start or not end:
            return None
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3)

    repair_durations = [
        elapsed(item.get("recorded_at"), item.get("completed_at")) for item in attempts
    ]
    design_rounds = state.get("design", {}).get("rounds", [])
    emit({
        "pr": state["pr"],
        "review_passes": len(state["rounds"]),
        "repair_attempts": len(attempts),
        "initial_blocking_findings": total_initial,
        "first_repair_closure_rate": (first_closed / total_initial) if total_initial else None,
        "cumulative_repair_closure_rate": (all_closed / total_initial) if total_initial else None,
        "no_op_repairs": sum(1 for item in attempts if not item.get("closed")),
        "repair_review_seconds": repair_durations,
        "design_rounds": len(design_rounds),
        "design_elapsed_seconds": elapsed(
            design_rounds[0].get("recorded_at") if design_rounds else None,
            design_rounds[-1].get("recorded_at") if design_rounds else None,
        ),
        "review_elapsed_seconds": elapsed(
            state["rounds"][0].get("recorded_at") if state["rounds"] else None,
            state["rounds"][-1].get("recorded_at") if state["rounds"] else None,
        ),
        "new_blocking_after_round_one": sum(
            1
            for entry in state["rounds"]
            if entry["scope_mode"] == FROZEN
            for key in entry["blocking"]
            if state["components"].get(key, {}).get("first_round") == entry["round"]
        ),
        "open_blocking": decide(state)["open_blocking"],
    })


def _design_state(state: dict[str, Any], max_rounds: int | None = None) -> dict[str, Any]:
    design = state.setdefault(
        "design",
        {"max_rounds": max_rounds or DEFAULT_MAX_DESIGN_ROUNDS, "rounds": [], "escalated": False},
    )
    if max_rounds is not None:
        design["max_rounds"] = max_rounds
    return design


def _design_plan(state: dict[str, Any]) -> dict[str, Any]:
    design = _design_state(state)
    rounds = design["rounds"]
    if rounds and rounds[-1]["verdict"] == "PASS":
        action = "implement"
    elif design.get("escalated") or len(rounds) >= design["max_rounds"]:
        action = ACTION_ESCALATE
    else:
        action = "redesign"
    return {
        "target": state["pr"],
        "design_rounds": len(rounds),
        "max_design_rounds": design["max_rounds"],
        "design_rounds_remaining": max(0, design["max_rounds"] - len(rounds)),
        "next_action": action,
    }


def cmd_design_open(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    rounds = max_rounds_for(args)
    design_rounds = positive_config_int(
        args, "max_design_rounds", "max_design_rounds", DEFAULT_MAX_DESIGN_ROUNDS
    )
    with locked(path):
        state = load(path) if path.exists() else new_state(args.pr, rounds)
        _design_state(state, design_rounds)
        save(path, state)
        emit({"ledger": str(path), **_design_plan(state)})


def cmd_design_record(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        design = _design_state(state)
        plan = _design_plan(state)
        if plan["next_action"] == ACTION_ESCALATE:
            raise LedgerError("the design-round budget is spent; hand off instead of running another round")
        if plan["next_action"] == "implement":
            raise LedgerError("the design gate already passed")
        design["rounds"].append(
            {
                "round": len(design["rounds"]) + 1,
                "verdict": args.verdict,
                "evidence": args.evidence,
                "recorded_at": now(),
            }
        )
        if args.verdict == "FAIL" and len(design["rounds"]) >= design["max_rounds"]:
            design["escalated"] = True
        save(path, state)
        emit(_design_plan(state))


def cmd_design_handoff(args: argparse.Namespace) -> None:
    state = load(ledger_path(args))
    design = _design_state(state)
    lines = [
        f"# Design gate stopped for {state['pr']}",
        "",
        f"Rounds used: {len(design['rounds'])} of {design['max_rounds']}.",
        "No production implementation is authorized while the last design verdict is FAIL.",
        "",
        "## Round history",
    ]
    for item in design["rounds"]:
        lines.append(f"- Round {item['round']}: {item['verdict']} -- {item['evidence']}")
    print("\n".join(lines))


def cmd_status(args: argparse.Namespace) -> None:
    state = load(ledger_path(args))
    components = {
        key: {
            "strikes": component["strikes"],
            "status": component["status"],
            "gates": component["gates"],
            "rounds": component["rounds"],
            "display": component["display"],
        }
        for key, component in sorted(state["components"].items())
    }
    emit({**decide(state), "components": components})


def cmd_brief(args: argparse.Namespace) -> None:
    """Emit the round-aware review contract to paste into the reviewer's brief."""
    state = load(ledger_path(args))
    plan = decide(state)
    lines = [
        f"REVIEW PASS {plan['next_round']} on PR {state['pr']} "
        f"(scope mode: {plan['next_scope_mode']})",
        f"Fix cycles used: {plan['fix_cycles']} of at most {plan['max_rounds']}. "
        f"When that budget is spent with findings still open, the loop stops for a "
        f"human instead of running another round.",
        "",
    ]
    if plan["next_scope_mode"] == FULL:
        lines += [
            "This is round 1. Sweep the entire diff with full blocking authority.",
            "Every defect class in your brief may block. Be exhaustive now -- findings",
            "you do not raise this round lose blocking authority in later rounds.",
        ]
    else:
        lines += [
            "Round 2+ scope freeze. Still sweep the ENTIRE diff -- a fix can break",
            "something elsewhere -- but only these may block:",
            "  1. Open ledger components listed below (the findings already agreed on).",
            "  2. A regression in the delta since the previous round. Report it with",
            "     the `--regression` flag so it keeps blocking authority.",
            "  3. Any security or data-loss finding, at any time.",
            "Anything newly noticed on code untouched since the last round is ADVISORY:",
            "report it for the PR body, but it does not FAIL this gate.",
        ]
    doubt = (
        "When unsure whether something is a real defect, treat it as BLOCKING."
        if plan["uncertainty_rule"] == "block-on-doubt"
        else (
            f"This is fix cycle {plan['fix_cycles'] + 1}. A false FAIL no longer costs one loop -- it costs\n"
            "the next one too. When unsure whether something is a real defect, file it as\n"
            "ADVISORY and name the exact evidence that would settle it."
        )
    )
    lines += ["", doubt, ""]

    open_list = open_components(state)
    if open_list:
        lines.append("OPEN LEDGER COMPONENTS (reuse these exact keys):")
        for component in sorted(open_list, key=lambda c: c["key"]):
            mark = "  [REDESIGN REQUIRED]" if component in redesign_pending(state) else ""
            lines.append(
                f"  - [component: {component['key']}] strikes={component['strikes']}"
                f" gates={','.join(component['gates'])}{mark}"
            )
    else:
        lines.append("OPEN LEDGER COMPONENTS: none.")
    lines += [
        "",
        "Key every finding as `[component: <path>:<symbol>]` -- the file path plus the",
        "enclosing symbol, never a line number and never a free-text subsystem name.",
        "If your finding is the same defect as an open component above, reuse its key",
        "verbatim so the strike lands on it.",
    ]
    print("\n".join(lines))


def cmd_handoff(args: argparse.Namespace) -> None:
    """Render the human escalation report when the loop did not converge."""
    state = load(ledger_path(args))
    plan = decide(state)
    lines = [
        f"# Review loop stopped for PR {state['pr']}",
        "",
        f"Fix cycles used: {plan['fix_cycles']} of {plan['max_rounds']} "
        f"across {plan['rounds_recorded']} review pass(es). "
        f"Next action: {plan['next_action']}.",
        "",
        "This PR was NOT merged and NOT abandoned. The loop hit its configured round",
        "cap with blocking findings still open, so it stopped for a human decision",
        "rather than looping further.",
        "",
        "## Still blocking",
    ]
    open_list = sorted(open_components(state), key=lambda c: -c["strikes"])
    if open_list:
        for component in open_list:
            lines.append(
                f"- `{component['key']}` -- {component['strikes']} strike(s), "
                f"rounds {component['rounds']}, gates {', '.join(component['gates'])}"
            )
            if component.get("finding"):
                finding = component["finding"]
                lines.append(f"  {finding['title']}: {finding['explanation']}")
    else:
        lines.append("- none")
    lines += ["", "## Round history"]
    for entry in state["rounds"]:
        lines.append(
            f"- Round {entry['round']} ({entry['gate']}, {entry['scope_mode']}): "
            f"{entry['effective_verdict']}"
            + (f" -- resolved {', '.join(entry['resolved'])}" if entry["resolved"] else "")
        )
    attempts = state.get("repair_attempts", [])
    if attempts:
        lines += ["", "## Repair history"]
        for attempt in attempts:
            lines.append(
                f"- Attempt {attempt['attempt']} at `{attempt['head']}`: "
                f"closed {', '.join(attempt.get('closed', [])) or 'none'}; "
                f"still open {', '.join(attempt.get('open_after', attempt['open_before'])) or 'none'}"
            )
            for item in attempt["findings"]:
                lines.append(
                    f"  - `{item['component']}`: root cause={item['root_cause']}; "
                    f"change={item['change']}; verification={item['verification']}"
                )
    advisories = state.get("advisories", [])
    if advisories:
        lines += ["", "## Advisory (non-blocking, for follow-up)"]
        for item in advisories:
            lines.append(f"- `{item['key']}` ({item['reason']})")
            if item.get("finding"):
                finding = item["finding"]
                lines.append(f"  {finding['title']}: {finding['explanation']}")
    lines += [
        "",
        "## Options",
        "- Fix the remaining components yourself and re-open the ledger with a raised cap.",
        "- Accept the advisories as follow-up tickets and merge if the blocking set is",
        "  actually empty.",
        "- Return the ticket to scoping: repeated strikes on one component usually mean",
        "  the acceptance criteria, not the code, are underspecified.",
    ]
    print("\n".join(lines))


def cmd_resolve(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        key = normalize_key(args.key)
        component = state["components"].get(key)
        if component is None:
            raise LedgerError(f"no such component on the ledger: {key}")
        component["status"] = "resolved"
        component["resolved_round"] = len(state["rounds"])
        component["resolved_by_gate"] = "manual"
        save(path, state)
        emit({"resolved": key, **decide(state)})


def cmd_redesign(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        key = normalize_key(args.key)
        component = state["components"].get(key)
        if component is None:
            raise LedgerError(f"no such component on the ledger: {key}")
        if args.verdict != "PASS":
            raise LedgerError(
                f"design gate returned {args.verdict} for {key}; "
                "redesign again before authorizing any implementation"
            )
        component["redesigned_at_strike"] = component["strikes"]
        component["redesigned_at_repair_failure"] = component.get("repair_failures", 0)
        component["last_redesign_round"] = len(state["rounds"])
        save(path, state)
        emit({"redesign_cleared": key, **decide(state)})


def cmd_alias(args: argparse.Namespace) -> None:
    """Merge a duplicate key into the canonical one so strikes accumulate."""
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        source = normalize_key(args.source)
        target = normalize_key(args.target)
        if source == target:
            raise LedgerError("alias source and target normalize to the same key")
        if source not in state["components"]:
            raise LedgerError(f"no such component on the ledger: {source}")
        merged = state["components"].pop(source)
        canonical = _component(state, target, args.target, merged["first_round"])
        canonical["strikes"] += merged["strikes"]
        canonical["rounds"] = sorted(set(canonical["rounds"] + merged["rounds"]))
        canonical["gates"] = sorted(set(canonical["gates"] + merged["gates"]))
        canonical["first_round"] = min(canonical["first_round"], merged["first_round"])
        if merged["status"] == "open":
            canonical["status"] = "open"
        state.setdefault("aliases", {})[source] = target
        save(path, state)
        emit({"aliased": {source: target}, "strikes": canonical["strikes"], **decide(state)})


def cmd_escalate(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        state["escalated"] = True
        state["escalation_reason"] = args.reason
        save(path, state)
        emit({"escalated": True, "reason": args.reason, **decide(state)})


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", help="repo orchestration config (default: .orchestration/config.yaml)")
    result.add_argument("--ledger-dir", help="ledger directory override")
    commands = result.add_subparsers(dest="command", required=True)

    open_parser = commands.add_parser("open", help="create or report the ledger for a PR")
    open_parser.add_argument("pr")
    open_parser.add_argument("--max-rounds", help="override the configured round cap")
    open_parser.add_argument("--max-design-rounds", help="override the configured design-round cap")
    open_parser.set_defaults(func=cmd_open)

    record_parser = commands.add_parser("record", help="record one completed gate round")
    record_parser.add_argument("pr")
    record_parser.add_argument("--gate", required=True)
    record_parser.add_argument("--result", help="validated structured reviewer JSON file")
    record_parser.add_argument("--verdict", choices=("PASS", "FAIL"))
    record_parser.add_argument(
        "--blocking", action="append", default=[], metavar="COMPONENT",
        help="a blocking finding's component key (repeatable)",
    )
    record_parser.add_argument(
        "--advisory", action="append", default=[], metavar="COMPONENT",
        help="a non-blocking finding's component key (repeatable)",
    )
    record_parser.add_argument(
        "--regression", action="append", default=[], metavar="COMPONENT",
        help="a new key that is a regression in the delta, so it keeps blocking authority",
    )
    record_parser.add_argument("--head", help="exact reviewed commit; required after record-repair")
    record_parser.set_defaults(func=cmd_record)

    for name, func, helptext in (
        ("status", cmd_status, "emit the ledger state and the loop's next action"),
        ("brief", cmd_brief, "emit the round-aware contract for the next reviewer"),
        ("handoff", cmd_handoff, "render the human escalation report"),
        ("repair-brief", cmd_repair_brief, "emit one deduplicated repair contract"),
        ("metrics", cmd_metrics, "emit repair effectiveness metrics"),
        ("complete-repair-review", cmd_complete_repair_review, "close a repaired head after every required gate records"),
        ("design-handoff", cmd_design_handoff, "render the design-round escalation report"),
    ):
        command = commands.add_parser(name, help=helptext)
        command.add_argument("pr")
        command.set_defaults(func=func)

    repair_parser = commands.add_parser("record-repair", help="record one complete repair report before re-review")
    repair_parser.add_argument("pr")
    repair_parser.add_argument("--report", required=True)
    repair_parser.set_defaults(func=cmd_record_repair)

    design_open = commands.add_parser("design-open", help="create or report the pre-code design ledger")
    design_open.add_argument("pr", help="ticket or change identifier")
    design_open.add_argument("--max-rounds")
    design_open.add_argument("--max-design-rounds")
    design_open.set_defaults(func=cmd_design_open)

    design_record = commands.add_parser("design-record", help="record one pre-code design verdict")
    design_record.add_argument("pr", help="ticket or change identifier")
    design_record.add_argument("--verdict", required=True, choices=("PASS", "FAIL"))
    design_record.add_argument("--evidence", required=True)
    design_record.set_defaults(func=cmd_design_record)

    resolve_parser = commands.add_parser("resolve", help="manually close a component")
    resolve_parser.add_argument("pr")
    resolve_parser.add_argument("--key", required=True)
    resolve_parser.set_defaults(func=cmd_resolve)

    redesign_parser = commands.add_parser("redesign", help="record a design-gate verdict for a component")
    redesign_parser.add_argument("pr")
    redesign_parser.add_argument("--key", required=True)
    redesign_parser.add_argument("--verdict", required=True, choices=("PASS", "FAIL"))
    redesign_parser.set_defaults(func=cmd_redesign)

    alias_parser = commands.add_parser("alias", help="merge a duplicate component key into the canonical one")
    alias_parser.add_argument("pr")
    alias_parser.add_argument("--from", dest="source", required=True)
    alias_parser.add_argument("--to", dest="target", required=True)
    alias_parser.set_defaults(func=cmd_alias)

    escalate_parser = commands.add_parser("escalate", help="stop the loop and hand the PR to a human")
    escalate_parser.add_argument("pr")
    escalate_parser.add_argument("--reason", required=True)
    escalate_parser.set_defaults(func=cmd_escalate)

    return result


def main() -> int:
    args = parser().parse_args()
    try:
        args.func(args)
        return 0
    except (LedgerError, context_pipeline.ContextError) as exc:
        print(f"review-ledger: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
