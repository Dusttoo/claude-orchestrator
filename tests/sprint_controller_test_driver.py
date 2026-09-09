#!/usr/bin/env python3
"""Test-only in-process entry point for sprint-controller fixture evidence."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "sprint_controller", ROOT / "scripts/sprint-controller.py"
)
assert SPEC and SPEC.loader
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)

raise SystemExit(controller.main_for_test(sys.argv[1:]))
