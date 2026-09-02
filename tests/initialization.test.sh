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
check "template defaults worktree cleanup to manual" \
  grep -Eq '^worktree_cleanup:[[:space:]]*manual([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template preserves desktop LLM execution by default" \
  grep -Eq '^[[:space:]]+execution:[[:space:]]*desktop([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template exposes optional per-role LLM routes" \
  rg -q '^[[:space:]]+roles:' "$ROOT/templates/config.yaml"
check "template gives API runs a hard USD ceiling" \
  rg -q '^[[:space:]]+max_usd_per_run:' "$ROOT/templates/config.yaml"
check "template requires explicit model pricing" \
  rg -q '^[[:space:]]+pricing:' "$ROOT/templates/config.yaml"
check "template configures an active Jira sprint by default" \
  grep -Eq '^sprint_id:[[:space:]]*active([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template keeps sprint checkpoints under orchestration runtime state" \
  grep -Eq '^sprint_checkpoint_dir:[[:space:]]*\.orchestration/\.sprint-state([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template defaults captain updates to event-driven" \
  grep -Eq '^sprint_status_update_mode:[[:space:]]*event([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template keeps a bounded captain heartbeat" \
  grep -Eq '^sprint_status_heartbeat_minutes:[[:space:]]*30([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template declares directional Jira dependency mapping" \
  rg -q '^sprint_dependency_links:' "$ROOT/templates/config.yaml"
check "Claude init command validates through shared engine" \
  rg -q 'orchestration-engine\.py validate-config' "$ROOT/commands/orchestration-init.md"
check "Codex init skill validates through shared engine" \
  rg -q 'orchestration-engine\.py validate-config' "$ROOT/skills/orchestration-init/SKILL.md"
check "Claude init runs plugin-owned conformance tests" \
  rg -q 'run-plugin-conformance\.sh' "$ROOT/commands/orchestration-init.md"
check "Codex init runs plugin-owned conformance tests" \
  rg -q 'run-plugin-conformance\.sh' "$ROOT/skills/orchestration-init/SKILL.md"
check "Claude init gitignores sprint checkpoints" \
  rg -q '\.orchestration/\.sprint-state/' "$ROOT/commands/orchestration-init.md"
check "Codex init gitignores sprint checkpoints" \
  rg -q '\.orchestration/\.sprint-state/' "$ROOT/skills/orchestration-init/SKILL.md"
check "Claude init gitignores API run state" \
  rg -q '\.orchestration/\.llm-runs/' "$ROOT/commands/orchestration-init.md"
  rg -q '\.orchestration/\.review-results/' "$ROOT/commands/orchestration-init.md"
  rg -q '\.orchestration/\.review-results/' "$ROOT/skills/orchestration-init/SKILL.md"
check "Codex init gitignores API usage state" \
  rg -q '\.orchestration/\.llm-usage/' "$ROOT/skills/orchestration-init/SKILL.md"
check "Claude init gitignores repository API credentials" \
  rg -q '\.orchestration/\.env' "$ROOT/commands/orchestration-init.md"
check "Codex init gitignores repository API credentials" \
  rg -q '\.orchestration/\.env' "$ROOT/skills/orchestration-init/SKILL.md"
check "API docs preserve container secret precedence" \
  rg -q 'take precedence, making platform secret injection' "$ROOT/docs/api-agent.md"
check_not "Claude init does not copy process docs into target repos" \
  rg -q 'Copy `templates/ORCHESTRATION\.md`' "$ROOT/commands/orchestration-init.md"
check_not "Codex init does not copy process docs into target repos" \
  rg -q 'Copy `templates/ORCHESTRATION\.md`' "$ROOT/skills/orchestration-init/SKILL.md"
check_not "Claude init does not vendor hooks into project settings" \
  rg -q 'Add .*hooks.*\.claude/settings\.json' "$ROOT/commands/orchestration-init.md"
check_not "Codex init does not vendor hooks into project settings" \
  rg -q 'add `hooks/hooks\.json` entries to `\.claude/settings\.json`' "$ROOT/skills/orchestration-init/SKILL.md"
check "plugin conformance runner owns merge-guard suite" \
  rg -q 'merge-guard\.test\.sh' "$ROOT/scripts/run-plugin-conformance.sh"
check "plugin conformance runner owns worktree-cleanup suite" \
  rg -q 'worktree\.test\.sh' "$ROOT/scripts/run-plugin-conformance.sh"
check "plugin conformance runner owns host-parity suite" \
  rg -q 'plugin-parity\.test\.sh' "$ROOT/scripts/run-plugin-conformance.sh"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
