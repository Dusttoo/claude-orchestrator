#!/usr/bin/env bash
# run-verification.test.sh -- tests the generic verification runner and its
# handshake with merge-guard --record-green: a GREEN run writes a sha-stamped
# result file, a RED run writes none, and --record-green accepts the file only
# when its sha matches the PR head.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fails=0
assert_exit() { [ "$2" = "$3" ] && printf 'ok   %s (exit %s)\n' "$1" "$3" \
  || { printf 'FAIL %s: want %s got %s\n' "$1" "$2" "$3"; fails=$((fails + 1)); }; }
assert_true() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then printf 'ok   %s\n' "$d"; \
  else printf 'FAIL %s\n' "$d"; fails=$((fails + 1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo/.orchestration" "$TMP/.codex-plugin" "$TMP/.claude-plugin" "$TMP/fake-gh-bin"
cp "$HERE"/../scripts/lib-config.sh "$HERE"/../scripts/run-verification.sh \
   "$HERE"/../scripts/merge-guard.sh "$HERE"/../scripts/merge-command-classifier.py \
   "$HERE"/../scripts/orchestration-engine.py \
   "$TMP/repo/"
cp "$HERE/../.codex-plugin/plugin.json" "$TMP/.codex-plugin/plugin.json"
cp "$HERE/../.claude-plugin/plugin.json" "$TMP/.claude-plugin/plugin.json"
cat > "$TMP/fake-gh-bin/gh" <<'SH'
#!/usr/bin/env bash
if [ "${1:-} ${2:-}" = "repo view" ]; then printf '%s\n' 'o/r'; exit 0; fi
printf '%s\t%s\t%s\t%s\n' \
  "${TEST_GH_HEAD_BRANCH:-feat/verify}" \
  "${TEST_GH_HEAD_SHA}" \
  "${TEST_GH_BASE_BRANCH:-develop}" \
  "${TEST_GH_BASE_SHA:-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}"
SH
chmod +x "$TMP/fake-gh-bin/gh"
export PATH="$TMP/fake-gh-bin:$PATH"
cat > "$TMP/repo/.orchestration/config.yaml" <<'YAML'
integration_branch: develop
production_branch: main
verification:
  - name: green-check
    run: 'true'
  - name: red-check
    run: 'false'
  - name: secret-check
    run: 'true # super-secret-token'
YAML
cd "$TMP/repo" && git init -q >/dev/null
echo x > f; git add -A; git -c user.email=t@t.t -c user.name=t commit -qm init
git branch -m feat/verify
export GATE_STATUS_DIR="$TMP/markers"
export MERGE_GUARD_STATUS_DIR="$TMP/markers"
export TEST_GH_HEAD_BRANCH="feat/verify"
export TEST_GH_BASE_BRANCH="develop"
export TEST_GH_BASE_SHA="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SHA="$(git rev-parse HEAD)"
export TEST_GH_HEAD_SHA="$SHA"

# 1. A GREEN verification exits 0 and writes a sha-stamped result file.
bash run-verification.sh green-check >/dev/null 2>&1
assert_exit "green verification exits 0" 0 "$?"
RESULT="$TMP/markers/verify-green-check-${SHA}.green"
assert_true "green verification wrote its result file" test -f "$RESULT"

# 2. A RED verification exits 1 and writes NO result file.
printf 'result=GREEN\n' > "$TMP/markers/verify-red-check-${SHA}.green"
bash run-verification.sh red-check >/dev/null 2>&1
assert_exit "red verification exits 1" 1 "$?"
assert_true "red verification wrote no result file" test ! -e "$TMP/markers/verify-red-check-${SHA}.green"

# 3. An unknown verification name is refused.
bash run-verification.sh nope >/dev/null 2>&1
assert_exit "unknown verification refused" 2 "$?"

# 4. --record-green accepts the matching result file (sha == head).
TEST_GH_HEAD_SHA="$SHA" bash merge-guard.sh --record-green 7 "$RESULT" >/dev/null 2>&1
assert_exit "record-green accepts matching result file" 0 "$?"
assert_true "marker snapshots verification provenance" grep -q "verification_file=" "$TMP/markers/pr-7.green"
assert_true "marker snapshots repository provenance" grep -q " repo=o/r " "$TMP/markers/pr-7.green"
MARKER_MODE="$(stat -f %Lp "$TMP/markers/pr-7.green" 2>/dev/null || stat -c %a "$TMP/markers/pr-7.green")"
assert_true "marker permissions are restrictive" test "$MARKER_MODE" = 600

# 5. --record-green refuses a result file whose sha != PR head.
printf 'all-green stale\n' > "$TMP/markers/pr-8.green"
TEST_GH_HEAD_SHA="dddddddddddddddddddddddddddddddddddddddd" bash merge-guard.sh --record-green 8 "$RESULT" >/dev/null 2>&1
assert_exit "record-green refuses mismatched result file" 2 "$?"
assert_true "no marker written on mismatch" test ! -e "$TMP/markers/pr-8.green"

# 6. Bare recording is forbidden and removes any prior marker.
printf 'all-green stale\n' > "$TMP/markers/pr-9.green"
TEST_GH_HEAD_SHA="$SHA" bash merge-guard.sh --record-green 9 >/dev/null 2>&1
assert_exit "record-green requires a result artifact" 2 "$?"
assert_true "failed bare record removes prior marker" test ! -e "$TMP/markers/pr-9.green"

# 7. Complete contract: real runner artifact -> record -> assert -> head movement.
TEST_GH_HEAD_SHA="$SHA" bash merge-guard.sh --record-green 10 "$RESULT" >/dev/null 2>&1
assert_exit "end-to-end record succeeds for exact head" 0 "$?"
TEST_GH_HEAD_SHA="$SHA" bash merge-guard.sh --assert-green 10 feat/verify >/dev/null 2>&1
assert_exit "end-to-end marker validates" 0 "$?"
TEST_GH_HEAD_SHA="0000000000000000000000000000000000000000" bash merge-guard.sh --assert-green 10 feat/verify >/dev/null 2>&1
assert_exit "end-to-end moved head is rejected" 2 "$?"

# The marker is an atomic verification snapshot; deleting the source artifact
# after successful recording does not invalidate it, but source movement does.
TEST_GH_HEAD_SHA="$SHA" bash merge-guard.sh --record-green 11 "$RESULT" >/dev/null 2>&1
rm "$RESULT"
TEST_GH_HEAD_SHA="$SHA" bash merge-guard.sh --assert-green 11 feat/verify >/dev/null 2>&1
assert_exit "marker remains valid after artifact deletion" 0 "$?"

# Publication is fail-closed and never leaves a result that can be mistaken for
# GREEN. The completed artifact is owner-only and exactly five records.
bash run-verification.sh green-check >/dev/null 2>&1
RESULT="$TMP/markers/verify-green-check-${SHA}.green"
assert_true "published artifact has five exact records" test "$(wc -l < "$RESULT" | tr -d ' ')" = 5
MODE="$(stat -f %Lp "$RESULT" 2>/dev/null || stat -c %a "$RESULT")"
assert_true "published artifact permissions are restrictive" test "$MODE" = 600
rm -f "$RESULT"
RUN_VERIFICATION_FORCE_PUBLISH_FAILURE=1 bash run-verification.sh green-check >/dev/null 2>&1
assert_exit "artifact publication failure is RED" 1 "$?"
assert_true "publication failure leaves no GREEN artifact" test ! -e "$RESULT"
BAD_STATUS="$TMP/not-a-directory"; printf x > "$BAD_STATUS"
GATE_STATUS_DIR="$BAD_STATUS" bash run-verification.sh green-check >/dev/null 2>&1
assert_exit "unavailable result directory fails closed" 2 "$?"
SECRET_OUTPUT="$(bash run-verification.sh secret-check 2>&1)"
assert_exit "configured command verification succeeds" 0 "$?"
assert_true "runner output does not expose configured command secrets" sh -c '! printf "%s" "$1" | grep -q super-secret-token' sh "$SECRET_OUTPUT"

# Concurrent writers publish only complete artifacts. Readers may observe the
# intentionally absent interval after invalidation, but never a partial file.
RESULT="$TMP/markers/verify-green-check-${SHA}.green"
for _ in 1 2 3 4; do bash run-verification.sh green-check >/dev/null 2>&1 & done
PARTIAL=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if [ -f "$RESULT" ]; then
    lines="$(wc -l < "$RESULT" | tr -d ' ')"
    [ "$lines" = 5 ] && grep -q '^result=GREEN$' "$RESULT" && grep -q '^at=.*Z$' "$RESULT" \
      || PARTIAL=1
  fi
done
wait
assert_exit "concurrent readers never observe partial artifact" 0 "$PARTIAL"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
