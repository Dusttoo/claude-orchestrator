#!/usr/bin/env bash
# plugin-parity.test.sh -- Claude commands and Codex skills must expose the same
# configured workflow operations through the shared engine.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
ENGINE="$ROOT/scripts/orchestration-engine.py"
FIX="$HERE/fixtures/workflows"

fails=0
ok() { printf 'ok   %s\n' "$1"; }
fail_case() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else
    printf 'FAIL %s\n     want: [%s]\n     got:  [%s]\n' "$1" "$2" "$3"
    fails=$((fails + 1))
  fi
}
contains_engine() {
  local file="$1"
  if rg -q 'orchestration-engine\.py' "$ROOT/$file"; then ok "$file uses shared engine"
  else fail_case "$file does not reference shared engine"; fi
}

contains_engine commands/release.md
contains_engine skills/release-integration/SKILL.md
contains_engine commands/orchestrate.md
contains_engine skills/orchestrate-ticket/SKILL.md
contains_engine commands/gate.md
contains_engine skills/gate-pr/SKILL.md

compare_plan() {
  local fixture="$1" transition="$2"; shift 2
  local claude codex
  claude="$("$ENGINE" --config "$FIX/$fixture.yaml" adapter-plan --host claude "$transition" "$@")"
  codex="$("$ENGINE" --config "$FIX/$fixture.yaml" adapter-plan --host codex "$transition" "$@")"
  eq "Claude/Codex adapter plan parity: $fixture:$transition" "$claude" "$codex"
}

compare_plan gecktopia-adr-008 freeze --var candidate_id=rc1
compare_plan gecktopia-adr-008 promote-production --var candidate_id=rc1
compare_plan protected-mainline verify --var ticket_key=ONE --var slug=x
compare_plan simple-integration merge --var ticket_key=ONE --var slug=x

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
