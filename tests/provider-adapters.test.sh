#!/usr/bin/env bash
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0
ok() { printf 'ok   %s\n' "$1"; }
bad() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

if python3 -m unittest "$ROOT/tests/provider_batch_adapter_test.py"; then
  ok "provider-native batch normalization unit matrix"
else
  bad "provider-native batch normalization unit matrix"
fi

cat > "$TMP/inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"1","name":"one"},"source_query":"parents","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-1","status":"Ready","subtasks":[]},{"key":"PROJ-2","status":"Ready","subtasks":[]}]}
JSON
cat > "$TMP/jira-transport.json" <<'JSON'
{"parents":[{"isLast":false,"nextPageToken":"page-2","issues":[{"key":"PROJ-1","fields":{"subtasks":[]}}]},{"isLast":true,"issues":[{"key":"PROJ-2","fields":{"subtasks":[]}}]}],"children":[{"isLast":true,"issues":[]}]}
JSON
if python3 "$ROOT/scripts/jira_inventory_fetch.py" --inventory-template "$TMP/inventory.json" --test-transport "$TMP/jira-transport.json" --artifact "$TMP/jira.json" --output "$TMP/output.json" \
  && python3 - "$TMP/jira.json" <<'PY'
import json, pathlib, sys
value=json.load(open(sys.argv[1]))
assert value["authority"] == "test-only"
assert [page["start_at"] for page in value["queries"][0]["pages"]] == [0, 1]
assert value["queries"][0]["pages"][1]["cursor_in"] == "page-2"
assert all(pathlib.Path(page["raw_path"]).name == "sha256-" + page["raw_sha256"] + ".json" for query in value["queries"] for page in query["pages"])
PY
then ok "Jira adapter exhausts pagination into content-addressed raw evidence"; else bad "Jira adapter exhausts pagination into content-addressed raw evidence"; fi

if python3 "$ROOT/scripts/jira_inventory_fetch.py" --inventory-template "$TMP/inventory.json" --base-url http://jira.example --artifact "$TMP/no.json" --output "$TMP/no-out.json" >/dev/null 2>&1; then
  bad "Jira adapter rejects a non-HTTPS origin"
else ok "Jira adapter rejects a non-HTTPS origin"; fi

cat > "$TMP/request.json" <<'JSON'
{"requests":[{"custom_id":"a","params":{}},{"custom_id":"b","params":{}}]}
JSON
REQUEST_SHA="$(shasum -a 256 "$TMP/request.json" | awk '{print $1}')"
cat > "$TMP/marker.json" <<JSON
{"schema_version":2,"batch_id":"local","provider":"anthropic","status":"pending_submission","request_file":"$TMP/request.json","request_sha256":"$REQUEST_SHA","provider_batch_id":"","jobs":[{"custom_id":"a"},{"custom_id":"b"}]}
JSON
cat > "$TMP/batch-transport.json" <<'JSON'
{"submit":{"id":"remote","type":"message_batch","processing_status":"in_progress"},"status":{"id":"remote","processing_status":"ended"},"result_pages":[[{"custom_id":"a","result":{"type":"succeeded","message":{"id":"msg-a","usage":{"input_tokens":1,"output_tokens":2}}}}],[{"custom_id":"b","result":{"type":"succeeded","message":{"id":"msg-b","usage":{"input_tokens":3,"output_tokens":4}}}}]]}
JSON
if python3 "$ROOT/scripts/provider_batch_adapter.py" submit --marker "$TMP/marker.json" --test-transport "$TMP/batch-transport.json" > "$TMP/receipt.json" \
  && python3 "$ROOT/scripts/provider_batch_adapter.py" fetch --marker "$TMP/marker.json" --test-transport "$TMP/batch-transport.json" --output-dir "$TMP" > "$TMP/bundle-ref.json" \
  && python3 - "$TMP/marker.json" "$TMP/bundle-ref.json" <<'PY'
import json, sys
marker=json.load(open(sys.argv[1])); ref=json.load(open(sys.argv[2])); value=json.load(open(ref["path"]))
assert marker["provider_batch_id"] == "remote" and marker["status"] == "submitted"
assert value["authority"] == "test-only"
assert [row["custom_id"] for row in value["results"]] == ["a", "b"]
assert all(row["response_id"].startswith("msg-") for row in value["results"])
assert len(value["raw_pages"]) == 3
assert ref["path"].endswith("sha256-" + ref["sha256"] + ".json")
PY
then ok "provider batch adapter owns submission and immutable terminal acquisition"; else bad "provider batch adapter owns submission and immutable terminal acquisition"; fi

cp "$TMP/marker.json" "$TMP/changed-marker.json"
printf '\n' >> "$TMP/request.json"
if python3 "$ROOT/scripts/provider_batch_adapter.py" fetch --marker "$TMP/changed-marker.json" --test-transport "$TMP/batch-transport.json" --output-dir "$TMP/changed" >/dev/null 2>&1; then
  bad "modified retry request is rejected"
else ok "modified retry request is rejected"; fi

cat > "$TMP/uncertain-request.json" <<'JSON'
{"requests":[]}
JSON
UNCERTAIN_SHA="$(shasum -a 256 "$TMP/uncertain-request.json" | awk '{print $1}')"
cat > "$TMP/uncertain-marker.json" <<JSON
{"schema_version":2,"batch_id":"uncertain","provider":"anthropic","status":"pending_submission","request_file":"$TMP/uncertain-request.json","request_sha256":"$UNCERTAIN_SHA","provider_batch_id":"","jobs":[]}
JSON
cat > "$TMP/uncertain-transport.json" <<'JSON'
{"submit_error":"timeout"}
JSON
if python3 "$ROOT/scripts/provider_batch_adapter.py" submit --marker "$TMP/uncertain-marker.json" --test-transport "$TMP/uncertain-transport.json" >/dev/null 2>&1; then
  bad "ambiguous submission fails closed"
elif python3 "$ROOT/scripts/provider_batch_adapter.py" submit --marker "$TMP/uncertain-marker.json" --test-transport "$TMP/batch-transport.json" >/dev/null 2>&1; then
  bad "ambiguous submission cannot be retried"
else ok "ambiguous submission remains fenced across retries"; fi

exit "$fails"
