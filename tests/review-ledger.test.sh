#!/usr/bin/env bash
# review-ledger.test.sh -- the review loop must terminate and its blocking set
# must shrink. These are the properties that keep a PR from looping forever.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LEDGER="$ROOT/scripts/review-ledger.py"

fails=0
ok() { printf 'ok   %s\n' "$1"; }
bad() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else printf 'FAIL %s\n     want: [%s]\n     got:  [%s]\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi; }

TMP="$(mktemp -d)"
LANE="${TMP}-lane"
trap 'rm -rf "$TMP" "$LANE"' EXIT
git -C "$TMP" init -q .
git -C "$TMP" -c user.name=Test -c user.email=test@example.com commit --allow-empty -qm initial
mkdir -p "$TMP/.orchestration"

led() { (cd "$TMP" && python3 "$LEDGER" "$@"); }
field() { python3 -c "import json,sys; v=json.load(sys.stdin)['$1']; print(','.join(v) if isinstance(v,list) else v)"; }
review_record() {
  local pr="$1" gate="$2" file="$3" role
  role="${gate}-reviewer"
  local head permit
  head="$(git -C "$TMP" rev-parse HEAD)"
  permit="$(led permit-review "$pr" --role "$role" --head "$head" | field review_phase_permit)" || return
  led complete-review "$pr" --role "$role" --phase-permit "$permit" --result "$file" >/dev/null || return
  led record "$pr" --gate "$gate-review" --result "$file" --head "$head" --phase-permit "$permit"
}
record_pass() {
  local pr="$1" gate="$2" advisory="${3:-}" file
  file="$TMP/pass-$pr-$gate.json"
  python3 - "$file" "$gate" "$advisory" <<'PY'
import json,sys
findings=[]
if sys.argv[3]: findings=[{"component":sys.argv[3],"disposition":"advisory","severity":"low","title":"follow-up","explanation":"non-blocking follow-up","regression":False}]
json.dump({"schema_version":1,"gate":sys.argv[2]+"-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":findings},open(sys.argv[1],"w"))
PY
  review_record "$pr" "$gate" "$file"
}

git -C "$TMP" worktree add -qb review-ledger-lane "$LANE"
(cd "$LANE" && python3 "$LEDGER" open shared-pr >/dev/null)
eq "linked worktrees share one review ledger" "review" "$(led status shared-pr | field next_action)"

# --- key normalization --------------------------------------------------------
led open 1 >/dev/null
json_subject="$(led status 1 | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["work_subject"],sort_keys=True))')"
eq "a no-tracker PR owns an immutable repository-bound subject" \
  "{\"id\": \"1\", \"kind\": \"pr\", \"repository\": \"$(cd "$TMP" && pwd -P)\"}" "$json_subject"
if led open 1 --work-kind jira --work-id PROJ-1 >/dev/null 2>&1; then
  bad "an existing ledger work subject cannot be rebound"
else ok "an existing ledger work subject cannot be rebound"; fi
led open jira-work --work-kind jira --work-id proj-101 >/dev/null
eq "a Jira-backed ledger normalizes its work subject" "PROJ-101" \
  "$(led status jira-work | python3 -c 'import json,sys; print(json.load(sys.stdin)["work_subject"]["id"])')"
led open no-tracker-e2e >/dev/null
cat > "$TMP/no-tracker-pass.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":[]}
JSON
eq "a no-tracker PR completes permit, receipt, and record end to end" "gates-clear" \
  "$(review_record no-tracker-e2e code "$TMP/no-tracker-pass.json" | field next_action)"
eq "line numbers are stripped from component keys" \
  "src/auth/session.ts:refreshtoken" \
  "$(led record 1 --gate code-review --verdict FAIL --blocking 'src/auth/session.ts:refreshToken:142' | field accepted_blocking)"
eq "the [component: ...] wrapper and casing normalize to the same key" \
  "src/auth/session.ts:refreshtoken" \
  "$(led record 1 --gate code-review --verdict FAIL --blocking '[component: SRC/auth/Session.ts:RefreshToken]' | field open_blocking)"
eq "the same defect named twice accumulates a second strike" \
  "2" "$(led status 1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["components"]["src/auth/session.ts:refreshtoken"]["strikes"])')"
led open cap-test --max-rounds 1 >/dev/null
led open cap-test --max-rounds 99 >/dev/null
eq "worker CLI cannot raise a durable repair cap" "1" "$(led status cap-test | field max_rounds)"

led design-open 'free/form' >/dev/null
led design-open 'free-form' >/dev/null
eq "sanitized free-form ids retain exact collision-free identity" "free/form" \
  "$(led status 'free/form' | python3 -c 'import json,sys; print(json.load(sys.stdin)["work_subject"]["id"])')"
eq "colliding free-form ids own distinct ledgers" "free-form" \
  "$(led status 'free-form' | python3 -c 'import json,sys; print(json.load(sys.stdin)["work_subject"]["id"])')"

# --- round 1 has full blocking authority --------------------------------------
led open 2 >/dev/null
out="$(led record 2 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --blocking 'src/b.ts:bar')"
eq "round 1 accepts every blocking finding" "src/a.ts:foo,src/b.ts:bar" "$(printf '%s' "$out" | field accepted_blocking)"
eq "round 1 is recorded as full-authority scope" "full-authority" "$(printf '%s' "$out" | field scope_mode)"
eq "the next round is announced as scope-frozen" "scope-frozen" "$(printf '%s' "$out" | field next_scope_mode)"

# --- the scope freeze ---------------------------------------------------------
out="$(led record 2 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --blocking 'src/new.ts:nit')"
eq "a new non-regression finding is demoted in a frozen round" "src/new.ts:nit" "$(printf '%s' "$out" | field demoted_to_advisory)"
eq "a known component still blocks in a frozen round" "src/a.ts:foo" "$(printf '%s' "$out" | field accepted_blocking)"
eq "a component the gate stopped reporting auto-resolves" "src/b.ts:bar" "$(printf '%s' "$out" | field resolved_this_round)"
eq "the blocking set shrank" "src/a.ts:foo" "$(printf '%s' "$out" | field open_blocking)"

led open 3 >/dev/null
led record 3 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
eq "a declared regression keeps blocking authority in a frozen round" \
  "src/a.ts:foo,src/broke.ts:oops" \
  "$(led record 3 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --blocking 'src/broke.ts:oops' --regression 'src/broke.ts:oops' | field accepted_blocking)"

# --- the security gate is never scope-frozen ----------------------------------
led open 4 >/dev/null
led record 4 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
out="$(led record 4 --gate security-review --verdict FAIL --blocking 'src/rls/policy.sql:tenantIsolation')"
eq "a late security finding is never demoted" "src/rls/policy.sql:tenantisolation" "$(printf '%s' "$out" | field accepted_blocking)"
eq "a late security finding still fails the gate" "FAIL" "$(printf '%s' "$out" | field effective_verdict)"

led open gate-owned >/dev/null
led record gate-owned --gate code-review --verdict FAIL --blocking 'src/shared.py:check' >/dev/null
led record gate-owned --gate security-review --verdict FAIL --blocking 'src/shared.py:check' >/dev/null
eq "one gate cannot auto-resolve another gate claim" "src/shared.py:check" \
  "$(led record gate-owned --gate code-review --verdict FAIL | field open_blocking)"
eq "aggregate resolves only after every owning gate clears its claim" "" \
  "$(led record gate-owned --gate security-review --verdict FAIL | field open_blocking)"

led open concurrent-permits >/dev/null
HEAD_CONCURRENT="$(git -C "$TMP" rev-parse HEAD)"
CODE_PERMIT="$(led permit-review concurrent-permits --role code-reviewer --head "$HEAD_CONCURRENT" | field review_phase_permit)"
SEC_PERMIT="$(led permit-review concurrent-permits --role security-reviewer --head "$HEAD_CONCURRENT" | field review_phase_permit)"
cat > "$TMP/concurrent-code.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":[]}
JSON
cat > "$TMP/concurrent-security.json" <<'JSON'
{"schema_version":1,"gate":"security-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":[]}
JSON
led complete-review concurrent-permits --role code-reviewer --phase-permit "$CODE_PERMIT" --result "$TMP/concurrent-code.json" >/dev/null
led record concurrent-permits --gate code-review --result "$TMP/concurrent-code.json" --head "$HEAD_CONCURRENT" --phase-permit "$CODE_PERMIT" >/dev/null
if led complete-review concurrent-permits --role security-reviewer --phase-permit "$SEC_PERMIT" --result "$TMP/concurrent-security.json" >/dev/null \
  && led record concurrent-permits --gate security-review --result "$TMP/concurrent-security.json" --head "$HEAD_CONCURRENT" --phase-permit "$SEC_PERMIT" >/dev/null; then
  ok "concurrent gate permits remain completable and recordable in either order"
else
  bad "concurrent gate permits remain completable and recordable in either order"
fi

# --- explicit repairs, redesign, and the cap ----------------------------------
led open 5 --max-rounds 2 >/dev/null
led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head abcdef1 >/dev/null
led repair-brief 5 | grep -q 'stable finding ID' && ok "repair brief carries stable IDs" || bad "repair brief carries stable IDs"
cat > "$TMP/repair-1.json" <<'JSON'
{"schema_version":1,"head":"abcdef1","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"wrong branch","change":"corrected branch","verification":"named regression passes"}]}
JSON
eq "recording a repair starts a pending review" "True" "$(led record-repair 5 --report "$TMP/repair-1.json" | field repair_pending_review)"
if led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head abcdef2 >/dev/null 2>&1; then
  bad "a reviewer cannot record against the wrong repaired head"
else ok "a reviewer cannot record against the wrong repaired head"; fi
led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head abcdef1 >/dev/null
eq "a repaired head must complete its required gate set" \
  "redesign" "$(led complete-repair-review 5 | field next_action)"
eq "a passing design gate releases the component for another fix" \
  "review" "$(led redesign 5 --key 'src/a.ts:foo' --verdict PASS | field next_action)"
cat > "$TMP/repair-2.json" <<'JSON'
{"schema_version":1,"head":"abcdef2","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"boundary missed","change":"fixed boundary","verification":"boundary regression passes"}]}
JSON
led record-repair 5 --report "$TMP/repair-2.json" >/dev/null
led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head abcdef2 >/dev/null
eq "spending the round cap with findings open stops the loop" \
  "escalate-human" "$(led complete-repair-review 5 | field next_action)"
if led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null 2>&1; then
  bad "an escalated ledger must refuse further rounds"
else ok "an escalated ledger refuses further rounds"; fi
led handoff 5 2>/dev/null | grep -q "Still blocking" && ok "handoff renders the human report" || bad "handoff renders the human report"

# --- the cap counts explicit repairs, not review passes ------------------------
led open 9 --max-rounds 2 >/dev/null
led record 9 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
record_pass 9 security >/dev/null
record_pass 9 code >/dev/null
eq "review passes do not spend a repair cycle" "0" "$(led status 9 | field fix_cycles)"
eq "three passes without a repair can still clear" "gates-clear" "$(led status 9 | field next_action)"

# --- the clean path -----------------------------------------------------------
led open 6 >/dev/null
eq "a clean gate clears the loop" "gates-clear" "$(record_pass 6 code | field next_action)"
led open 11 >/dev/null
eq "advisory-only findings do not fail a gate" \
  "PASS" "$(record_pass 11 code 'src/x.ts:nit' | field effective_verdict)"

# --- structured reviewer results ---------------------------------------------
led open 10 >/dev/null
cat > "$TMP/review.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"FAIL","checks":[{"name":"tests","status":"fail"}],"findings":[{"component":"src/a.ts:parse","disposition":"blocking","severity":"high","title":"Missing rejection","explanation":"Invalid input reaches parse and is accepted; reject it and add the regression assertion.","regression":true}]}
JSON
eq "structured results populate the durable ledger" \
  "src/a.ts:parse" "$(review_record 10 code "$TMP/review.json" | field accepted_blocking)"
led handoff 10 | grep -q "Invalid input reaches parse" && ok "finding-only explanation survives handoff" || bad "finding-only explanation survives handoff"
if led record 10 --gate code-review --result "$TMP/review.json" --verdict FAIL >/dev/null 2>&1; then
  bad "structured and manual review inputs must not be mixed"
else ok "structured and manual review inputs cannot be mixed"; fi

# --- contradictions are rejected ----------------------------------------------
if led record 6 --gate code-review --verdict PASS --blocking 'src/a.ts:foo' >/dev/null 2>&1; then
  bad "a PASS listing blocking findings must be rejected"
else ok "a PASS listing blocking findings is rejected"; fi

# --- round-aware guidance -----------------------------------------------------
led open 7 >/dev/null
led brief 7 | grep -q 'JSON `component` field to the bare `<path>:<symbol>` key' && ok "review brief requests a bare JSON component key" || bad "review brief requests a bare JSON component key"
if led brief 7 | grep -q 'Key every finding as `\[component:'; then
  bad "review brief does not instruct reviewers to wrap JSON component keys"
else ok "review brief does not instruct reviewers to wrap JSON component keys"; fi
led brief 7 | grep -q "block-on-doubt\|treat it as BLOCKING" && ok "round 1 briefs block-on-doubt" || bad "round 1 briefs block-on-doubt"
led record 7 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head abcdef7 >/dev/null
cat > "$TMP/repair-7.json" <<'JSON'
{"schema_version":1,"head":"abcdef7","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"bad condition","change":"fixed condition","verification":"regression passes"}]}
JSON
led record-repair 7 --report "$TMP/repair-7.json" >/dev/null
led record 7 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head abcdef7 >/dev/null
led complete-repair-review 7 >/dev/null
led brief 7 | grep -q "ADVISORY and name the exact evidence" && ok "round 3 briefs advisory-on-doubt" || bad "round 3 briefs advisory-on-doubt"
led brief 7 | grep -q "REDESIGN REQUIRED" && ok "the brief flags a component needing redesign" || bad "the brief flags a component needing redesign"

# --- pre-code design rounds have their own durable cap -------------------------
led design-open BL-1 --max-design-rounds 2 >/dev/null
eq "a failed design returns to redesign" "redesign" "$(led design-record BL-1 --verdict FAIL --evidence 'boundary incomplete' | field next_action)"
eq "the independent design cap escalates" "escalate-human" "$(led design-record BL-1 --verdict FAIL --evidence 'boundary still incomplete' | field next_action)"

DESIGN_ID='free form architecture'
led design-open "$DESIGN_ID" >/dev/null
eq "a free-form design owns a design subject" "design" \
  "$(led status "$DESIGN_ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["work_subject"]["kind"])')"
HEAD_SHA="$(git -C "$TMP" rev-parse HEAD)"
printf 'reviewed boundary\n' > "$TMP/design-free-form.md"
ARTIFACT_SHA="$(shasum -a 256 "$TMP/design-free-form.md" | awk '{print $1}')"
PERMIT="$(led permit-review "$DESIGN_ID" --role design-reviewer --head "$HEAD_SHA" | python3 -c 'import json,sys; print(json.load(sys.stdin)["review_phase_permit"])')"
cat > "$TMP/design-pass.json" <<JSON
{"schema_version":1,"gate":"design-review","verdict":"PASS","source_sha":"$HEAD_SHA","artifact":"design-free-form.md","artifact_sha256":"$ARTIFACT_SHA","phase_permit":"$PERMIT","checks":[{"name":"trust-boundary","status":"pass"}]}
JSON
led complete-review "$DESIGN_ID" --role design-reviewer --phase-permit "$PERMIT" --result "$TMP/design-pass.json" >/dev/null
python3 - "$TMP/design-pass.json" "$TMP/design-short.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1])); value['source_sha']=value['source_sha'][:12]
json.dump(value, open(sys.argv[2], 'w'))
PY
if led design-record "$DESIGN_ID" --result "$TMP/design-short.json" >/dev/null 2>&1; then
  bad "abbreviated design source SHA must fail closed"
else ok "abbreviated design source SHA fails closed"; fi
python3 - "$TMP/design-pass.json" "$TMP/design-bad-digest.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1])); value['artifact_sha256']='0'*64
json.dump(value, open(sys.argv[2], 'w'))
PY
if led design-record "$DESIGN_ID" --result "$TMP/design-bad-digest.json" >/dev/null 2>&1; then
  bad "mismatched design artifact digest must fail closed"
else ok "mismatched design artifact digest fails closed"; fi
eq "free-form design PASS completes end to end" "implement" "$(led design-record "$DESIGN_ID" --result "$TMP/design-pass.json" | field next_action)"
if led permit-review "$DESIGN_ID" --role design-reviewer --head "$HEAD_SHA" >/dev/null 2>&1; then
  bad "passed design phase must not mint another reviewer permit"
else ok "passed design phase cannot mint another reviewer permit"; fi
led design-handoff BL-1 | grep -q 'No production implementation is authorized' && ok "design handoff blocks implementation" || bad "design handoff blocks implementation"

# --- aliasing merges a drifted key --------------------------------------------
led open 8 >/dev/null
led record 8 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
led record 8 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --blocking 'src/a.ts:fooHelper' --regression 'src/a.ts:fooHelper' >/dev/null
eq "aliasing a drifted key merges its strikes" "3" "$(led alias 8 --from 'src/a.ts:fooHelper' --to 'src/a.ts:foo' | field strikes)"

# --- v0.7 ledgers preserve their already-spent budget -------------------------
cat > "$TMP/.orchestration/.review-ledger/pr-legacy.json" <<'JSON'
{"schema_version":1,"pr":"legacy","created_at":"2026-01-01T00:00:00+00:00","updated_at":"2026-01-01T00:00:00+00:00","max_rounds":2,"rounds":[{"round":1,"gate":"code-review","scope_mode":"full-authority","claimed_verdict":"FAIL","effective_verdict":"FAIL","recorded_at":"2026-01-01T00:00:00+00:00","blocking":["src/a.ts:foo"],"advisory":[],"resolved":[]}],"components":{"src/a.ts:foo":{"key":"src/a.ts:foo","display":"src/a.ts:foo","strikes":1,"status":"open","first_round":1,"last_round":1,"rounds":[1],"gates":["code-review"],"redesigned_at_strike":0}},"escalated":false}
JSON
eq "v0.7 failed passes retain their spent repair budget" "1" "$(led status legacy | field fix_cycles)"
if led --ledger-dir "$TMP/fresh-ledger" open escape >/dev/null 2>&1; then
  bad "absolute review ledger override must fail closed"
else ok "absolute review ledger override fails closed"; fi

echo
if [ "$fails" -eq 0 ]; then echo "review ledger tests passed"; else echo "$fails FAILED"; fi
exit "$fails"
