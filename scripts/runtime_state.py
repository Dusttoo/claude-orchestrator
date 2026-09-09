#!/usr/bin/env python3
"""Resolve worktree-local configuration and repository-wide runtime state."""

from __future__ import annotations

import contextlib
import fcntl
import filecmp
import shutil
import subprocess
from pathlib import Path
from typing import Iterator


class RuntimeStateError(RuntimeError):
    pass


def working_repository_root(start: Path) -> Path:
    start = start.resolve()
    try:
        value = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if value:
            return Path(value).resolve()
    except (OSError, subprocess.CalledProcessError):
        pass
    # A malformed/fake parent `.git` marker must not absorb an unrelated
    # directory. Real repositories (including nested paths) resolve above.
    if (start / ".git").exists():
        return start
    return start


def shared_repository_root(start: Path) -> Path:
    # Runtime enforcement must have one identity. Environment overrides used by
    # pre-0.11 builds let any worker select an empty ledger and reset every cap.
    root = working_repository_root(start)
    try:
        common = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if common:
            return Path(common).resolve().parent
    except (OSError, subprocess.CalledProcessError):
        pass
    return root


@contextlib.contextmanager
def _migration_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".git" / "orchestration-runtime-migration.lock"
    if not lock_path.parent.is_dir():
        lock_path = root / ".orchestration-runtime-migration.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def migrate_legacy_runtime_dir(start: Path, relative: str | Path) -> Path:
    """Copy worktree-local pre-0.11 state into the shared domain, or fail.

    Missing files are preserved. A differing file at the same relative path is
    an ambiguity that requires operator reconciliation; choosing either copy
    would reset or overwrite live enforcement state.
    """
    working = working_repository_root(start)
    shared = shared_repository_root(working)
    requested = Path(relative)
    if requested.is_absolute():
        raise RuntimeStateError("runtime state path must be repository-relative")
    legacy = (working / requested).resolve()
    target = shared_runtime_path(working, requested)
    if legacy == target or not legacy.exists():
        return target
    with _migration_lock(shared):
        if legacy.is_file():
            if target.exists() and not filecmp.cmp(legacy, target, shallow=False):
                raise RuntimeStateError(f"conflicting legacy runtime state: {legacy} and {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(legacy, target)
            return target
        for source in sorted(path for path in legacy.rglob("*") if path.is_file()):
            destination = target / source.relative_to(legacy)
            if destination.exists():
                if not filecmp.cmp(source, destination, shallow=False):
                    raise RuntimeStateError(
                        f"conflicting legacy runtime state: {source} and {destination}"
                    )
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return target


def shared_runtime_path(start: Path, relative: str | Path) -> Path:
    root = shared_repository_root(start)
    requested = Path(relative)
    if requested.is_absolute():
        raise RuntimeStateError("runtime state path must be repository-relative")
    resolved = (root / requested).resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeStateError("runtime state path escapes the shared repository root")
    return resolved


def canonical_config_path(start: Path, requested: str | Path | None = None) -> Path:
    """Return the sole policy file for all worktrees.

    Alternate config paths multiplied enforcement domains. The canonical main
    checkout config is now the only accepted policy source.
    """
    shared = shared_repository_root(start)
    canonical = (shared / ".orchestration/config.yaml").resolve()
    if requested:
        candidate = Path(requested)
        if not candidate.is_absolute():
            candidate = (working_repository_root(start) / candidate).resolve()
        else:
            candidate = candidate.resolve()
        local_default = (working_repository_root(start) / ".orchestration/config.yaml").resolve()
        if candidate not in {canonical, local_default}:
            raise RuntimeStateError("alternate orchestration config paths are not allowed")
    return canonical
