#!/usr/bin/env bash
# release-workflow.test.sh -- release-candidate behavior must be expressed by
# fixture configuration, not by a shared hard-coded transition graph.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
ENGINE="$ROOT/scripts/orchestration-engine.py"
FIX="$HERE/fixtures/workflows"

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
contains_text() {
  local desc="$1" haystack="$2" needle="$3"
  if grep -q "$needle" <<<"$haystack"; then ok "$desc"; else fail_case "$desc"; fi
}

plan_geck() {
  "$ENGINE" --config "$FIX/gecktopia-adr-008.yaml" plan-transition "$1" --var candidate_id=rc1
}

check "Gecktopia fixture validates as configuration" \
  "$ENGINE" --config "$FIX/gecktopia-adr-008.yaml" validate-config
QA_PLAN="$(plan_geck qa-approve)"
MERGE_PLAN="$(plan_geck merge-candidate)"
PROMOTE_PLAN="$(plan_geck promote-production)"
RECONCILE_PLAN="$(plan_geck reconcile)"
contains_text "QA approval is a distinct configured transition" "$QA_PLAN" '^to=qa-approved$'
contains_text "candidate merge reaches merged state" "$MERGE_PLAN" '^to=merged$'
contains_text "production promotion is separate from merge" "$PROMOTE_PLAN" '^to=promoted$'
contains_text "promotion requires exact artifact by policy" "$PROMOTE_PLAN" '^artifact_identity_required=true$'
contains_text "promotion requires tag by policy" "$PROMOTE_PLAN" '^tag_required=true$'
contains_text "reconciliation is explicit" "$RECONCILE_PLAN" '^to=reconciled$'
contains_text "reconciliation actions are configured" "$RECONCILE_PLAN" '^reconciliation_actions='

check_not "mainline fixture does not inherit candidate freeze transition" \
  "$ENGINE" --config "$FIX/protected-mainline.yaml" plan-transition freeze
check_not "simple integration fixture does not inherit candidate branch role" \
  "$ENGINE" --config "$FIX/simple-integration.yaml" branch-name candidate --var candidate_id=rc1
check_not "legacy config does not auto-adopt release-candidate transitions" \
  "$ENGINE" --config "$FIX/legacy-v1.yaml" plan-transition freeze

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
