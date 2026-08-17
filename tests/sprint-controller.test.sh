#!/usr/bin/env bash
# sprint-controller.test.sh -- scheduling is bounded, resumable, and continues
# independent work past blocked tickets.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
CONTROLLER="$ROOT/scripts/sprint-controller.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fails=0
ok() { printf 'ok   %s\n' "$1"; }
fail_case() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
run_ok() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$label"; else fail_case "$label"; fi
}
run_fail() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then fail_case "$label"; else ok "$label"; fi
}
json_check() {
  local label="$1" file="$2" expression="$3"
  if python3 -c 'import json,sys; data=json.load(open(sys.argv[1])); assert eval(sys.argv[2], {"data": data})' "$file" "$expression"; then
    ok "$label"
  else
    fail_case "$label"
  fi
}

mkdir -p "$TMP/repo/.git" "$TMP/repo/.orchestration"
cp "$ROOT/templates/config.yaml" "$TMP/repo/.orchestration/config.yaml"
sed -i.bak 's/^concurrency_max:.*/concurrency_max: 2/' "$TMP/repo/.orchestration/config.yaml"
rm "$TMP/repo/.orchestration/config.yaml.bak"

cat > "$TMP/repo/inventory.json" <<'JSON'
{
  "project": "PROJ",
  "sprint": {"id": "42", "name": "Sprint 42"},
  "source_query": "project = PROJ AND sprint = 42",
  "tickets": [
    {"key": "PROJ-1", "summary": "root", "status": "Ready", "dependencies": []},
    {"key": "PROJ-2", "summary": "after root", "status": "Ready", "dependencies": ["PROJ-1", "PROJ-1"]},
    {"key": "PROJ-3", "summary": "independent", "status": "Ready", "dependencies": []},
    {"key": "PROJ-4", "summary": "jira blocked", "status": "Blocked", "dependencies": []},
    {"key": "PROJ-5", "summary": "external wait", "status": "Ready", "dependencies": ["EXT-9"]},
    {"key": "PROJ-6", "summary": "cycle a", "status": "Ready", "dependencies": ["PROJ-7"]},
    {"key": "PROJ-7", "summary": "cycle b", "status": "Ready", "dependencies": ["PROJ-6"]},
    {"key": "PROJ-8", "summary": "needs owner", "status": "In Progress", "dependencies": []}
  ],
  "dependency_status": {"EXT-9": "In Progress"}
}
JSON

cd "$TMP/repo" || exit 1
run_ok "sync creates normalized durable checkpoint" "$CONTROLLER" sync --inventory inventory.json
"$CONTROLLER" plan --sprint 42 > "$TMP/plan1.json"
json_check "plan fills exactly two lanes" "$TMP/plan1.json" 'data["launch"] == ["PROJ-1", "PROJ-3"] and data["concurrency_max"] == 2'
json_check "dependency and cycle tickets wait without stopping independent work" "$TMP/plan1.json" 'len(data["waiting"]) == 4'

run_ok "first lane reserves atomically" "$CONTROLLER" reserve --sprint 42 --ticket PROJ-1 --run-ref pending-one
run_ok "second lane reserves atomically" "$CONTROLLER" reserve --sprint 42 --ticket PROJ-3 --run-ref pending-three
run_fail "third reservation is rejected at concurrency_max" "$CONTROLLER" reserve --sprint 42 --ticket PROJ-2 --run-ref should-fail
run_ok "actual worker reference attaches after launch" "$CONTROLLER" attach --sprint 42 --ticket PROJ-1 --run-ref codex-task-one

"$CONTROLLER" plan --sprint 42 > "$TMP/restart.json"
json_check "restart exposes running work for reconciliation" "$TMP/restart.json" 'data["needs_reconcile"] == ["PROJ-1", "PROJ-3"] and data["launch"] == []'

run_ok "completed prerequisite checkpoints immediately" "$CONTROLLER" finish --sprint 42 --ticket PROJ-1 --outcome completed --summary merged --pr 101 --branch feature/one
run_ok "blocked independent ticket frees its lane" "$CONTROLLER" finish --sprint 42 --ticket PROJ-3 --outcome blocked --summary 'test failure'
"$CONTROLLER" plan --sprint 42 > "$TMP/plan2.json"
json_check "completed prerequisite unlocks dependent ticket" "$TMP/plan2.json" 'data["launch"] == ["PROJ-2"]'

run_ok "unlocked ticket reserves" "$CONTROLLER" reserve --sprint 42 --ticket PROJ-2 --run-ref pending-two
run_ok "running ticket survives inventory resync" "$CONTROLLER" sync --inventory inventory.json
"$CONTROLLER" plan --sprint 42 > "$TMP/resync.json"
json_check "resync does not duplicate a running workflow" "$TMP/resync.json" 'data["needs_reconcile"] == ["PROJ-2"] and "PROJ-2" not in data["launch"]'
run_ok "lost worker can be explicitly requeued after proof" "$CONTROLLER" requeue --sprint 42 --ticket PROJ-2 --reason 'worker no longer exists'
run_ok "requeued ticket can reserve again" "$CONTROLLER" reserve --sprint 42 --ticket PROJ-2 --run-ref codex-task-two
run_ok "recovered ticket completes" "$CONTROLLER" finish --sprint 42 --ticket PROJ-2 --outcome completed --summary merged --pr 102 --branch feature/two

"$CONTROLLER" summary --sprint 42 > "$TMP/summary.json"
json_check "summary separates completed, blocked, and user action" "$TMP/summary.json" '([x["key"] for x in data["completed"]] == ["PROJ-1", "PROJ-2"] and [x["key"] for x in data["user_action"]] == ["PROJ-8"] and set(x["key"] for x in data["blocked"]) == {"PROJ-3", "PROJ-4", "PROJ-5", "PROJ-6", "PROJ-7"})'
json_check "summary finishes after autonomous work is exhausted" "$TMP/summary.json" 'data["finished"] is True and data["running"] == []'

cat > "$TMP/repo/duplicate.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"99","name":"bad"},"tickets":[{"key":"PROJ-1","status":"Ready"},{"key":"proj-1","status":"Ready"}]}
JSON
run_fail "duplicate normalized Jira keys fail closed" "$CONTROLLER" sync --inventory duplicate.json
run_fail "checkpoint directory cannot escape the repository" "$CONTROLLER" --state-dir ../outside sync --inventory inventory.json

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
