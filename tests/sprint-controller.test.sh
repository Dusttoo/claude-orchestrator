#!/usr/bin/env bash
# sprint-controller.test.sh -- scheduling is bounded, resumable, and continues
# independent work past blocked tickets.
set -uo pipefail
export ORCHESTRATION_TEST_MODE=1

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
  if "$@" >/dev/null; then ok "$label"; else fail_case "$label"; fi
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
jira_receipt() {
  local inventory="$1" artifact="$1.fetch.json" transport="$1.transport.json"
  python3 - "$inventory" "$transport" <<'PY'
import json,sys
value=json.load(open(sys.argv[1])); parents=sorted(x["key"].upper() for x in value["tickets"])
children=sorted(x.upper() for x in value["subtask_keys"])
by_key={x["key"].upper():x for x in value["tickets"]}
parent_issues=[{"key":key,"fields":{"subtasks":[{"key":x} for x in by_key[key].get("subtasks",[])]}} for key in parents]
child_issues=[{"key":key,"fields":{"parent":{"key":by_key.get(key,{}).get("parent","")}}} for key in children]
json.dump({"parents":[{"startAt":0,"total":len(parents),"isLast":True,"issues":parent_issues}],"children":[{"startAt":0,"total":len(children),"isLast":True,"issues":child_issues}]},open(sys.argv[2],"w"))
PY
  python3 "$ROOT/scripts/jira_inventory_fetch.py" --inventory-template "$inventory" \
    --test-transport "$transport" --artifact "$artifact" --output "$inventory"
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
  "subtask_source_query": "parent in sprint tickets",
  "subtask_keys": [],
  "tickets": [
    {"key": "PROJ-1", "summary": "root", "status": "Ready", "dependencies": [], "subtasks": []},
    {"key": "PROJ-2", "summary": "after root", "status": "Ready", "dependencies": ["PROJ-1", "PROJ-1"], "subtasks": []},
    {"key": "PROJ-3", "summary": "independent", "status": "Ready", "dependencies": [], "subtasks": []},
    {"key": "PROJ-4", "summary": "jira blocked", "status": "Blocked", "dependencies": [], "subtasks": []},
    {"key": "PROJ-5", "summary": "external wait", "status": "Ready", "dependencies": ["EXT-9"], "subtasks": []},
    {"key": "PROJ-6", "summary": "cycle a", "status": "Ready", "dependencies": ["PROJ-7"], "subtasks": []},
    {"key": "PROJ-7", "summary": "cycle b", "status": "Ready", "dependencies": ["PROJ-6"], "subtasks": []},
    {"key": "PROJ-8", "summary": "needs owner", "status": "In Progress", "dependencies": [], "subtasks": []}
  ],
  "dependency_status": {"EXT-9": "In Progress"}
}
JSON
jira_receipt "$TMP/repo/inventory.json"

cd "$TMP/repo" || exit 1
run_ok "sync creates normalized durable checkpoint" "$CONTROLLER" sync --inventory inventory.json
if env -u ORCHESTRATION_TEST_MODE "$CONTROLLER" sync --inventory inventory.json >/dev/null 2>&1; then
  fail_case "test transport cannot authorize production Jira sync"
else
  ok "test transport cannot authorize production Jira sync"
fi
python3 - inventory.json "$TMP/repo/self-sealed.json" "$TMP/repo/self-sealed.fetch.json" <<'PY'
import hashlib,json,sys
value=json.load(open(sys.argv[1])); artifact=json.load(open(value["fetch_artifact"]["path"]))
artifact["authority"]="provider-network"; artifact["approved_origin"]="https://jira.example"
json.dump(artifact,open(sys.argv[3],"w"),indent=2,sort_keys=True)
value["fetch_artifact"]={"path":sys.argv[3],"sha256":hashlib.sha256(json.dumps(artifact,sort_keys=True,separators=(",",":")).encode()).hexdigest()}
json.dump(value,open(sys.argv[2],"w"))
PY
run_fail "self-sealed caller Jira bundle cannot authorize production sync" "$CONTROLLER" sync --inventory "$TMP/repo/self-sealed.json"
python3 - inventory.json "$TMP/repo/gapped.json" "$TMP/repo/gapped.fetch.json" <<'PY'
import copy,json,sys
value=json.load(open(sys.argv[1])); artifact=json.load(open(value["fetch_artifact"]["path"]))
artifact["queries"][0]["pages"][0]["start_at"]=1
json.dump(artifact,open(sys.argv[3],"w")); value["fetch_artifact"]["path"]=sys.argv[3]
json.dump(value,open(sys.argv[2],"w"))
PY
run_fail "gapped Jira pagination is rejected" "$CONTROLLER" sync --inventory "$TMP/repo/gapped.json"
python3 - inventory.json "$TMP/repo/truncated.json" "$TMP/repo/truncated.fetch.json" <<'PY'
import json,sys
value=json.load(open(sys.argv[1])); artifact=json.load(open(value["fetch_artifact"]["path"]))
artifact["queries"][0]["pages"][0]["total"]+=1
json.dump(artifact,open(sys.argv[3],"w")); value["fetch_artifact"]["path"]=sys.argv[3]
json.dump(value,open(sys.argv[2],"w"))
PY
run_fail "Jira pages truncated before provider total are rejected" "$CONTROLLER" sync --inventory "$TMP/repo/truncated.json"
python3 - inventory.json "$TMP/repo/wrong-items.json" "$TMP/repo/wrong-items.fetch.json" <<'PY'
import json,sys
value=json.load(open(sys.argv[1])); artifact=json.load(open(value["fetch_artifact"]["path"]))
artifact["queries"][0]["pages"][0]["item_keys"][0]="PROJ-999"
json.dump(artifact,open(sys.argv[3],"w")); value["fetch_artifact"]["path"]=sys.argv[3]
json.dump(value,open(sys.argv[2],"w"))
PY
run_fail "Jira page evidence must bind exact item keys" "$CONTROLLER" sync --inventory "$TMP/repo/wrong-items.json"
"$CONTROLLER" plan --sprint 42 > "$TMP/plan1.json"
json_check "plan fills exactly two lanes" "$TMP/plan1.json" 'data["launch"] == ["PROJ-1", "PROJ-3"] and data["concurrency_max"] == 2'
json_check "dependency and cycle tickets wait without stopping independent work" "$TMP/plan1.json" 'len(data["waiting"]) == 4'

"$CONTROLLER" reserve --sprint 42 --ticket PROJ-1 --run-ref pending-one > "$TMP/reserve1.json" && ok "first lane reserves atomically" || bad "first lane reserves atomically"
"$CONTROLLER" reserve --sprint 42 --ticket PROJ-3 --run-ref pending-three > "$TMP/reserve3.json" && ok "second lane reserves atomically" || bad "second lane reserves atomically"
TOKEN1="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_token"])' "$TMP/reserve1.json")"
ATTACH1="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attach_capability"])' "$TMP/reserve1.json")"
TOKEN3="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_token"])' "$TMP/reserve3.json")"
run_fail "third reservation is rejected at concurrency_max" "$CONTROLLER" reserve --sprint 42 --ticket PROJ-2 --run-ref should-fail
run_fail "stale worker cannot attach without its controller capability" "$CONTROLLER" attach --sprint 42 --ticket PROJ-1 --run-ref stale --attach-capability attach_stale
run_ok "actual worker reference attaches after launch" "$CONTROLLER" attach --sprint 42 --ticket PROJ-1 --run-ref pid:999999 --attach-capability "$ATTACH1"

"$CONTROLLER" plan --sprint 42 > "$TMP/restart.json"
json_check "restart exposes running work for reconciliation" "$TMP/restart.json" 'data["needs_reconcile"] == ["PROJ-1", "PROJ-3"] and data["launch"] == []'
"$CONTROLLER" summary --sprint 42 > "$TMP/restart-summary.json"
json_check "public sprint summary does not disclose attempt capabilities" "$TMP/restart-summary.json" 'all("attempt_token" not in x for x in data["running"])'

run_ok "completed prerequisite checkpoints immediately" "$CONTROLLER" finish --sprint 42 --ticket PROJ-1 --outcome completed --summary merged --pr 101 --branch feature/one --attempt-token "$TOKEN1"
run_ok "blocked independent ticket frees its lane" "$CONTROLLER" finish --sprint 42 --ticket PROJ-3 --outcome blocked --summary 'test failure' --attempt-token "$TOKEN3"
"$CONTROLLER" plan --sprint 42 > "$TMP/plan2.json"
json_check "completed prerequisite unlocks dependent ticket" "$TMP/plan2.json" 'data["launch"] == ["PROJ-2"]'

"$CONTROLLER" reserve --sprint 42 --ticket PROJ-2 --run-ref "pid:999999" > "$TMP/reserve2.json" && ok "unlocked ticket reserves with dead provisional identity" || bad "unlocked ticket reserves with dead provisional identity"
TOKEN2="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_token"])' "$TMP/reserve2.json")"
ATTACH2="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attach_capability"])' "$TMP/reserve2.json")"
run_ok "running ticket survives inventory resync" "$CONTROLLER" sync --inventory inventory.json
"$CONTROLLER" plan --sprint 42 > "$TMP/resync.json"
json_check "resync does not duplicate a running workflow" "$TMP/resync.json" 'data["needs_reconcile"] == ["PROJ-2"] and "PROJ-2" not in data["launch"]'
run_fail "requeue without stopped-worker proof fails closed" "$CONTROLLER" requeue --sprint 42 --ticket PROJ-2 --reason missing-proof --attempt-token "$TOKEN2"
run_fail "worker attempt token cannot rewrite death evidence" "$CONTROLLER" attach --sprint 42 --ticket PROJ-2 --run-ref pid:999999 --attach-capability "$TOKEN2"
run_ok "controller attach capability establishes actual worker identity once" "$CONTROLLER" attach --sprint 42 --ticket PROJ-2 --run-ref "pid:$$" --attach-capability "$ATTACH2"
run_fail "controller attach capability is one-use" "$CONTROLLER" attach --sprint 42 --ticket PROJ-2 --run-ref second-display --attach-capability "$ATTACH2"
run_fail "live attached worker blocks requeue despite dead provisional identity" "$CONTROLLER" requeue --sprint 42 --ticket PROJ-2 --reason 'worker no longer exists' --attempt-token "$TOKEN2"
printf 'recover-once' > .orchestration/operator-recovery.cap
run_ok "operator can requeue a live mechanically bound worker" "$CONTROLLER" requeue --sprint 42 --ticket PROJ-2 --reason 'operator stopped worker' --attempt-token "$TOKEN2" --operator-capability recover-once
run_ok "pending attempt history survives Jira resync" "$CONTROLLER" sync --inventory inventory.json
"$CONTROLLER" reserve --sprint 42 --ticket PROJ-2 --run-ref codex-task-two > "$TMP/reserve2b.json" && ok "requeued ticket can reserve again" || bad "requeued ticket can reserve again"
json_check "relaunch accounting survives pending sync" "$TMP/reserve2b.json" 'data["attempt"] == 2'
TOKEN2B="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_token"])' "$TMP/reserve2b.json")"
run_fail "superseded attempt cannot finish replacement" "$CONTROLLER" finish --sprint 42 --ticket PROJ-2 --outcome blocked --summary stale --attempt-token "$TOKEN2"
run_ok "recovered ticket completes" "$CONTROLLER" finish --sprint 42 --ticket PROJ-2 --outcome completed --summary merged --pr 102 --branch feature/two --attempt-token "$TOKEN2B"

"$CONTROLLER" summary --sprint 42 > "$TMP/summary.json"
json_check "summary separates completed, blocked, and user action" "$TMP/summary.json" '([x["key"] for x in data["completed"]] == ["PROJ-1", "PROJ-2"] and [x["key"] for x in data["user_action"]] == ["PROJ-8"] and set(x["key"] for x in data["blocked"]) == {"PROJ-3", "PROJ-4", "PROJ-5", "PROJ-6", "PROJ-7"})'
json_check "summary finishes after autonomous work is exhausted" "$TMP/summary.json" 'data["finished"] is True and data["running"] == []'

cat > "$TMP/repo/priority.json" <<'JSON'
{
  "project": "PROJ",
  "sprint": {"id": "43", "name": "Sprint 43"},
  "source_query": "project = PROJ AND sprint = 43",
  "subtask_source_query": "parent in sprint tickets",
  "subtask_keys": [],
  "tickets": [
    {"key": "PROJ-20", "summary": "medium", "status": "Ready", "priority": 3, "dependencies": [], "subtasks": []},
    {"key": "PROJ-21", "summary": "unranked", "status": "Ready", "dependencies": [], "subtasks": []},
    {"key": "PROJ-22", "summary": "urgent late key", "status": "Ready", "priority": 1, "dependencies": [], "subtasks": []},
    {"key": "PROJ-23", "summary": "urgent tie", "status": "Ready", "priority": "1", "dependencies": [], "subtasks": []},
    {"key": "PROJ-24", "summary": "urgent but dependent", "status": "Ready", "priority": 1, "dependencies": ["PROJ-20"], "subtasks": []},
    {"key": "PROJ-25", "summary": "low and dependent", "status": "Ready", "priority": 5, "dependencies": ["PROJ-20"], "subtasks": []}
  ]
}
JSON
jira_receipt "$TMP/repo/priority.json"

run_ok "sync accepts optional per-ticket priority" "$CONTROLLER" sync --inventory priority.json
"$CONTROLLER" plan --sprint 43 > "$TMP/priority-plan.json"
json_check "highest priority fills lanes first, ties broken by key" "$TMP/priority-plan.json" 'data["launch"] == ["PROJ-22", "PROJ-23"]'
json_check "unprioritized tickets sort after every ranked ticket" "$TMP/priority-plan.json" '[x["key"] for x in data["waiting"]] == ["PROJ-24", "PROJ-25"]'

"$CONTROLLER" reserve --sprint 43 --ticket PROJ-22 --run-ref p-one > "$TMP/reserve22.json" && ok "priority lane one reserves" || bad "priority lane one reserves"
run_ok "priority lane two reserves" "$CONTROLLER" reserve --sprint 43 --ticket PROJ-23 --run-ref p-two
TOKEN22="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_token"])' "$TMP/reserve22.json")"
run_fail "priority board still refuses a third lane" "$CONTROLLER" reserve --sprint 43 --ticket PROJ-21 --run-ref p-jump
run_ok "priority lane one finishes" "$CONTROLLER" finish --sprint 43 --ticket PROJ-22 --outcome completed --summary merged --pr 201 --branch feature/p-one --attempt-token "$TOKEN22"
"$CONTROLLER" plan --sprint 43 > "$TMP/priority-plan2.json"
json_check "next lane goes to the ranked ticket, not the unranked one" "$TMP/priority-plan2.json" 'data["launch"] == ["PROJ-20"]'

run_ok "priority survives inventory resync" "$CONTROLLER" sync --inventory priority.json
"$CONTROLLER" summary --sprint 43 > "$TMP/priority-summary.json"
json_check "summary reports each ticket priority" "$TMP/priority-summary.json" '{x["key"]: x["priority"] for x in data["completed"] + data["blocked"] + data["user_action"] + data["running"]} == {"PROJ-20": 3, "PROJ-21": None, "PROJ-22": 1, "PROJ-23": 1, "PROJ-24": 1, "PROJ-25": 5}'

# A checkpoint written before priority existed must keep planning, not crash.
python3 - "$TMP/repo/.orchestration/.sprint-state" <<'PY'
import json, sys
from pathlib import Path
for path in Path(sys.argv[1]).glob("*.json"):
    state = json.loads(path.read_text())
    for ticket in state["tickets"].values():
        ticket.pop("priority", None)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
PY
run_ok "pre-priority checkpoints still plan" "$CONTROLLER" plan --sprint 43
run_ok "pre-priority checkpoints still summarize" "$CONTROLLER" summary --sprint 42

cat > "$TMP/repo/bad-priority.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"44","name":"bad"},"source_query":"q","tickets":[{"key":"PROJ-30","status":"Ready","priority":"urgent"}]}
JSON
run_fail "non-integer priority fails closed" "$CONTROLLER" sync --inventory bad-priority.json

cat > "$TMP/repo/duplicate.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"99","name":"bad"},"tickets":[{"key":"PROJ-1","status":"Ready"},{"key":"proj-1","status":"Ready"}]}
JSON
run_fail "duplicate normalized Jira keys fail closed" "$CONTROLLER" sync --inventory duplicate.json
cat > "$TMP/repo/missing-subtask.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"100","name":"bad child inventory"},"source_query":"q","subtask_source_query":"children","subtask_keys":["PROJ-2"],"tickets":[{"key":"PROJ-1","status":"Ready","subtasks":["PROJ-2"]}]}
JSON
run_fail "missing referenced Jira subtasks fail closed" "$CONTROLLER" sync --inventory missing-subtask.json
cat > "$TMP/repo/unproven-empty-subtasks.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"101","name":"unproven children"},"source_query":"q","tickets":[{"key":"PROJ-1","status":"Ready","subtasks":[]}]}
JSON
run_fail "empty subtasks without an independent child query fail closed" "$CONTROLLER" sync --inventory unproven-empty-subtasks.json
run_fail "checkpoint directory cannot escape the repository" "$CONTROLLER" --state-dir ../outside sync --inventory inventory.json

cat > "$TMP/repo/relations.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"102","name":"relations"},"source_query":"sprint = 102","subtask_source_query":"parent in (PROJ-80)","subtask_keys":["PROJ-81"],"tickets":[{"key":"PROJ-80","status":"Ready","dependencies":[],"subtasks":["PROJ-81"]},{"key":"PROJ-81","parent":"PROJ-80","status":"Ready","dependencies":[],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/relations.json"
run_ok "adapter evidence binds both directions of Jira child relations" "$CONTROLLER" sync --inventory relations.json
python3 - relations.json "$TMP/repo/bad-relations.json" "$TMP/repo/bad-relations.fetch.json" <<'PY'
import json,sys
value=json.load(open(sys.argv[1])); artifact=json.load(open(value["fetch_artifact"]["path"]))
artifact["child_parents"]["PROJ-81"]="PROJ-999"
json.dump(artifact,open(sys.argv[3],"w")); value["fetch_artifact"]["path"]=sys.argv[3]
json.dump(value,open(sys.argv[2],"w"))
PY
run_fail "one-way or contradictory Jira relations are rejected" "$CONTROLLER" sync --inventory "$TMP/repo/bad-relations.json"

cat > "$TMP/repo/legacy-inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"47","name":"legacy running"},"source_query":"q","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-60","status":"Ready","dependencies":[],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/legacy-inventory.json"
run_ok "legacy migration fixture syncs" "$CONTROLLER" sync --inventory legacy-inventory.json
"$CONTROLLER" reserve --sprint 47 --ticket PROJ-60 --run-ref pid:999999 > /dev/null
python3 - "$TMP/repo/.orchestration/.sprint-state" <<'PY'
import json, sys
from pathlib import Path
path = next(Path(sys.argv[1]).glob('47-*.json'))
state = json.loads(path.read_text())
state['schema_version'] = 1
state['tickets']['PROJ-60'].pop('attempt_token', None)
path.write_text(json.dumps(state) + '\n')
PY
"$CONTROLLER" summary --sprint 47 > "$TMP/legacy-summary.json"
json_check "schema-v1 running lanes fence to explicit recovery" "$TMP/legacy-summary.json" 'data["user_action"][0]["key"] == "PROJ-60" and "legacy running lane" in data["user_action"][0]["reason"]'
run_ok "fenced legacy lane has an explicit recovery path" "$CONTROLLER" recover-legacy --sprint 47 --ticket PROJ-60 --reason 'operator verified old worker stopped'
"$CONTROLLER" plan --sprint 47 > "$TMP/legacy-plan.json"
json_check "recovered legacy lane becomes launchable without duplication" "$TMP/legacy-plan.json" 'data["launch"] == ["PROJ-60"]'
run_fail "legacy recovery capability is one-shot" "$CONTROLLER" recover-legacy --sprint 47 --ticket PROJ-60 --reason replay

cat > "$TMP/repo/limit-inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"48","name":"run limit"},"source_query":"q","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-70","status":"Ready","dependencies":[],"subtasks":[]},{"key":"PROJ-71","status":"Ready","dependencies":[],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/limit-inventory.json"
run_ok "run-limit inventory syncs" "$CONTROLLER" sync --inventory limit-inventory.json
python3 - "$TMP/repo/.orchestration/.llm-usage/usage.jsonl" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True)
with p.open('a') as f:
  for i in range(12):
    f.write(json.dumps({"kind":"reservation","reservation_id":f"limit-{i}","run_id":f"run-{i}","ticket":"PROJ-70","sprint":"48","role":"sprint-worker","projected_cost_usd":"0.001"})+'\n')
  for i in range(6):
    f.write(json.dumps({"kind":"reservation","reservation_id":f"review-limit-{i}","run_id":f"review-run-{i}","ticket":"PROJ-71","sprint":"48","role":"code-reviewer","projected_cost_usd":"0.001"})+'\n')
PY
"$CONTROLLER" plan --sprint 48 > "$TMP/limit-plan.json"
json_check "model and reviewer run breakers remove doomed replacements" "$TMP/limit-plan.json" 'data["launch"] == [] and all(data["spend"][key]["state"] == "operator_action" for key in ("PROJ-70","PROJ-71")) and data["spend"]["PROJ-71"]["reviewer_run_count"] == 6'
"$CONTROLLER" summary --sprint 48 > "$TMP/limit-summary.json"
json_check "run-limited-only sprint is terminal for captain" "$TMP/limit-summary.json" 'data["finished"] is True and [x["key"] for x in data["user_action"]] == ["PROJ-70","PROJ-71"]'

cat > "$TMP/repo/batch-inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"45","name":"batch"},"source_query":"q","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-40","summary":"batch one","status":"Ready","dependencies":[],"subtasks":[]},{"key":"PROJ-41","summary":"batch two","status":"Ready","dependencies":[],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/batch-inventory.json"
cat > "$TMP/repo/batch-jobs.json" <<'JSON'
{"jobs":[{"ticket":"PROJ-40","background":true,"interactive":false,"params":{"model":"claude-sonnet-5","max_tokens":100,"system":[{"type":"text","text":"cached","cache_control":{"type":"ephemeral"}}],"messages":[{"role":"user","content":"ticket 40"}]}},{"ticket":"PROJ-41","background":true,"interactive":false,"params":{"model":"claude-sonnet-5","max_tokens":100,"messages":[{"role":"user","content":"ticket 41"}]}}]}
JSON
run_ok "batch sprint inventory syncs" "$CONTROLLER" sync --inventory batch-inventory.json
"$CONTROLLER" prepare-batch --sprint 45 --jobs batch-jobs.json > "$TMP/batch-result.json"
json_check "non-interactive background lanes serialize as one Message Batch" "$TMP/batch-result.json" 'data["status"] == "pending_submission" and data["tickets"] == ["PROJ-40", "PROJ-41"]'
python3 - "$TMP/batch-result.json" <<'PY'
import json, sys
result=json.load(open(sys.argv[1]))
request=json.load(open(result["request"]))
marker=json.load(open(result["marker"]))
assert len(request["requests"]) == 2
assert all(set(item) == {"custom_id", "params"} for item in request["requests"])
assert marker["endpoint"] == "/v1/messages/batches"
assert marker["status"] == "pending_submission"
PY
if [ "$?" -eq 0 ]; then ok "batch request and durable state marker match Anthropic shape"; else fail_case "batch request and durable state marker match Anthropic shape"; fi
"$CONTROLLER" plan --sprint 45 > "$TMP/batch-plan.json"
json_check "serialized batch jobs atomically reserve their sprint lanes" "$TMP/batch-plan.json" 'data["launch"] == [] and data["running"] == ["PROJ-40", "PROJ-41"]'
BATCH_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["batch_id"])' "$TMP/batch-result.json")"
run_fail "caller failure cannot release uncertain submitted work" "$CONTROLLER" reconcile-batch --batch "$BATCH_ID" --outcome failed
cat > "$TMP/batch-terminal.json" <<JSON
{"schema_version":1,"provider":"anthropic","batch_id":"$BATCH_ID","provider_batch_id":"msgbatch_test","status":"cancelled","job_ids":["ticket_PROJ_40_$BATCH_ID","ticket_PROJ_41_$BATCH_ID"]}
JSON
run_fail "hand-authored terminal JSON cannot transition an uncertain batch" "$CONTROLLER" reconcile-batch --batch "$BATCH_ID" --outcome failed --provider-evidence "$TMP/batch-terminal.json"
cat > "$TMP/batch-transport.json" <<'JSON'
{"submit":{"id":"msgbatch_test","type":"message_batch","processing_status":"in_progress"},"status":{"id":"msgbatch_test","processing_status":"cancelled"},"result_pages":[]}
JSON
run_fail "production CLI has no synthetic batch transport authority" "$CONTROLLER" submit-batch --batch "$BATCH_ID" --test-transport "$TMP/batch-transport.json"

cat > "$TMP/repo/interactive-job.json" <<'JSON'
{"jobs":[{"ticket":"PROJ-40","background":true,"interactive":true,"params":{"model":"claude-sonnet-5","max_tokens":10,"messages":[{"role":"user","content":"x"}]}}]}
JSON
run_fail "interactive work is rejected from asynchronous batching" "$CONTROLLER" prepare-batch --sprint 45 --jobs interactive-job.json

cat > "$TMP/repo/openai-inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"46","name":"openai batch"},"source_query":"q","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-50","summary":"openai lane","status":"Ready","dependencies":[],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/openai-inventory.json"
cat > "$TMP/repo/openai-jobs.json" <<'JSON'
{"provider":"openai","jobs":[{"ticket":"PROJ-50","background":true,"interactive":false,"params":{"model":"gpt-5.6-sol","max_output_tokens":100,"input":[{"role":"developer","content":"stable"},{"role":"user","content":"ticket 50"}]}}]}
JSON
run_ok "OpenAI batch sprint inventory syncs" "$CONTROLLER" sync --inventory openai-inventory.json
"$CONTROLLER" prepare-batch --sprint 46 --jobs openai-jobs.json > "$TMP/openai-batch-result.json"
python3 - "$TMP/openai-batch-result.json" <<'PY'
import json, sys
result=json.load(open(sys.argv[1]))
assert result["provider"] == "openai" and result["status"] == "pending_upload"
line=json.loads(open(result["request"]).readline())
marker=json.load(open(result["marker"]))
assert set(line) == {"custom_id", "method", "url", "body"}
assert line["method"] == "POST" and line["url"] == "/v1/responses"
assert marker["endpoint"] == "/v1/batches" and marker["provider"] == "openai"
PY
if [ "$?" -eq 0 ]; then ok "OpenAI background lanes serialize to Batch JSONL"; else fail_case "OpenAI background lanes serialize to Batch JSONL"; fi
OPENAI_BATCH_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["batch_id"])' "$TMP/openai-batch-result.json")"
run_fail "OpenAI production CLI also rejects synthetic transport" "$CONTROLLER" submit-batch --batch "$OPENAI_BATCH_ID" --test-transport "$TMP/openai-nonterminal.json"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
