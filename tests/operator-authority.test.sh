#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
HELPER="$ROOT/host-tools/orchestration-recovery-authority.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo"
export ORCHESTRATION_AUTHORITY_TEST_MODE=1
export ORCHESTRATION_AUTHORITY_STATE_DIR="$TMP/state"

fails=0
ok() { printf 'ok   %s\n' "$1"; }
bad() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

budget_scope="$(python3 -c 'import json,sys,pathlib; print(json.dumps({"kind":"budget","repository":str(pathlib.Path(sys.argv[1]).resolve()),"ticket":"PROJ-1"},sort_keys=True,separators=(",",":")))' "$TMP/repo")"
recovery_scope="$(python3 -c 'import json,sys,pathlib; print(json.dumps({"kind":"recovery","repository":str(pathlib.Path(sys.argv[1]).resolve()),"ticket":"PROJ-1","attempt":2},sort_keys=True,separators=(",",":")))' "$TMP/repo")"
relaunch_scope="$(python3 -c 'import json,sys,pathlib; print(json.dumps({"kind":"relaunch","repository":str(pathlib.Path(sys.argv[1]).resolve()),"ticket":"PROJ-1"},sort_keys=True,separators=(",",":")))' "$TMP/repo")"

budget_token="$($HELPER issue-budget --repository "$TMP/repo" --ticket PROJ-1 --ceiling-usd 35.25)"
if printf '%s\n' "$budget_token" | "$HELPER" activate-budget --scope "$budget_scope" | grep -qx 35.25; then
  ok "budget capability activates its exact absolute ceiling"
else bad "budget capability activates its exact absolute ceiling"; fi
if "$HELPER" budget-ceiling --scope "$budget_scope" | grep -qx 35.25; then
  ok "active budget ceiling remains queryable"
else bad "active budget ceiling remains queryable"; fi
if printf '%s\n' "$budget_token" | "$HELPER" activate-budget --scope "$budget_scope" >/dev/null 2>&1; then
  bad "budget capability is one-shot"
else ok "budget capability is one-shot"; fi

recovery_token="$($HELPER issue-recovery --repository "$TMP/repo" --ticket PROJ-1 --attempt 2)"
if printf '%s\n' "$recovery_token" | "$HELPER" consume-recovery --scope "$recovery_scope"; then
  ok "recovery capability consumes for its exact attempt"
else bad "recovery capability consumes for its exact attempt"; fi
if printf '%s\n' "$recovery_token" | "$HELPER" consume-recovery --scope "$recovery_scope" >/dev/null 2>&1; then
  bad "recovery capability is one-shot"
else ok "recovery capability is one-shot"; fi

relaunch_token="$($HELPER issue-relaunch --repository "$TMP/repo" --ticket PROJ-1 --ceiling-attempts 4)"
if printf '%s\n' "$relaunch_token" | "$HELPER" activate-relaunch --scope "$relaunch_scope" | grep -qx 4; then
  ok "relaunch capability activates its exact absolute attempt ceiling"
else bad "relaunch capability activates its exact absolute attempt ceiling"; fi
if "$HELPER" relaunch-ceiling --scope "$relaunch_scope" | grep -qx 4; then
  ok "active relaunch ceiling remains queryable"
else bad "active relaunch ceiling remains queryable"; fi
if printf '%s\n' "$relaunch_token" | "$HELPER" activate-relaunch --scope "$relaunch_scope" >/dev/null 2>&1; then
  bad "relaunch capability is one-shot"
else ok "relaunch capability is one-shot"; fi
run_lower="$($HELPER issue-relaunch --repository "$TMP/repo" --ticket PROJ-1 --ceiling-attempts 3)"
if printf '%s\n' "$run_lower" | "$HELPER" activate-relaunch --scope "$relaunch_scope" | grep -qx 4; then
  ok "a lower relaunch grant cannot narrow an active higher ceiling"
else bad "a lower relaunch grant cannot narrow an active higher ceiling"; fi
$HELPER revoke-relaunch --repository "$TMP/repo" --ticket PROJ-1
if "$HELPER" relaunch-ceiling --scope "$relaunch_scope" >/dev/null 2>&1; then
  bad "revoked relaunch ceiling is unavailable"
else ok "revoked relaunch ceiling is unavailable"; fi

mkdir "$TMP/symlink-target"
ln -s "$TMP/symlink-target" "$TMP/symlink-state"
ORCHESTRATION_AUTHORITY_STATE_DIR="$TMP/symlink-state" \
  "$HELPER" issue-budget --repository "$TMP/repo" --ticket PROJ-1 \
  --ceiling-usd 40 >/dev/null 2>&1
if [ "$?" -eq 0 ]; then
  bad "authority rejects a symlinked state root"
else
  ok "authority rejects a symlinked state root"
fi

if "$HELPER" issue-budget --repository "$TMP/repo" --ticket 'not-a-ticket' \
  --ceiling-usd 40 >/dev/null 2>&1; then
  bad "authority rejects a non-canonical ticket scope"
else
  ok "authority rejects a non-canonical ticket scope"
fi
if "$HELPER" issue-budget --repository "$TMP/repo" --ticket PROJ-1 \
  --ceiling-usd NaN >/dev/null 2>&1; then
  bad "authority rejects a non-finite budget ceiling"
else
  ok "authority rejects a non-finite budget ceiling"
fi
if "$HELPER" issue-relaunch --repository "$TMP/repo" --ticket PROJ-1 \
  --ceiling-attempts 0 >/dev/null 2>&1; then
  bad "authority rejects a non-positive relaunch ceiling"
else
  ok "authority rejects a non-positive relaunch ceiling"
fi

if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails failure(s)"; fi
exit "$fails"
