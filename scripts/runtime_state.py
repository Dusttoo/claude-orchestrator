#!/usr/bin/env python3
"""Resolve worktree-local configuration and repository-wide runtime state."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


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
    override = os.environ.get("ORCHESTRATION_RUNTIME_ROOT", "").strip()
    if not override:
        # Compatibility with the pre-0.11 usage-ledger-only override.
        override = os.environ.get("ORCHESTRATION_USAGE_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
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


def shared_runtime_path(start: Path, relative: str | Path) -> Path:
    root = shared_repository_root(start)
    requested = Path(relative)
    if requested.is_absolute():
        raise RuntimeStateError("runtime state path must be repository-relative")
    resolved = (root / requested).resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeStateError("runtime state path escapes the shared repository root")
    return resolved
