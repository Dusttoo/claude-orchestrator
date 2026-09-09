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
git branch -M main
SOURCE_SHA="$(git rev-parse HEAD)"

if bash -c 'unset ORCH_RUN_ID; "$1" --ticket-id 72 --ticket-title "Repair inbound intakes" --source-ref main --source-sha "$2"' _ "$SCRIPT" "$SOURCE_SHA" >/dev/null 2>&1; then printf 'FAIL template refuses missing caller run id\n'; fails=$((fails + 1)); else printf 'ok   template refuses missing caller run id\n'; fi

branch="$(ORCH_RUN_ID=canvas-123 "$SCRIPT" --ticket-id 72 --ticket-title 'Repair inbound intakes!' --source-ref main --source-sha "$SOURCE_SHA")"
check "template resolves deterministic namespaced branch" test "$branch" = 'run/canvas-123/feat/72-repair-inbound-intakes'
check "run mirror is persisted" test -f .orchestration/runs/canvas-123/ticket-branch.json
check "run mirror is gitignored" git check-ignore -q .orchestration/runs/canvas-123/ticket-branch.json
same="$(ORCH_RUN_ID=canvas-123 "$SCRIPT" --ticket-id 72 --ticket-title 'Repair inbound intakes!' --source-ref main --source-sha "$SOURCE_SHA")"
check "same durable caller context reuses branch" test "$same" = "$branch"
if ORCH_RUN_ID=canvas-123 "$SCRIPT" --ticket-id 72 --ticket-title 'Repair inbound intakes!' --source-ref main --source-sha 0000000000000000000000000000000000000000 >/dev/null 2>&1; then printf 'FAIL source mismatch is refused\n'; fails=$((fails + 1)); else printf 'ok   source mismatch is refused\n'; fi
other="$(ORCH_RUN_ID=canvas-456 "$SCRIPT" --ticket-id 72 --ticket-title 'Repair inbound intakes!' --source-ref main --source-sha "$SOURCE_SHA")"
check "different caller run receives independent branch" test "$other" = 'run/canvas-456/feat/72-repair-inbound-intakes'

sed -i.bak 's#run/{run_id}/feat/{ticket_id}-{ticket_slug}#bad branch/{run_id}#' .orchestration/config.yaml
if ORCH_RUN_ID=invalid-first "$SCRIPT" --ticket-id 72 --ticket-title 'Repair inbound intakes!' --source-ref main --source-sha "$SOURCE_SHA" >/dev/null 2>&1; then printf 'FAIL invalid ref is refused before persistence\n'; fails=$((fails + 1)); else printf 'ok   invalid ref is refused before persistence\n'; fi
check "invalid ref leaves no durable mirror" test ! -e .orchestration/runs/invalid-first/ticket-branch.json
mv .orchestration/config.yaml.bak .orchestration/config.yaml
check "corrected template can reuse run after invalid ref" env ORCH_RUN_ID=invalid-first "$SCRIPT" --ticket-id 72 --ticket-title 'Repair inbound intakes!' --source-ref main --source-sha "$SOURCE_SHA"

ORCH_RUN_ID=concurrent "$SCRIPT" --ticket-id 72 --ticket-title 'Repair inbound intakes!' --source-ref main --source-sha "$SOURCE_SHA" > "$TMP/one" & first=$!
ORCH_RUN_ID=concurrent "$SCRIPT" --ticket-id 72 --ticket-title 'Repair inbound intakes!' --source-ref main --source-sha "$SOURCE_SHA" > "$TMP/two" & second=$!
if wait "$first" && wait "$second" && cmp -s "$TMP/one" "$TMP/two"; then printf 'ok   concurrent identical resolution is atomic\n'; else printf 'FAIL concurrent identical resolution is atomic\n'; fails=$((fails + 1)); fi

echo
if [ "$fails" -eq 0 ]; then echo 'ALL PASS'; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
