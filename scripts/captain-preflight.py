#!/usr/bin/env python3
"""Fail-closed provenance check before an interactive sprint captain starts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


REQUIRED = (
    "scripts/sprint-controller.py",
    "scripts/orchestration-engine.py",
    "scripts/api_agent.py",
    "skills/orchestrate-sprint/SKILL.md",
    "skills/orchestrate-ticket/SKILL.md",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", required=True)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--host", choices=("claude", "codex"), required=True)
    args = parser.parse_args()
    plugin = Path(args.plugin_root).expanduser().resolve()
    repo = Path(args.repo).expanduser().resolve()
    manifest = plugin / ".claude-plugin/plugin.json"
    failures = [str(plugin / relative) for relative in REQUIRED if not (plugin / relative).is_file()]
    if not manifest.is_file():
        failures.append(str(manifest))
        version = "unknown"
    else:
        version = str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "unknown")
    config = repo / ".orchestration/config.yaml"
    if not config.is_file():
        failures.append(str(config))
    if failures:
        print(json.dumps({"status": "blocked", "missing": failures}, indent=2))
        return 2
    digest = hashlib.sha256()
    for relative in REQUIRED:
        digest.update(relative.encode())
        digest.update((plugin / relative).read_bytes())
    print(json.dumps({
        "status": "ready",
        "captain_mode": "controller-only",
        "host": args.host,
        "plugin_root": str(plugin),
        "plugin_version": version,
        "runtime_fingerprint": digest.hexdigest(),
        "rules": [
            "do not implement sprint tickets in the captain context",
            "do not invent, approximate, or bypass missing plugin skills",
            "use sprint-controller for every reservation and terminal transition",
            "launch only the exact orchestrate-ticket skill from this plugin root",
        ],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
