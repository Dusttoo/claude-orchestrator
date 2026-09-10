#!/usr/bin/env python3
"""Bridge controller decisions to the separately owned host authority."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_HELPER = Path("/usr/local/libexec/orchestration-recovery-authority")


class AuthorityError(RuntimeError):
    pass


def _scope(kind: str, repository: Path, ticket: str, attempt: int | None = None) -> str:
    value: dict[str, Any] = {
        "kind": kind,
        "repository": str(repository.resolve()),
        "ticket": ticket,
    }
    if attempt is not None:
        value["attempt"] = attempt
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _helper() -> tuple[Path, bool]:
    test_helper = os.environ.get("ORCHESTRATION_TEST_AUTHORITY_HELPER")
    if os.environ.get("ORCHESTRATION_TEST_MODE") == "1" and test_helper:
        helper = Path(test_helper).resolve()
        if not helper.is_file():
            raise AuthorityError("test authority helper is missing")
        return helper, True

    helper = DEFAULT_HELPER
    try:
        info = helper.lstat()
    except FileNotFoundError:
        raise AuthorityError("host operator authority is not installed") from None
    except OSError as exc:
        raise AuthorityError(f"cannot inspect host operator authority: {exc}") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & 0o022
    ):
        raise AuthorityError(
            "host operator authority must be a root-owned, non-writable regular file"
        )
    return helper, False


def _call(
    command: str,
    scope: str,
    *,
    token: str = "",
    no_authority_ok: bool = False,
) -> str | None:
    try:
        helper, test_mode = _helper()
    except AuthorityError as exc:
        if no_authority_ok and "not installed" in str(exc):
            return None
        raise
    argv = [str(helper), command, "--scope", scope]
    if not test_mode:
        argv = ["sudo", "-n", *argv]
    try:
        result = subprocess.run(
            argv,
            input=(token + "\n") if token else None,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthorityError(f"host operator authority failed: {exc}") from exc
    if result.returncode == 3 and command in {"budget-ceiling", "relaunch-ceiling"}:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or "request denied"
        raise AuthorityError(f"host operator authority denied {command}: {detail}")
    return result.stdout.strip()


def budget_ceiling(repository: Path, ticket: str) -> Decimal | None:
    raw = _call(
        "budget-ceiling",
        _scope("budget", repository, ticket),
        no_authority_ok=True,
    )
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise AuthorityError("host authority returned an invalid budget ceiling") from exc
    if not value.is_finite() or value <= 0:
        raise AuthorityError("host authority returned a non-positive budget ceiling")
    return value


def activate_budget(repository: Path, ticket: str, token: str) -> Decimal:
    if not token.strip():
        raise AuthorityError("budget capability must not be empty")
    raw = _call(
        "activate-budget",
        _scope("budget", repository, ticket),
        token=token.strip(),
    )
    try:
        value = Decimal(raw or "")
    except InvalidOperation as exc:
        raise AuthorityError("host authority returned an invalid budget ceiling") from exc
    if not value.is_finite() or value <= 0:
        raise AuthorityError("host authority returned a non-positive budget ceiling")
    return value


def relaunch_ceiling(repository: Path, ticket: str) -> int | None:
    raw = _call(
        "relaunch-ceiling",
        _scope("relaunch", repository, ticket),
        no_authority_ok=True,
    )
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise AuthorityError("host authority returned an invalid attempt ceiling") from exc
    if value <= 0:
        raise AuthorityError("host authority returned a non-positive attempt ceiling")
    return value


def activate_relaunch(repository: Path, ticket: str, token: str) -> int:
    if not token.strip():
        raise AuthorityError("relaunch capability must not be empty")
    raw = _call(
        "activate-relaunch",
        _scope("relaunch", repository, ticket),
        token=token.strip(),
    )
    try:
        value = int(raw or "")
    except ValueError as exc:
        raise AuthorityError("host authority returned an invalid attempt ceiling") from exc
    if value <= 0:
        raise AuthorityError("host authority returned a non-positive attempt ceiling")
    return value


def consume_recovery(
    repository: Path, ticket: str, attempt: int, token: str
) -> None:
    if not token.strip():
        raise AuthorityError("recovery capability must not be empty")
    _call(
        "consume-recovery",
        _scope("recovery", repository, ticket, attempt),
        token=token.strip(),
    )
