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
mkdir -p "$TMP/repo/.orchestration"
cp "$HERE"/../scripts/lib-config.sh "$HERE"/../scripts/merge-guard.sh \
   "$HERE"/../scripts/merge-on-green.sh "$TMP/repo/"
printf 'integration_branch: develop\nmerge_to_integration: merge\n' > "$TMP/repo/.orchestration/config.yaml"
cd "$TMP/repo" && git init -q
MOG="$TMP/repo/merge-on-green.sh"

# 1. A non-"all-green" gate status is refused before anything else happens.
bash "$MOG" 42 feat/x not-green >/dev/null 2>&1
assert_exit "refuses when gate is not all-green" 2 "$?"

# 2. When the merge lock is already held, a second merge is refused with 75
#    (EX_TEMPFAIL) and does not disturb the existing lock.
echo "pid=1 pr=1 held" > "$TMP/repo/.git/orchestrator-merge.lock"
bash "$MOG" 42 feat/x all-green >/dev/null 2>&1
assert_exit "refuses when merge lock is held" 75 "$?"
grep -q "pid=1 pr=1 held" "$TMP/repo/.git/orchestrator-merge.lock" \
  && printf 'ok   existing lock left intact\n' \
  || { printf 'FAIL existing lock was disturbed\n'; fails=$((fails + 1)); }

# 3. Regression: the merge must not use --delete-branch (that couples branch
#    cleanup to the merge and, under set -e, aborts a verified merge when a
#    worktree still holds the branch), and branch deletion must be best-effort.
SRC="$HERE/../scripts/merge-on-green.sh"
grep -Eq 'gh pr merge "\$PR" "\$MERGE_FLAG"[[:space:]]*$' "$SRC" \
  && printf 'ok   merge invocation has no --delete-branch\n' \
  || { printf 'FAIL merge invocation still couples --delete-branch\n'; fails=$((fails + 1)); }
grep -Eq 'git push origin --delete "\$BRANCH".*\|\| true' "$SRC" \
  && grep -Eq 'git branch -D "\$BRANCH".*\|\| true' "$SRC" \
  && printf 'ok   branch deletion is best-effort (remote + local, tolerant)\n' \
  || { printf 'FAIL branch deletion is not best-effort\n'; fails=$((fails + 1)); }

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
