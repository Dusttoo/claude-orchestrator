#!/usr/bin/env bash
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0
ok() { printf 'ok   %s\n' "$1"; }
bad() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

cat > "$TMP/inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"1","name":"one"},"source_query":"parents","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-1","status":"Ready","subtasks":[]},{"key":"PROJ-2","status":"Ready","subtasks":[]}]}
JSON
cat > "$TMP/jira-transport.json" <<'JSON'
{"parents":[{"isLast":false,"nextPageToken":"page-2","issues":[{"key":"PROJ-1","fields":{"summary":"one","status":{"name":"Ready"},"priority":null,"sprint":{"id":"1","name":"one"},"subtasks":[],"issuelinks":[]}}]},{"isLast":true,"issues":[{"key":"PROJ-2","fields":{"summary":"two","status":{"name":"Ready"},"priority":null,"sprint":{"id":"1","name":"one"},"subtasks":[],"issuelinks":[]}}]}],"children":[{"isLast":true,"issues":[]}]}
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

cat > "$TMP/marker.json" <<'JSON'
{"batch_id":"local","provider":"anthropic","provider_batch_id":"remote","jobs":[{"custom_id":"a"},{"custom_id":"b"}]}
JSON
cat > "$TMP/batch-transport.json" <<'JSON'
{"status":{"id":"remote","processing_status":"ended"},"result_pages":[[{"custom_id":"a","result":{"type":"succeeded"}}],[{"custom_id":"b","result":{"type":"succeeded"}}]]}
JSON
if python3 "$ROOT/scripts/provider_batch_fetch.py" --marker "$TMP/marker.json" --test-transport "$TMP/batch-transport.json" --bundle "$TMP/bundle.json" \
  && python3 - "$TMP/bundle.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1]))
assert value["authority"] == "test-only"
assert [row["custom_id"] for row in value["results"]] == ["a", "b"]
assert len(value["raw"]) == 3
PY
then ok "provider batch adapter downloads every result page"; else bad "provider batch adapter downloads every result page"; fi

exit "$fails"
