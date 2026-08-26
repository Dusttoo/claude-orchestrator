#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0
ok() { printf 'ok   %s\n' "$1"; }
fail_case() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

mkdir -p "$TMP/repo/.git" "$TMP/repo/.orchestration"
cat > "$TMP/repo/.orchestration/config.yaml" <<'YAML'
self_check:
  - name: noisy
    run: 'i=1; while [ "$i" -le 10 ]; do echo "line-$i"; i=$((i+1)); done; exit 1'
gate_log_lines: 3
YAML

cd "$TMP/repo" || exit 1
if "$ROOT/scripts/run-gates.sh" >"$TMP/output" 2>&1; then
  fail_case "failing noisy gate remains blocking"
else
  ok "failing noisy gate remains blocking"
fi
if grep -q 'line-10' "$TMP/output" && ! grep -q 'line-1$' "$TMP/output"; then
  ok "gate output includes only the configured log tail"
else
  fail_case "gate output includes only the configured log tail"
fi
log="$(find "$TMP/repo/.orchestration/.gate-logs" -type f -name '*.log' -print -quit)"
if [ -n "$log" ] && grep -q 'line-1' "$log" && grep -q 'line-10' "$log"; then
  ok "full failure output remains available outside model context"
else
  fail_case "full failure output remains available outside model context"
fi

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
