#!/usr/bin/env python3
"""Test-only in-process entry point for Jira fixture transport injection."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "jira_inventory_fetch", ROOT / "scripts/jira_inventory_fetch.py"
)
assert SPEC and SPEC.loader
jira = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jira)

transport = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
config = Path(sys.argv[2])
raise SystemExit(jira.run_fixture_adapter(sys.argv[3:], transport, config))
