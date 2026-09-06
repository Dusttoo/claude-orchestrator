#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/ticket-branch.sh"
fails=0
check() { local desc="$1"; shift; if "$@" >/dev/null 2>&1; then printf 'ok   %s\n' "$desc"; else printf 'FAIL %s\n' "$desc"; fails=$((fails + 1)); fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git init -q "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t.t; git config user.name t
mkdir -p .orchestration
printf '%s\n' 'integration_branch: main' 'ticket_branch_template: "run/{run_id}/feat/{ticket_id}-{ticket_slug}"' > .orchestration/config.yaml
printf '%s\n' '.orchestration/runs/' > .gitignore
git add .; git commit -qm init

if bash -c 'unset ORCH_RUN_ID; "$1" --ticket-id 72 --ticket-title "Repair inbound intakes" --source-ref main --source-sha abc' _ "$SCRIPT" >/dev/null 2>&1; then printf 'FAIL template refuses missing caller run id\n'; fails=$((fails + 1)); else printf 'ok   template refuses missing caller run id\n'; fi

branch="$(ORCH_RUN_ID=canvas-123 "$SCRIPT" --ticket-id 72 --ticket-title 'Repair inbound intakes!' --source-ref main --source-sha abc)"
check "template resolves deterministic namespaced branch" test "$branch" = 'run/canvas-123/feat/72-repair-inbound-intakes'
check "run mirror is persisted" test -f .orchestration/runs/canvas-123/ticket-branch.json
check "run mirror is gitignored" git check-ignore -q .orchestration/runs/canvas-123/ticket-branch.json
same="$(ORCH_RUN_ID=canvas-123 "$SCRIPT" --ticket-id 72 --ticket-title 'Repair inbound intakes!' --source-ref main --source-sha abc)"
check "same durable caller context reuses branch" test "$same" = "$branch"
if ORCH_RUN_ID=canvas-123 "$SCRIPT" --ticket-id 72 --ticket-title 'Repair inbound intakes!' --source-ref main --source-sha changed >/dev/null 2>&1; then printf 'FAIL source mismatch is refused\n'; fails=$((fails + 1)); else printf 'ok   source mismatch is refused\n'; fi
other="$(ORCH_RUN_ID=canvas-456 "$SCRIPT" --ticket-id 72 --ticket-title 'Repair inbound intakes!' --source-ref main --source-sha abc)"
check "different caller run receives independent branch" test "$other" = 'run/canvas-456/feat/72-repair-inbound-intakes'

echo
if [ "$fails" -eq 0 ]; then echo 'ALL PASS'; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
