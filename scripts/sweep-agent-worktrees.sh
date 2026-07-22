#!/usr/bin/env bash
# sweep-agent-worktrees.sh -- Claude Code Stop hook. Removes FINISHED subagent
# worktrees so the fleet never bloats and starves the machine.
#
# SAFE BY CONSTRUCTION. It removes a worktree only when ALL of these hold:
#   * path is under <worktree_base>/agent-*  (an ephemeral subagent worktree --
#     NOT the random-word session worktrees of other sessions, NOT the main repo)
#   * NOT locked        -- the harness locks a worktree while its agent is live,
#                          so unlocked == the agent has finished or died
#   * clean working tree -- no uncommitted changes, so nothing is lost. A dead
#                          agent mid-edit leaves a DIRTY tree and is PRESERVED for
#                          recovery; a committed+pushed branch survives as a ref
#                          even after its worktree dir is removed.
#
# Removing a clean worktree never loses commits (branch refs live in the shared
# .git) and PRs are reviewed from a fresh `gh pr checkout`, so this is lossless.
#
# Usage: sweep-agent-worktrees.sh             # sweep
#        WT_QUIET=1 sweep-agent-worktrees.sh  # only print if something was removed
# Always exits 0 (safe as a hook); tolerant of concurrent sessions.
set -uo pipefail

GIT="$(command -v git 2>/dev/null)" || exit 0
ROOT="$("$GIT" rev-parse --show-toplevel 2>/dev/null)" || exit 0

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-config.sh
. "$HERE/lib-config.sh" 2>/dev/null || true
WTB="$(orch_get worktree_base .claude/worktrees 2>/dev/null || printf '%s' .claude/worktrees)"

removed=0
swept_list=""

# path<TAB>locked from porcelain (a `locked` line follows the worktree line).
while IFS=$'\t' read -r wt lk; do
  case "$wt" in
    */"$WTB"/agent-*) : ;;               # subagent worktrees ONLY
    *) continue ;;
  esac
  [ "$lk" = "1" ] && continue            # locked == active agent -> skip
  if [ ! -d "$wt" ]; then                # stale registration, just prune
    "$GIT" -C "$ROOT" worktree prune 2>/dev/null || true
    continue
  fi
  # uncommitted work -> preserve (this is how a dead-mid-edit agent is rescued)
  if [ -n "$("$GIT" -C "$wt" status --porcelain 2>/dev/null)" ]; then
    continue
  fi
  if "$GIT" -C "$ROOT" worktree remove --force "$wt" >/dev/null 2>&1; then
    removed=$((removed + 1))
    swept_list="${swept_list}  swept: ${wt#"$ROOT"/}"$'\n'
  fi
done < <("$GIT" -C "$ROOT" worktree list --porcelain 2>/dev/null | awk '
  /^worktree /{ if (p != "") print p "\t" lk; p = substr($0, 10); lk = 0 }
  /^locked/  { lk = 1 }
  END        { if (p != "") print p "\t" lk }')

"$GIT" -C "$ROOT" worktree prune 2>/dev/null || true

if [ "$removed" -gt 0 ]; then
  printf 'sweep-agent-worktrees: removed %d finished subagent worktree(s)\n%s' "$removed" "$swept_list"
elif [ -z "${WT_QUIET:-}" ]; then
  echo "sweep-agent-worktrees: nothing to sweep (no unlocked+clean agent-* worktrees)"
fi
exit 0
