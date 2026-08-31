#!/usr/bin/env bash
# review-contract.test.sh -- keep the review-efficiency contracts aligned.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
failures=0

require_text() {
  local file="$1" pattern="$2" label="$3"
  if ! grep -Fq -- "$pattern" "$ROOT/$file"; then
    echo "FAIL: $label ($file missing: $pattern)" >&2
    failures=$((failures + 1))
  fi
}

require_text agents/orchestration-design-reviewer.md "Trust boundary" "design gate defines trust boundary"
require_text agents/orchestration-design-reviewer.md "Impossible guarantees" "design gate names impossible guarantees"
require_text agents/orchestration-design-reviewer.md "Rejected alternatives" "design gate rejects fragile alternatives"
require_text agents/orchestration-design-reviewer.md "Finish every section and the full" "design review batches findings"

for file in skills/orchestrate-ticket/SKILL.md commands/orchestrate.md skills/scope-ticket/SKILL.md; do
  require_text "$file" "adversarial test matrix" "pre-implementation matrix is required"
done

for file in agents/orchestration-code-reviewer.md agents/orchestration-security-reviewer.md agents/orchestration-visual-qa.md; do
  require_text "$file" "full" "reviewer completes the full sweep"
  require_text "$file" "[component:" "review findings identify a stable component"
done

for file in agents/orchestration-code-reviewer.md agents/orchestration-security-reviewer.md; do
  require_text "$file" "raw unified git diff" "reviewers default to the raw unified diff"
  require_text "$file" "Do not index" "reviewers do not re-index the full codebase"
  require_text "$file" "Return JSON only" "reviewers emit machine-readable output"
  require_text "$file" "explanation" "reviewers explain findings only"
  require_text "$file" '"findings":[]' "clean reviews avoid narrative output"
done

require_text scripts/context_pipeline.py '"type": "json_schema"' "API providers receive a reviewer schema"
require_text scripts/context_pipeline.py 'validate_review_output' "review output is validated mechanically"
require_text docs/reviewer-output.md "Explanations are generated only for actual findings" \
  "reviewer output efficiency is documented"

for file in skills/orchestrate-ticket/SKILL.md skills/gate-pr/SKILL.md commands/orchestrate.md commands/gate.md; do
  require_text "$file" "raw unified" "gate handoffs provide raw unified diffs"
  require_text "$file" "full-codebase index" "gate handoffs omit full repository indexes"
done

for file in skills/orchestrate-ticket/SKILL.md skills/gate-pr/SKILL.md commands/orchestrate.md commands/gate.md; do
  require_text "$file" "failure ledger" "orchestrator tracks repeated component failures"
  require_text "$file" "record-repair" "repairs are durable artifacts"
  require_text "$file" "complete-repair-review" "all required gates close one repair attempt"
done

# --- review-loop convergence contracts ----------------------------------------
# The loop must terminate. Each of these is one of the mechanisms that makes it.

require_text agents/orchestration-code-reviewer.md "Round 1 -- full authority" \
  "round 1 sweeps with full blocking authority"
require_text agents/orchestration-code-reviewer.md "Round 2+ -- scope freeze" \
  "later rounds freeze what may block"
require_text agents/orchestration-code-reviewer.md "Severity: BLOCKING vs ADVISORY" \
  "reviewer splits blocking from advisory findings"
require_text agents/orchestration-code-reviewer.md "[component: <path>:<symbol>]" \
  "component keys are path plus symbol, not free text"
require_text agents/orchestration-code-reviewer.md "Round 3 or later" \
  "reviewer doubt rule is round-aware"
require_text agents/orchestration-code-reviewer.md "ADVISORY -- report it, do not block" \
  "dead weight is advisory, not a merge blocker"
require_text agents/orchestration-security-reviewer.md "exempt from the review loop's scope freeze" \
  "security findings keep blocking authority in every round"
require_text agents/orchestration-design-reviewer.md "Finding survived a completed repair" \
  "failed-repair redesign is scoped to the failing component"
require_text templates/config.yaml "max_design_rounds" "the design cap is configurable"
require_text templates/config.yaml "max_repair_cycles" "the repair cap is configurable"
require_text templates/ORCHESTRATION.md "max_repair_cycles" "the repair cap is documented"

for file in skills/orchestrate-ticket/SKILL.md skills/gate-pr/SKILL.md commands/orchestrate.md commands/gate.md; do
  require_text "$file" "review-ledger.py" "orchestrator drives the durable review ledger"
  require_text "$file" "escalate-human" "orchestrator stops the loop at the round cap"
  require_text "$file" "brief" "orchestrator hands each reviewer its round brief"
done

claude_version="$(sed -n 's/.*"version": "\([^"]*\)".*/\1/p' "$ROOT/.claude-plugin/plugin.json" | head -1)"
codex_version="$(sed -n 's/.*"version": "\([^"]*\)".*/\1/p' "$ROOT/.codex-plugin/plugin.json" | head -1)"
if [ "$claude_version" != "$codex_version" ]; then
  echo "FAIL: plugin versions differ ($claude_version != $codex_version)" >&2
  failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
  echo "review contract tests passed"
fi
exit "$failures"
