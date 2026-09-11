#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$HERE/ticket_restart_test.py"
python3 "$HERE/sprint_controller_resilience_test.py"
