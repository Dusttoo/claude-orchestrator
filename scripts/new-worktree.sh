#!/usr/bin/env bash
# new-worktree.sh -- create an isolated worktree + branch by hand.
#
# When you spawn an implementer via the Agent tool with isolation: "worktree",
# Claude Code creates the worktree for you and this script is unnecessary. Use it
# for the cases where the orchestrator manages worktrees itself: checking out a
# PR branch for a review or visual-QA agent, or running the pipeline without the
# Agent tool's built-in isolation.
#
# Worktrees are created under the configured `worktree_base` so the Stop-hook
# sweeper can clean finished `agent-*` worktrees. The default base branch comes
# from the configured branch role in ORCH_BASE_ROLE, or the legacy integration
# role when ORCH_BASE_ROLE is unset.
#
# Usage:
#   new-worktree.sh <branch> [base]
#   new-worktree.sh feat/x-add-widget                     # new branch off origin/<configured role>
#   new-worktree.sh feat/x-add-widget origin/<branch>     # explicit base
#   new-worktree.sh --existing feat/x-add-widget          # check out an existing remote branch
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-config.sh
. "$HERE/lib-config.sh"

REPO_ROOT="$(git rev-parse --show-toplevel)"
WT_BASE="$(orch_get worktree_base .claude/worktrees)"
case "$WT_BASE" in
  /*) WT_ROOT="${WT_BASE%/}" ;;
  *)  WT_ROOT="${REPO_ROOT}/${WT_BASE%/}" ;;
esac
mkdir -p "$WT_ROOT"

EXISTING=0
if [ "${1:-}" = "--existing" ]; then EXISTING=1; shift; fi

BRANCH="${1:?usage: new-worktree.sh <branch> [base]}"
BASE_ROLE="${ORCH_BASE_ROLE:-integration}"
BASE="${2:-origin/$(orch_branch_name "$BASE_ROLE")}"
SLUG="$(printf '%s' "$BRANCH" | tr '/' '-')"
case "$SLUG" in
  agent-*) WT_NAME="$SLUG" ;;
  *)       WT_NAME="agent-$SLUG" ;;
esac
WT_PATH="${WT_ROOT}/${WT_NAME}"

git -C "$REPO_ROOT" fetch origin --quiet

if [ -d "$WT_PATH" ]; then
  echo "worktree already exists: $WT_PATH"; exit 0
fi

if [ "$EXISTING" = "1" ]; then
  # Review/QA: check out an existing branch (the implementer's PR branch).
  git -C "$REPO_ROOT" worktree add "$WT_PATH" "$BRANCH"
else
  # Implementer: create a fresh branch off the latest base.
  git -C "$REPO_ROOT" worktree add -b "$BRANCH" "$WT_PATH" "$BASE"
fi

echo "WORKTREE: $WT_PATH"
echo "BRANCH:   $BRANCH"
echo "BASE:     $BASE"
echo "Next: cd into the worktree and install dependencies if they are not shared."
