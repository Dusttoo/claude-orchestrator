#!/usr/bin/env python3
"""Single-use phase permits backed by the durable review ledger."""

from __future__ import annotations

import fcntl
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
        if not permit or permit.get("consumed_at"):
            raise ReviewPermitError("review phase permit is missing or already consumed")
        expected = {"ticket": ticket, "role": role, "head": head.lower()}
        if any(permit.get(key) != value for key, value in expected.items()):
            raise ReviewPermitError("review phase permit does not match ticket, role, and exact head")
        permit["consumed_at"] = timestamp
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


def consumed_permit(
    state: dict[str, Any], token: str, *, role: str, head: str
) -> bool:
    return any(
        item.get("token") == token
        and item.get("role") == role
        and item.get("head") == head.lower()
        and item.get("consumed_at")
        for item in state.get("review_permits", [])
    )
