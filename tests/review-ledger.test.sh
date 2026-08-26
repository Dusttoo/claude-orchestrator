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
trap 'rm -rf "$TMP"' EXIT
git -C "$TMP" init -q .
mkdir -p "$TMP/.orchestration"

led() { (cd "$TMP" && python3 "$LEDGER" "$@"); }
field() { python3 -c "import json,sys; v=json.load(sys.stdin)['$1']; print(','.join(v) if isinstance(v,list) else v)"; }

# --- key normalization --------------------------------------------------------
led open 1 >/dev/null
eq "line numbers are stripped from component keys" \
  "src/auth/session.ts:refreshtoken" \
  "$(led record 1 --gate code-review --verdict FAIL --blocking 'src/auth/session.ts:refreshToken:142' | field accepted_blocking)"
eq "the [component: ...] wrapper and casing normalize to the same key" \
  "src/auth/session.ts:refreshtoken" \
  "$(led record 1 --gate code-review --verdict FAIL --blocking '[component: SRC/auth/Session.ts:RefreshToken]' | field open_blocking)"
eq "the same defect named twice accumulates a second strike" \
  "redesign" "$(led status 1 | field next_action)"

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

# --- two strikes, redesign, and the cap ---------------------------------------
led open 5 --max-rounds 3 >/dev/null
led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
eq "a second strike on one component demands a redesign" \
  "redesign" "$(led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' | field next_action)"
eq "a passing design gate releases the component for another fix" \
  "review" "$(led redesign 5 --key 'src/a.ts:foo' --verdict PASS | field next_action)"
eq "spending the round cap with findings open stops the loop" \
  "escalate-human" "$(led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' | field next_action)"
if led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null 2>&1; then
  bad "an escalated ledger must refuse further rounds"
else ok "an escalated ledger refuses further rounds"; fi
led handoff 5 2>/dev/null | grep -q "Still blocking" && ok "handoff renders the human report" || bad "handoff renders the human report"

# --- the cap counts fix cycles, not review passes -----------------------------
# A PR running both gates records two passes per cycle. Counting raw passes would
# spend the budget of exactly the security-sensitive PRs that need it most.
led open 9 --max-rounds 2 >/dev/null
led record 9 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
led record 9 --gate security-review --verdict PASS >/dev/null
led record 9 --gate code-review --verdict PASS >/dev/null
eq "a passing gate does not spend a fix cycle" "1" "$(led status 9 | field fix_cycles)"
eq "three passes with one failure still clears" "gates-clear" "$(led status 9 | field next_action)"

# --- the clean path -----------------------------------------------------------
led open 6 >/dev/null
eq "a clean gate clears the loop" "gates-clear" "$(led record 6 --gate code-review --verdict PASS | field next_action)"
eq "advisory-only findings do not fail a gate" \
  "PASS" "$(led record 6 --gate code-review --verdict PASS --advisory 'src/x.ts:nit' | field effective_verdict)"

# --- structured reviewer results ---------------------------------------------
led open 10 >/dev/null
cat > "$TMP/review.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"FAIL","checks":[{"name":"tests","status":"fail"}],"findings":[{"component":"src/a.ts:parse","disposition":"blocking","severity":"high","title":"Missing rejection","explanation":"Invalid input reaches parse and is accepted; reject it and add the regression assertion.","regression":true}]}
JSON
eq "structured results populate the durable ledger" \
  "src/a.ts:parse" "$(led record 10 --gate code-review --result "$TMP/review.json" | field accepted_blocking)"
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
led brief 7 | grep -q "block-on-doubt\|treat it as BLOCKING" && ok "round 1 briefs block-on-doubt" || bad "round 1 briefs block-on-doubt"
led record 7 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
led record 7 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
led brief 7 | grep -q "ADVISORY and name the exact evidence" && ok "round 3 briefs advisory-on-doubt" || bad "round 3 briefs advisory-on-doubt"
led brief 7 | grep -q "REDESIGN REQUIRED" && ok "the brief flags a component needing redesign" || bad "the brief flags a component needing redesign"

# --- aliasing merges a drifted key --------------------------------------------
led open 8 >/dev/null
led record 8 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
led record 8 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --blocking 'src/a.ts:fooHelper' --regression 'src/a.ts:fooHelper' >/dev/null
eq "aliasing a drifted key merges its strikes" "3" "$(led alias 8 --from 'src/a.ts:fooHelper' --to 'src/a.ts:foo' | field strikes)"

echo
if [ "$fails" -eq 0 ]; then echo "review ledger tests passed"; else echo "$fails FAILED"; fi
exit "$fails"
