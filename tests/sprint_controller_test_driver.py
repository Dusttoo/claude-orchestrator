#!/usr/bin/env python3
"""Test-only in-process entry point for sprint-controller fixture evidence."""

from __future__ import annotations

import importlib.util
import os
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

os.environ["ORCHESTRATION_TEST_MODE"] = "1"
args = controller.parser().parse_args(sys.argv[1:])
try:
    config = controller.settings(args)
    config["allow_test_evidence"] = True
    # Scheduling fixtures have no real provider; production CLI always enforces health.
    config["runtime_admission"] = False
    args.func(args, config)
except controller.SprintError as exc:
    print(f"sprint-controller test driver: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc
raise SystemExit(0)
