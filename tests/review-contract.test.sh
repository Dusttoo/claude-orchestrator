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

for file in skills/orchestrate-ticket/SKILL.md skills/gate-pr/SKILL.md commands/orchestrate.md commands/gate.md; do
  require_text "$file" "failure ledger" "orchestrator tracks repeated component failures"
  require_text "$file" "second failure" "second component failure triggers redesign"
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
