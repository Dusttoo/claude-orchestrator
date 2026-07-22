#!/usr/bin/env bash
# worktree.test.sh -- tests for the worktree lifecycle scripts, focused on the
# two that DELETE things (cleanup-worktree, sweep-agent-worktrees). Both must
# never remove a worktree with uncommitted work. Uses real git worktrees.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$HERE/../scripts"

fails=0
check() { # <desc> <cond-cmd...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then printf 'ok   %s\n' "$desc"
  else printf 'FAIL %s\n' "$desc"; fails=$((fails + 1)); fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# A real origin so worktrees have a base branch.
git init -q --bare "$TMP/origin.git"
git clone -q "$TMP/origin.git" "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t.t; git config user.name t
mkdir -p .orchestration
printf 'integration_branch: main\nworktree_base: .claude/worktrees\n' > .orchestration/config.yaml
git checkout -q -b main
git add -A; git commit -qm init; git push -q origin main

# ---- cleanup-worktree: refuses a dirty tree, removes a clean one -------------
git worktree add -q "$TMP/repo/../repo-worktrees/wt-dirty" -b wt-dirty >/dev/null 2>&1
echo "scratch" > "$TMP/repo/../repo-worktrees/wt-dirty/uncommitted.txt"
bash "$SCRIPTS/cleanup-worktree.sh" "$TMP/repo/../repo-worktrees/wt-dirty" >/dev/null 2>&1
rc=$?
check "cleanup refuses a dirty worktree (exit 1)" test "$rc" -eq 1
check "cleanup left the dirty worktree in place" test -d "$TMP/repo/../repo-worktrees/wt-dirty"

git worktree add -q "$TMP/repo/../repo-worktrees/wt-clean" -b wt-clean >/dev/null 2>&1
bash "$SCRIPTS/cleanup-worktree.sh" "$TMP/repo/../repo-worktrees/wt-clean" >/dev/null 2>&1
check "cleanup removed a clean worktree (exit 0)" test "$?" -eq 0
check "cleanup deleted the clean worktree dir" test ! -d "$TMP/repo/../repo-worktrees/wt-clean"

# ---- sweep: removes a clean agent-* worktree, preserves a dirty one ----------
mkdir -p .claude/worktrees
git worktree add -q ".claude/worktrees/agent-clean" -b agent-clean >/dev/null 2>&1
git worktree add -q ".claude/worktrees/agent-dirty" -b agent-dirty >/dev/null 2>&1
echo "wip" > ".claude/worktrees/agent-dirty/wip.txt"
# A non-agent worktree must be ignored entirely.
git worktree add -q ".claude/worktrees/session-keep" -b session-keep >/dev/null 2>&1

WT_QUIET=1 bash "$SCRIPTS/sweep-agent-worktrees.sh" >/dev/null 2>&1
check "sweep removed the clean agent worktree"      test ! -d ".claude/worktrees/agent-clean"
check "sweep preserved the dirty agent worktree"    test   -d ".claude/worktrees/agent-dirty"
check "sweep ignored the non-agent worktree"        test   -d ".claude/worktrees/session-keep"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
