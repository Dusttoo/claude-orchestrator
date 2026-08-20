#!/usr/bin/env bash
# merge-on-green.test.sh -- tests for the safe-merge guard rails that run before
# any network call: the refuse-unless-green check and the merge lock. The actual
# `gh pr merge` + fetch/verify path needs a live remote and is covered by the
# integration run, not here.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fails=0
assert_exit() { # <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then printf 'ok   %s (exit %s)\n' "$1" "$3"
  else printf 'FAIL %s: want exit %s, got %s\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo/.orchestration" "$TMP/.codex-plugin" "$TMP/.claude-plugin" "$TMP/fake-gh-bin"
cp "$HERE"/../scripts/lib-config.sh "$HERE"/../scripts/merge-guard.sh \
   "$HERE"/../scripts/merge-command-classifier.py \
   "$HERE"/../scripts/merge-on-green.sh "$HERE"/../scripts/orchestration-engine.py \
   "$TMP/repo/"
cp "$HERE/../.codex-plugin/plugin.json" "$TMP/.codex-plugin/plugin.json"
cp "$HERE/../.claude-plugin/plugin.json" "$TMP/.claude-plugin/plugin.json"
cat > "$TMP/fake-gh-bin/gh" <<'SH'
#!/usr/bin/env bash
if [ "${1:-} ${2:-}" = "repo view" ]; then printf '%s\n' 'o/r'; exit 0; fi
if [ -n "${GH_REPO:-}" ] || [ -n "${GH_HOST:-}" ]; then exit 91; fi
repo=""; repo_count=0; previous=""
for arg in "$@"; do
  if [ "$previous" = "--repo" ]; then repo="$arg"; repo_count=$((repo_count + 1)); fi
  case "$arg" in --repo=*) repo="${arg#--repo=}"; repo_count=$((repo_count + 1)) ;; esac
  previous="$arg"
done
[ "$repo_count" -eq 1 ] && [ "$repo" = "o/r" ] || exit 92
if [ "${1:-}" = pr ] && [ "${2:-}" = merge ]; then
  pin=""; previous=""
  for arg in "$@"; do
    if [ "$previous" = "--match-head-commit" ]; then pin="$arg"; fi
    previous="$arg"
  done
  if [ -z "$pin" ]; then touch "${TEST_GH_MERGE_SIDE_EFFECT:?}"; exit 0; fi
  [ "$pin" = "${TEST_GH_MERGE_LIVE_HEAD:-${TEST_GH_HEAD_SHA}}" ] || exit 1
  touch "${TEST_GH_MERGE_SIDE_EFFECT:?}"
  exit 0
fi
count_file="${TEST_GH_STATE_DIR:?}/view-count"
count="$(cat "$count_file" 2>/dev/null || printf 0)"
count=$((count + 1)); printf '%s\n' "$count" > "$count_file"
head_sha="${TEST_GH_HEAD_SHA:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
base_sha="${TEST_GH_BASE_SHA:-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}"
if [ -n "${TEST_GH_MOVE_AFTER_CALL:-}" ] && [ "$count" -ge "$TEST_GH_MOVE_AFTER_CALL" ]; then
  head_sha="${TEST_GH_MOVED_HEAD:-$head_sha}"
  base_sha="${TEST_GH_MOVED_BASE:-$base_sha}"
fi
printf '%s\t%s\t%s\t%s\n' \
  "${TEST_GH_HEAD_BRANCH:-feat/x}" \
  "$head_sha" \
  "${TEST_GH_BASE_BRANCH:-develop}" \
  "$base_sha"
SH
chmod +x "$TMP/fake-gh-bin/gh"
export PATH="$TMP/fake-gh-bin:$PATH"
cat > "$TMP/repo/.orchestration/config.yaml" <<'YAML'
integration_branch: develop
production_branch: main
merge_to_integration: merge
verification:
  - name: full-suite
    run: 'true'
YAML
cd "$TMP/repo" && git init -q
git config user.email t@t.t
git config user.name t
git add -A
git commit -qm init
git branch -M develop
git init -q --bare "$TMP/origin.git"
git remote add origin "$TMP/origin.git"
git push -q -u origin develop
MOG="$TMP/repo/merge-on-green.sh"
export MERGE_GUARD_STATUS_DIR="$TMP/repo/.orchestration/.gate-status"
export TEST_GH_HEAD_BRANCH="feat/x"
export TEST_GH_HEAD_SHA="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
export TEST_GH_BASE_BRANCH="develop"
export TEST_GH_BASE_SHA="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
export TEST_GH_MERGE_SIDE_EFFECT="$TMP/merge-side-effect"
export TEST_GH_STATE_DIR="$TMP/gh-state"
mkdir -p "$TEST_GH_STATE_DIR"

# 1. A non-"all-green" gate status is refused before anything else happens.
bash "$MOG" 42 feat/x not-green >/dev/null 2>&1
assert_exit "refuses when gate is not all-green" 2 "$?"

# Remaining pre-network cases need valid host-neutral evidence; no hook is
# involved. This proves merge-on-green performs the assertion directly.
RESULT="$MERGE_GUARD_STATUS_DIR/verify-full-suite-${TEST_GH_HEAD_SHA}.green"
mkdir -p "$MERGE_GUARD_STATUS_DIR"
printf 'result=GREEN\nname=full-suite\nbranch=feat/x\nsha=%s\nat=%s\n' "$TEST_GH_HEAD_SHA" "$(date -u +%FT%TZ)" > "$RESULT"
bash "$TMP/repo/merge-guard.sh" --record-green 42 "$RESULT" >/dev/null 2>&1
IDENTITY_SNAPSHOT="$TMP/identity-snapshot"
bash "$TMP/repo/merge-guard.sh" --assert-green 42 feat/x "$IDENTITY_SNAPSHOT" >/dev/null 2>&1
if [ "$(wc -l < "$IDENTITY_SNAPSHOT" | tr -d ' ')" = 5 ] \
   && [ "$(sed -n '1p' "$IDENTITY_SNAPSHOT")" = o/r ]; then
  printf 'ok   assertion publishes an exact five-record repository snapshot\n'
else
  printf 'FAIL assertion snapshot is not exact or repository-bound\n'; fails=$((fails + 1))
fi

# 2. When the merge lock is already held, a second merge is refused with 75
#    (EX_TEMPFAIL) and does not disturb the existing lock.
echo "pid=1 pr=1 held" > "$TMP/repo/.git/orchestrator-merge.lock"
bash "$MOG" 42 feat/x all-green >/dev/null 2>&1
assert_exit "refuses when merge lock is held" 75 "$?"
grep -q "pid=1 pr=1 held" "$TMP/repo/.git/orchestrator-merge.lock" \
  && printf 'ok   existing lock left intact\n' \
  || { printf 'FAIL existing lock was disturbed\n'; fails=$((fails + 1)); }
rm "$TMP/repo/.git/orchestrator-merge.lock"

# 3. A linked worktree has a .git file, not a directory. Its merge wrapper must
#    use the shared Git common directory and observe a lock created elsewhere.
git worktree add -q --detach "$TMP/worktree"
echo "pid=1 pr=1 held-from-primary" > "$TMP/repo/.git/orchestrator-merge.lock"
cd "$TMP/worktree"
bash "$MOG" 42 feat/x all-green >/dev/null 2>&1
assert_exit "linked worktree observes shared merge lock" 75 "$?"
rm "$TMP/repo/.git/orchestrator-merge.lock"
cd "$TMP/repo"

# 4. Regression: the merge must not use --delete-branch (that couples branch
#    cleanup to the merge and, under set -e, aborts a verified merge when a
#    worktree still holds the branch), and branch deletion must be best-effort.
SRC="$HERE/../scripts/merge-on-green.sh"
grep -Eq 'env -u GH_REPO -u GH_HOST gh pr merge "\$PR" --repo "\$REPOSITORY" "\$MERGE_FLAG" --match-head-commit "\$HEAD_SHA"[[:space:]]*$' "$SRC" \
  && printf 'ok   merge invocation is exact-head pinned\n' \
  || { printf 'FAIL merge invocation is not exact-head pinned\n'; fails=$((fails + 1)); }
grep -Eq 'git push origin --delete "\$BRANCH".*\|\| true' "$SRC" \
  && grep -Eq 'git branch -D "\$BRANCH".*\|\| true' "$SRC" \
  && printf 'ok   branch deletion is best-effort (remote + local, tolerant)\n' \
  || { printf 'FAIL branch deletion is not best-effort\n'; fails=$((fails + 1)); }
grep -Eq 'merge-guard\.sh" --assert-green "\$PR" "\$BRANCH" "\$SNAPSHOT"' "$SRC" \
  && printf 'ok   sanctioned merge validates evidence without hooks\n' \
  || { printf 'FAIL sanctioned merge still depends on host hooks\n'; fails=$((fails + 1)); }
grep -Eq 'env -u GH_REPO -u GH_HOST gh pr view "\$PR" --repo "\$REPOSITORY"' "$SRC" \
  && printf 'ok   final identity read is repository-bound and host-neutral\n' \
  || { printf 'FAIL final identity read is not repository-bound and host-neutral\n'; fails=$((fails + 1)); }

# The lock covers authoritative assertion through the merge. A push after the
# final read is rejected by GitHub's exact-head primitive and has no merge side
# effect.
rm -f "$TEST_GH_MERGE_SIDE_EFFECT"
printf '0\n' > "$TEST_GH_STATE_DIR/view-count"
export TEST_GH_MOVE_AFTER_CALL=2
export TEST_GH_MOVED_HEAD="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
bash "$MOG" 42 feat/x all-green >/dev/null 2>&1
assert_exit "head movement at final read is rejected before merge" 2 "$?"
if [ ! -e "$TEST_GH_MERGE_SIDE_EFFECT" ]; then printf 'ok   pre-merge head movement has no merge side effect\n'
else printf 'FAIL pre-merge head movement produced a merge side effect\n'; fails=$((fails + 1)); fi
unset TEST_GH_MOVED_HEAD
printf '0\n' > "$TEST_GH_STATE_DIR/view-count"
export TEST_GH_MOVED_BASE="dddddddddddddddddddddddddddddddddddddddd"
bash "$MOG" 42 feat/x all-green >/dev/null 2>&1
assert_exit "base movement at final read is rejected before merge" 2 "$?"
if [ ! -e "$TEST_GH_MERGE_SIDE_EFFECT" ]; then printf 'ok   pre-merge movement has no merge side effect\n'
else printf 'FAIL pre-merge movement produced a merge side effect\n'; fails=$((fails + 1)); fi
unset TEST_GH_MOVE_AFTER_CALL TEST_GH_MOVED_BASE
printf '0\n' > "$TEST_GH_STATE_DIR/view-count"
export TEST_GH_MERGE_LIVE_HEAD="cccccccccccccccccccccccccccccccccccccccc"
export GH_REPO=other/r GH_HOST=example.com
bash "$MOG" 42 feat/x all-green >/dev/null 2>&1
assert_exit "movement after final read is rejected" 1 "$?"
if [ ! -e "$TEST_GH_MERGE_SIDE_EFFECT" ]; then printf 'ok   rejected race has no merge side effect\n'
else printf 'FAIL rejected race produced a merge side effect\n'; fails=$((fails + 1)); fi
unset GH_REPO GH_HOST

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
