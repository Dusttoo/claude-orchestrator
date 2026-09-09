#!/usr/bin/env python3
"""Single-use phase permits backed by the durable review ledger."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


class ReviewPermitError(RuntimeError):
    pass


def safe_pr(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-")
    if not result:
        raise ReviewPermitError("review permit requires a valid PR or design-ledger id")
    return result


def ledger_path(shared_root: Path, ledger_dir: str, pr: str) -> Path:
    relative = Path(ledger_dir)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReviewPermitError("review ledger directory must stay in the shared repository")
    return shared_root / relative / f"pr-{safe_pr(pr)}.json"


def consume(
    *, shared_root: Path, ledger_dir: str, pr: str, token: str,
    ticket: str, role: str, head: str, timestamp: str,
) -> None:
    path = ledger_path(shared_root, ledger_dir, pr)
    lock_path = path.with_suffix(path.suffix + ".lock")
    if not path.is_file():
        raise ReviewPermitError("review phase permit ledger does not exist")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = json.loads(path.read_text(encoding="utf-8"))
        permit = next((item for item in state.get("review_permits", []) if item.get("token") == token), None)
        if not permit or permit.get("started_at") or permit.get("completion_receipt"):
            raise ReviewPermitError("review phase permit is missing or already started")
        expected = {"ticket": ticket, "role": role, "head": head.lower()}
        if any(permit.get(key) != value for key, value in expected.items()):
            raise ReviewPermitError("review phase permit does not match ticket, role, and exact head")
        current_phase = {
            "round_count": len(state.get("rounds", [])),
            "repair_count": len(state.get("repair_attempts", [])),
            "design_round_count": len((state.get("design") or {}).get("rounds", [])),
        }
        if any(permit.get(key) != value for key, value in current_phase.items()):
            raise ReviewPermitError("review phase changed after this permit was issued")
        permit["started_at"] = timestamp
        _save(path, state)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _save(path: Path, state: dict[str, Any]) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def complete(
    *, shared_root: Path, ledger_dir: str, pr: str, token: str,
    ticket: str, role: str, head: str, result: Any, timestamp: str,
    desktop: bool = False,
) -> str:
    """Create a digest-bound completion receipt after successful review output.

    API reviewers must have started their permit first. A desktop reviewer uses
    this controller-owned atomic transition to start and complete the same
    single permit after its structured result exists.
    """
    path = ledger_path(shared_root, ledger_dir, pr)
    lock_path = path.with_suffix(path.suffix + ".lock")
    digest = canonical_digest(result)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = json.loads(path.read_text(encoding="utf-8"))
        permits = state.get("review_permits", [])
        permit = next((item for item in permits if item.get("token") == token), None)
        if not permit or permit.get("completion_receipt"):
            raise ReviewPermitError("review phase permit is missing or already completed")
        expected = {"ticket": ticket, "role": role, "head": head.lower()}
        if any(permit.get(key) != value for key, value in expected.items()):
            raise ReviewPermitError("review phase permit does not match ticket, role, and exact head")
        current_phase = {
            "round_count": len(state.get("rounds", [])),
            "repair_count": len(state.get("repair_attempts", [])),
            "design_round_count": len((state.get("design") or {}).get("rounds", [])),
        }
        if any(permit.get(key) != value for key, value in current_phase.items()):
            raise ReviewPermitError("review phase changed after this permit was issued")
        if not permit.get("started_at"):
            if not desktop:
                raise ReviewPermitError("API review permit was not started by the provider runner")
            permit["started_at"] = timestamp
            permit["execution"] = "desktop"
        receipt = "receipt_" + os.urandom(24).hex()
        permit.update({
            "completed_at": timestamp,
            "completion_receipt": receipt,
            "result_sha256": digest,
            "receipt_consumed_at": "",
        })
        _save(path, state)
    return receipt


def cancel_started(
    *, shared_root: Path, ledger_dir: str, pr: str, token: str,
    ticket: str, role: str, head: str, timestamp: str,
) -> None:
    """Release a started permit only after a known pre-ack rejection."""
    path = ledger_path(shared_root, ledger_dir, pr)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = json.loads(path.read_text(encoding="utf-8"))
        permit = next((item for item in state.get("review_permits", []) if item.get("token") == token), None)
        expected = {"ticket": ticket, "role": role, "head": head.lower()}
        if (
            not permit or any(permit.get(key) != value for key, value in expected.items())
            or not permit.get("started_at") or permit.get("completion_receipt")
        ):
            raise ReviewPermitError("only a started, incomplete matching permit can be cancelled")
        permit["cancelled_at"] = timestamp
        permit["receipt_consumed_at"] = timestamp
        _save(path, state)


def consume_completion(
    state: dict[str, Any], *, token: str, role: str, head: str, result: Any, timestamp: str
) -> bool:
    digest = canonical_digest(result)
    permit = next(
        (
            item for item in state.get("review_permits", [])
            if item.get("token") == token and item.get("role") == role
            and item.get("head") == head.lower()
        ),
        None,
    )
    if (
        not permit or not permit.get("completion_receipt")
        or permit.get("receipt_consumed_at") or permit.get("result_sha256") != digest
    ):
        return False
    permit["receipt_consumed_at"] = timestamp
    return True


def consumed_permit(
    state: dict[str, Any], token: str, *, role: str, head: str
) -> bool:
    return any(
        item.get("token") == token
        and item.get("role") == role
        and item.get("head") == head.lower()
        and item.get("completion_receipt")
        for item in state.get("review_permits", [])
    )
