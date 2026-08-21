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

contains_contract() {
  local label="$1" pattern="$2"; shift 2
  local file
  for file in "$@"; do
    if ! rg -q "$pattern" "$ROOT/$file"; then
      fail_case "$label missing from $file"
      return
    fi
  done
  ok "$label present in Claude and Codex adapters"
}

contains_contract "shared sprint controller" 'sprint-controller\.py' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "sprint restart reconciliation" 'needs_reconcile' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "atomic sprint reservation" 'reserve' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "per-ticket workflow dispatch" 'orchestrate' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "three-way sprint summary" 'user-action' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "optional sprint ticket priority ordering" 'priority' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md

contains_contract "durable review ledger" 'review-ledger\.py' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md \
  commands/gate.md skills/gate-pr/SKILL.md
contains_contract "bounded review loop" 'escalate-human' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md \
  commands/gate.md skills/gate-pr/SKILL.md

contains_contract "host-neutral merge evidence" 'plugin version' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md \
  commands/gate.md skills/gate-pr/SKILL.md
contains_contract "explicit cleanup policy" 'worktree_cleanup' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md
contains_contract "hooks are optional" 'defense in depth' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md

if rg -q 'merge-guard\.sh" --assert-green "\$PR" "\$BRANCH"' "$ROOT/scripts/merge-on-green.sh"; then
  ok "sanctioned merge enforces evidence independently of host hooks"
else
  fail_case "sanctioned merge does not enforce evidence independently of host hooks"
fi

if rg -n '(^|[ `])scripts/[A-Za-z0-9_-]+\.(sh|py)' "$ROOT/commands" -g '*.md' >/dev/null; then
  fail_case "Claude commands contain target-relative plugin script paths"
else
  ok "Claude commands resolve every plugin script through CLAUDE_PLUGIN_ROOT"
fi

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
