#!/usr/bin/env bash
# cleanup-worktree.sh -- remove a worktree after its PR has merged (or been
# abandoned). Refuses to remove a worktree with uncommitted changes, so a
# half-finished branch is never silently lost.
#
# Usage:
#   cleanup-worktree.sh <worktree_path>
set -euo pipefail

WT="${1:?usage: cleanup-worktree.sh <worktree_path>}"
REPO_ROOT="$(git rev-parse --show-toplevel)"

if [ ! -d "$WT" ]; then
  echo "no such worktree: $WT (already cleaned up?)"; exit 0
fi

# Refuse to remove a worktree with uncommitted changes, to avoid losing work.
if [ -n "$(git -C "$WT" status --porcelain 2>/dev/null)" ]; then
  echo "REFUSED: $WT has uncommitted changes. Commit/push or remove manually:" >&2
  git -C "$WT" status --short >&2
  echo "  git worktree remove --force \"$WT\"   # only if you are sure the work is saved" >&2
  exit 1
fi

git -C "$REPO_ROOT" worktree remove "$WT"
git -C "$REPO_ROOT" worktree prune
echo "removed worktree: $WT"
