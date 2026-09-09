#!/usr/bin/env python3
"""Root-owned, file-backed authority for recovery and budget capabilities.

Runtime commands are intended to be exposed through a narrow sudoers rule.
Issuance and revocation commands require a real root invocation and are never
included in that rule.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_STATE = Path("/var/lib/claude-orchestrator-authority")


def test_mode() -> bool:
    # sudo sets the real uid to root. A production/root invocation must never
    # honor caller-controlled paths, even on a host with unusual env_keep rules.
    return os.getuid() != 0 and os.environ.get("ORCHESTRATION_AUTHORITY_TEST_MODE") == "1"


def state_root() -> Path:
    if test_mode():
        override = os.environ.get("ORCHESTRATION_AUTHORITY_STATE_DIR")
        if override:
            # Keep the final path component unresolved so ensure_layout can
            # reject a symlink instead of silently following it.
            return Path(os.path.abspath(override))
    return DEFAULT_STATE


def fail(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def canonical_scope(raw: str, expected_kind: str) -> str:
    if not raw or len(raw) > 4096 or "\n" in raw or "\r" in raw:
        raise ValueError("invalid authority scope")
    value = json.loads(raw)
    expected = {"kind", "repository", "ticket"}
    if expected_kind == "recovery":
        expected.add("attempt")
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("authority scope has unexpected fields")
    if value.get("kind") != expected_kind:
        raise ValueError("authority scope kind mismatch")
    repository = str(value.get("repository") or "")
    ticket = str(value.get("ticket") or "")
    if (
        not repository.startswith("/")
        or not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", ticket)
        or len(ticket) > 64
    ):
        raise ValueError("authority scope is incomplete")
    if expected_kind == "recovery":
        attempt = value.get("attempt")
        if not isinstance(attempt, int) or attempt < 0:
            raise ValueError("recovery attempt must be a non-negative integer")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if not hmac.compare_digest(canonical, raw):
        raise ValueError("authority scope must use canonical JSON")
    return canonical


def build_scope(kind: str, repository: str, ticket: str, attempt: int | None) -> str:
    root = Path(repository).resolve()
    if not root.is_dir():
        raise ValueError("repository does not exist")
    value: dict[str, Any] = {
        "kind": kind,
        "repository": str(root),
        "ticket": ticket.strip().upper(),
    }
    if kind == "recovery":
        if attempt is None or attempt < 0:
            raise ValueError("recovery attempt is required")
        value["attempt"] = attempt
    return canonical_scope(
        json.dumps(value, sort_keys=True, separators=(",", ":")), kind
    )


def validate_directory(path: Path, *, private: bool = True) -> None:
    info = path.lstat()
    expected_uid = os.getuid() if test_mode() else 0
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != expected_uid
        or (private and info.st_mode & 0o077)
    ):
        raise PermissionError(f"authority path is not a private owned directory: {path}")


def ensure_layout(root: Path) -> None:
    if root.is_symlink():
        raise PermissionError("authority state root must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("pending", "active", "consumed"):
        path = root / name
        if path.is_symlink():
            raise PermissionError(f"authority path must not be a symlink: {path}")
        path.mkdir(mode=0o700, exist_ok=True)
    for path in (root, root / "pending", root / "active", root / "consumed"):
        validate_directory(path, private=False)
        os.chmod(path, 0o700)
        validate_directory(path)


def require_layout(root: Path) -> None:
    for path in (root, root / "pending", root / "active", root / "consumed"):
        validate_directory(path)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".authority-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_record(path: Path) -> dict[str, Any]:
    info = path.lstat()
    expected_uid = os.getuid() if test_mode() else 0
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != expected_uid
        or info.st_mode & 0o077
    ):
        raise PermissionError("capability record is not a private owned regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid capability record")
    return value


def require_real_root() -> None:
    if os.getuid() != 0 and not test_mode():
        raise PermissionError("capabilities may only be issued or revoked by root")


def read_token() -> str:
    token = sys.stdin.readline().strip()
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise ValueError("invalid capability token")
    return token


def live(record: dict[str, Any]) -> bool:
    return float(record.get("expires_at", 0)) > time.time()


def record_ceiling(record: dict[str, Any]) -> Decimal:
    try:
        value = Decimal(str(record["ceiling_usd"]))
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("budget capability has an invalid ceiling") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("budget capability has an invalid ceiling")
    return value


def locked(root: Path):
    class Lock:
        def __enter__(self):
            self.handle = (root / ".lock").open("a+")
            os.chmod(root / ".lock", 0o600)
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, *_args):
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()

    return Lock()


def issue(args: argparse.Namespace, kind: str) -> int:
    require_real_root()
    if not 0 < args.expires_hours <= 168:
        raise ValueError("capability expiry must be greater than zero and at most 168 hours")
    scope = build_scope(kind, args.repository, args.ticket, getattr(args, "attempt", None))
    ceiling = None
    if kind == "budget":
        try:
            ceiling = Decimal(args.ceiling_usd)
        except InvalidOperation as exc:
            raise ValueError("ceiling must be a decimal number") from exc
        if not ceiling.is_finite() or ceiling <= 0:
            raise ValueError("ceiling must be positive")
    token = secrets.token_hex(32)
    root = state_root()
    ensure_layout(root)
    record = {
        "kind": kind,
        "scope": scope,
        "issued_at": time.time(),
        "expires_at": time.time() + args.expires_hours * 3600,
    }
    if ceiling is not None:
        record["ceiling_usd"] = str(ceiling)
    with locked(root):
        atomic_json(root / "pending" / f"{token}.json", record)
    print(token)
    return 0


def consume_recovery(args: argparse.Namespace) -> int:
    scope = canonical_scope(args.scope, "recovery")
    token = read_token()
    root = state_root()
    require_layout(root)
    source = root / "pending" / f"{token}.json"
    with locked(root):
        if not source.is_file():
            return fail("capability is missing or already consumed")
        record = load_record(source)
        if record.get("kind") != "recovery" or not live(record):
            return fail("recovery capability is invalid or expired")
        if not hmac.compare_digest(str(record.get("scope") or ""), scope):
            return fail("recovery capability scope mismatch")
        os.replace(source, root / "consumed" / f"{token}.json")
    return 0


def activate_budget(args: argparse.Namespace) -> int:
    scope = canonical_scope(args.scope, "budget")
    token = read_token()
    root = state_root()
    require_layout(root)
    source = root / "pending" / f"{token}.json"
    key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    target = root / "active" / f"{key}.json"
    with locked(root):
        if not source.is_file():
            return fail("capability is missing or already consumed")
        record = load_record(source)
        if record.get("kind") != "budget" or not live(record):
            return fail("budget capability is invalid or expired")
        if not hmac.compare_digest(str(record.get("scope") or ""), scope):
            return fail("budget capability scope mismatch")
        if target.is_file():
            current = load_record(target)
            if live(current) and record_ceiling(current) > record_ceiling(record):
                record = current
        record_ceiling(record)
        atomic_json(target, record)
        os.replace(source, root / "consumed" / f"{token}.json")
    print(record["ceiling_usd"])
    return 0


def budget_ceiling(args: argparse.Namespace) -> int:
    scope = canonical_scope(args.scope, "budget")
    root = state_root()
    require_layout(root)
    key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    target = root / "active" / f"{key}.json"
    with locked(root):
        if not target.is_file():
            return 3
        record = load_record(target)
        if (
            record.get("kind") != "budget"
            or not live(record)
            or not hmac.compare_digest(str(record.get("scope") or ""), scope)
        ):
            return 3
        record_ceiling(record)
    print(record["ceiling_usd"])
    return 0


def revoke_budget(args: argparse.Namespace) -> int:
    require_real_root()
    scope = build_scope("budget", args.repository, args.ticket, None)
    root = state_root()
    ensure_layout(root)
    key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    with locked(root):
        (root / "active" / f"{key}.json").unlink(missing_ok=True)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("consume-recovery", "activate-budget", "budget-ceiling"):
        command = commands.add_parser(name)
        command.add_argument("--scope", required=True)
    recovery = commands.add_parser("issue-recovery")
    recovery.add_argument("--repository", required=True)
    recovery.add_argument("--ticket", required=True)
    recovery.add_argument("--attempt", required=True, type=int)
    recovery.add_argument("--expires-hours", type=float, default=24)
    budget = commands.add_parser("issue-budget")
    budget.add_argument("--repository", required=True)
    budget.add_argument("--ticket", required=True)
    budget.add_argument("--ceiling-usd", required=True)
    budget.add_argument("--expires-hours", type=float, default=24)
    revoke = commands.add_parser("revoke-budget")
    revoke.add_argument("--repository", required=True)
    revoke.add_argument("--ticket", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "issue-recovery":
            return issue(args, "recovery")
        if args.command == "issue-budget":
            return issue(args, "budget")
        if args.command == "consume-recovery":
            return consume_recovery(args)
        if args.command == "activate-budget":
            return activate_budget(args)
        if args.command == "budget-ceiling":
            return budget_ceiling(args)
        if args.command == "revoke-budget":
            return revoke_budget(args)
    except (OSError, ValueError, PermissionError, json.JSONDecodeError) as exc:
        return fail(str(exc))
    return fail("unsupported command")


if __name__ == "__main__":
    raise SystemExit(main())
