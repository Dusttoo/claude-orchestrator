#!/usr/bin/env bash
# merge-on-green.sh -- the sanctioned merge step. Merges a PR to the integration
# branch, but ONLY after the gates are green, only one merge at a time (a lock
# serialises concurrent agents), and verifies the work actually landed.
#
# This script does NOT decide green/red -- the orchestrator passes that in after
# the gate pipeline. Its job is the safe-merge mechanics: refuse-unless-green,
# lock, note the base sha, merge with the configured strategy, verify the branch
# advanced and (optionally) that an added file propagated, then report.
#
# Branch model and merge strategy come from .orchestration/config.yaml
# (integration_branch, merge_to_integration).
#
# Usage:
#   merge-on-green.sh <pr_number> <branch> <gate_status> [verify_path]
#     gate_status  : must be the literal "all-green" or the merge is refused.
#     verify_path  : a repo-relative path to a file the PR ADDED; used to confirm
#                    the merge reached origin/<integration>. Recommended for any
#                    PR that adds files.
#
# Example:
#   merge-on-green.sh 412 feat/x-add-widget all-green src/components/Widget.tsx
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-config.sh
. "$HERE/lib-config.sh"

PR="${1:?usage: merge-on-green.sh <pr> <branch> <gate_status> [verify_path]}"
BRANCH="${2:?branch required}"
GATE="${3:?gate_status required (must be 'all-green')}"
VERIFY_PATH="${4:-}"

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

INTEGRATION="$(orch_get integration_branch develop)"
case "$(orch_get merge_to_integration merge)" in
  squash) MERGE_FLAG="--squash" ;;
  *)      MERGE_FLAG="--merge" ;;
esac

if [ "$GATE" != "all-green" ]; then
  echo "REFUSED: gate_status is '$GATE', not 'all-green'. Not merging PR #$PR." >&2
  exit 2
fi

# ---- merge lock: only one merge at a time across concurrent agents ----
LOCK="$REPO_ROOT/.git/orchestrator-merge.lock"
if ! ( set -o noclobber; echo "pid=$$ pr=$PR $(date -u +%FT%TZ)" > "$LOCK" ) 2>/dev/null; then
  echo "MERGE LOCK HELD by:" >&2; cat "$LOCK" >&2
  echo "Queue PR #$PR and retry after the current merge completes." >&2
  exit 75
fi
trap 'rm -f "$LOCK"' EXIT

echo "== Merging PR #$PR ($BRANCH) -> ${INTEGRATION} (${MERGE_FLAG}) =="

# ---- note the base sha; a moved integration branch invalidates earlier gates ----
git fetch origin "$INTEGRATION" --quiet
PRE="$(git rev-parse "origin/${INTEGRATION}")"
echo "${INTEGRATION} is at ${PRE:0:12}"
echo "Reminder: gates must have been run against ${INTEGRATION} @ ${PRE:0:12}."
echo "If ${INTEGRATION} moved since the gate run, abort, rebase, re-run the gate, then retry."

# ---- the merge ----
# Deliberately WITHOUT --delete-branch: gh's branch deletion also removes the
# LOCAL branch, which fails (and, under `set -e`, would abort this script BEFORE
# the verify step) when a leftover agent worktree still holds that branch. The
# merge is the irreversible act; branch cleanup is not, so we separate them and
# do cleanup best-effort AFTER verification.
gh pr merge "$PR" "$MERGE_FLAG"

# ---- verify the merge propagated ----
git fetch origin "$INTEGRATION" --quiet
POST="$(git rev-parse "origin/${INTEGRATION}")"
echo "${INTEGRATION} now at ${POST:0:12}"

if [ "$POST" = "$PRE" ]; then
  echo "ERROR: origin/${INTEGRATION} did not advance after merge. Investigate before continuing." >&2
  exit 3
fi

if [ -n "$VERIFY_PATH" ]; then
  if git cat-file -e "origin/${INTEGRATION}:${VERIFY_PATH}" 2>/dev/null; then
    echo "VERIFIED: ${VERIFY_PATH} is present on origin/${INTEGRATION}."
  else
    echo "ERROR: ${VERIFY_PATH} is NOT on origin/${INTEGRATION} after merge. Possible orphaned work." >&2
    exit 4
  fi
else
  echo "WARNING: no verify_path given -- could not confirm added files landed. Pass one next time."
fi

# ---- housekeeping (best effort; must NEVER fail a merge that already landed) ----
# Drop the now-spent green marker.
"$HERE/merge-guard.sh" --clear "$PR" >/dev/null 2>&1 || true
# Delete the merged branch, remote then local, tolerating a worktree that still
# holds it. A leftover agent worktree must not turn a verified merge into a
# non-zero exit (this bit a real run: the merge succeeded but --delete-branch
# aborted the script before it could verify).
git push origin --delete "$BRANCH" >/dev/null 2>&1 || true
git branch -D "$BRANCH" >/dev/null 2>&1 || true

echo "== PR #$PR merged and verified on ${INTEGRATION} =="
