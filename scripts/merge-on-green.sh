#!/usr/bin/env bash
# merge-on-green.sh -- the sanctioned merge step. Merges a PR to the configured
# target branch role, but ONLY after the gates are green, only one merge at a
# time (a lock serialises concurrent agents), and verifies the work landed.
#
# This script does NOT decide green/red -- the orchestrator passes that in after
# the gate pipeline. Its job is the safe-merge mechanics: refuse-unless-green,
# lock, note the base sha, merge with the configured strategy, verify the branch
# advanced and (optionally) that an added file propagated, then report.
#
# Branch roles and merge strategy come from .orchestration/config.yaml. Legacy
# configs default this script to the "integration" role; set MERGE_TARGET_ROLE to
# use another configured role.
#
# Usage:
#   merge-on-green.sh <pr_number> <branch> <gate_status> [verify_path]
#     gate_status  : must be the literal "all-green" or the merge is refused.
#     verify_path  : a repo-relative path to a file the PR ADDED; used to confirm
#                    the merge reached origin/<target>. Recommended for any
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

if ! [[ "$PR" =~ ^[1-9][0-9]*$ ]]; then
  echo "REFUSED: PR must be a positive decimal identifier." >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

TARGET_ROLE="${MERGE_TARGET_ROLE:-integration}"
TARGET_BRANCH="$(orch_branch_name "$TARGET_ROLE")"
if [ -z "$TARGET_BRANCH" ]; then
  echo "REFUSED: could not resolve branch role '${TARGET_ROLE}' from orchestration config." >&2
  exit 2
fi
case "$(orch_get "merge_to_${TARGET_ROLE}" merge)" in
  squash) MERGE_FLAG="--squash" ;;
  *)      MERGE_FLAG="--merge" ;;
esac

if [ "$GATE" != "all-green" ]; then
  echo "REFUSED: gate_status is '$GATE', not 'all-green'. Not merging PR #$PR." >&2
  exit 2
fi

# ---- merge lock: only one merge at a time across concurrent agents ----
# Use Git's common directory rather than <worktree>/.git. In a linked worktree,
# .git is a gitfile, while --git-common-dir points at the shared repository
# metadata directory and therefore keeps the lock visible to every worktree.
GIT_COMMON_DIR="$(git rev-parse --git-common-dir)"
case "$GIT_COMMON_DIR" in
  /*) ;;
  *) GIT_COMMON_DIR="$REPO_ROOT/$GIT_COMMON_DIR" ;;
esac
LOCK="$GIT_COMMON_DIR/orchestrator-merge.lock"
if ! ( set -o noclobber; echo "pid=$$ pr=$PR $(date -u +%FT%TZ)" > "$LOCK" ) 2>/dev/null; then
  echo "MERGE LOCK HELD by:" >&2; cat "$LOCK" >&2
  echo "Queue PR #$PR and retry after the current merge completes." >&2
  exit 75
fi
SNAPSHOT="$(mktemp "$GIT_COMMON_DIR/orchestrator-merge-snapshot.XXXXXX")" || {
  rm -f "$LOCK"
  echo "REFUSED: could not prepare an authoritative identity snapshot." >&2
  exit 2
}
trap 'rm -f "$LOCK" "$SNAPSHOT"' EXIT

# Capture one coherent authoritative identity under the common-dir lock. The
# guard validates this exact snapshot against the marker; it does not rediscover
# or substitute another approved SHA for the merge wrapper.
if ! "$HERE/merge-guard.sh" --assert-green "$PR" "$BRANCH" "$SNAPSHOT"; then
  echo "REFUSED: all-green evidence does not match the current PR identity." >&2
  exit 2
fi
if [ "$(wc -l < "$SNAPSHOT" | tr -d ' ')" != 5 ]; then
  echo "REFUSED: authoritative identity snapshot is malformed." >&2
  exit 2
fi
REPOSITORY="$(sed -n '1p' "$SNAPSHOT")"
HEAD_BRANCH="$(sed -n '2p' "$SNAPSHOT")"
HEAD_SHA="$(sed -n '3p' "$SNAPSHOT")"
BASE_BRANCH="$(sed -n '4p' "$SNAPSHOT")"
BASE_SHA="$(sed -n '5p' "$SNAPSHOT")"
if ! [[ "$REPOSITORY" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$ ]] \
   || [ "$HEAD_BRANCH" != "$BRANCH" ] || [ "$BASE_BRANCH" != "$TARGET_BRANCH" ] \
   || ! [[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]] || ! [[ "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "REFUSED: authoritative identity snapshot is invalid or inconsistent." >&2
  exit 2
fi

echo "== Merging PR #$PR ($BRANCH) -> ${TARGET_BRANCH} (${MERGE_FLAG}) =="

# ---- synchronize the local target before the final authoritative read ----
git fetch origin "$TARGET_BRANCH" --quiet
PRE="$(git rev-parse "origin/${TARGET_BRANCH}")"
echo "${TARGET_BRANCH} is at ${PRE:0:12}"

# Last authoritative read immediately before the irreversible operation. Both
# head and base must still equal the validated snapshot. GitHub provides an
# atomic expected-head primitive but no corresponding expected-base primitive;
# branch protection or a merge queue must cover target movement after this read.
FINAL_IDENTITY="$(env -u GH_REPO -u GH_HOST gh pr view "$PR" --repo "$REPOSITORY" --json headRefName,headRefOid,baseRefName,baseRefOid \
  --jq '[.headRefName,.headRefOid,.baseRefName,.baseRefOid] | @tsv' 2>/dev/null)"
IFS=$'\t' read -r FINAL_HEAD_BRANCH FINAL_HEAD_SHA FINAL_BASE_BRANCH FINAL_BASE_SHA <<<"$FINAL_IDENTITY"
if [ "$FINAL_HEAD_BRANCH" != "$HEAD_BRANCH" ] || [ "$FINAL_HEAD_SHA" != "$HEAD_SHA" ] \
   || [ "$FINAL_BASE_BRANCH" != "$BASE_BRANCH" ] || [ "$FINAL_BASE_SHA" != "$BASE_SHA" ]; then
  echo "REFUSED: PR head or base moved after authoritative assertion; re-gate." >&2
  exit 2
fi

# ---- the merge ----
# Deliberately WITHOUT --delete-branch: gh's branch deletion also removes the
# LOCAL branch, which fails (and, under `set -e`, would abort this script BEFORE
# the verify step) when a leftover agent worktree still holds that branch. The
# merge is the irreversible act; branch cleanup is not, so we separate them and
# do cleanup best-effort AFTER verification.
env -u GH_REPO -u GH_HOST gh pr merge "$PR" --repo "$REPOSITORY" "$MERGE_FLAG" --match-head-commit "$HEAD_SHA"

# ---- verify the merge propagated ----
git fetch origin "$TARGET_BRANCH" --quiet
POST="$(git rev-parse "origin/${TARGET_BRANCH}")"
echo "${TARGET_BRANCH} now at ${POST:0:12}"

if [ "$POST" = "$PRE" ]; then
  echo "ERROR: origin/${TARGET_BRANCH} did not advance after merge. Investigate before continuing." >&2
  exit 3
fi

if [ -n "$VERIFY_PATH" ]; then
  if git cat-file -e "origin/${TARGET_BRANCH}:${VERIFY_PATH}" 2>/dev/null; then
    echo "VERIFIED: ${VERIFY_PATH} is present on origin/${TARGET_BRANCH}."
  else
    echo "ERROR: ${VERIFY_PATH} is NOT on origin/${TARGET_BRANCH} after merge. Possible orphaned work." >&2
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

echo "== PR #$PR merged and verified on ${TARGET_BRANCH} =="
