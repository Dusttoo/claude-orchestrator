#!/usr/bin/env bash
# initialization.test.sh -- default scaffolding must remain legacy-safe and
# initialization adapters must validate through the shared engine.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."

fails=0
ok() { printf 'ok   %s\n' "$1"; }
fail_case() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
check() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else fail_case "$desc"; fi
}
check_not() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then fail_case "$desc"; else ok "$desc"; fi
}

check "template declares legacy schema by default" \
  grep -Eq '^schema_version:[[:space:]]*1([[:space:]]|$)' "$ROOT/templates/config.yaml"
check_not "template does not actively enable schema v2" \
  grep -Eq '^schema_version:[[:space:]]*2([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template leaves integration branch for repo detection" \
  grep -Eq '^integration_branch:[[:space:]]*""' "$ROOT/templates/config.yaml"
check "template leaves production branch for repo detection" \
  grep -Eq '^production_branch:[[:space:]]*""' "$ROOT/templates/config.yaml"
check "Claude init command validates through shared engine" \
  rg -q 'orchestration-engine\.py validate-config' "$ROOT/commands/orchestration-init.md"
check "Codex init skill validates through shared engine" \
  rg -q 'orchestration-engine\.py validate-config' "$ROOT/skills/orchestration-init/SKILL.md"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
