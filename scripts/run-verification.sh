#!/usr/bin/env bash
# run-verification.sh -- run one named heavy verification from the config
# `verification:` block (e.g. the full e2e suite) to completion, and on success
# write a sha-stamped result file the merge-guard can validate.
#
# This is the generalisation of a project's "full suite" gate. A repo declares
# its verifications in .orchestration/config.yaml; this script runs one by name.
# The sha stamp closes the "recorded green without actually running it" gap: the
# merge-guard's --record-green refuses to register a marker unless a result file
# whose sha matches the PR head exists (see merge-guard.sh).
#
# Run from INSIDE the PR's worktree, AFTER rebasing onto the configured target
# branch, so the verification exercises the actual post-merge state.
#
# Usage:
#   run-verification.sh <name>          # e.g. run-verification.sh e2e
#
# Operational note: a heavy suite takes many minutes. Run it where no short
# command timeout applies; a timeout that kills the run is RED, not a pass.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-config.sh
. "$HERE/lib-config.sh"

NAME="${1:?usage: run-verification.sh <name>}"
if ! [[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "run-verification: verification name is unsafe." >&2
  exit 2
fi
RUN="$(orch_named verification "$NAME" run)"
if [ -z "$RUN" ]; then
  echo "run-verification: no verification named '$NAME' with a 'run:' in $(orch_config_file)." >&2
  exit 2
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
SHA_FULL="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
if [ -z "$BRANCH" ] || [ "${BRANCH#-}" != "$BRANCH" ] \
   || ! git check-ref-format --branch "$BRANCH" >/dev/null 2>&1; then
  echo "run-verification: could not resolve a safe source branch name." >&2
  exit 2
fi
if ! [[ "$SHA_FULL" =~ ^[0-9a-f]{40}$ ]]; then
  echo "run-verification: could not resolve a canonical full source SHA." >&2
  exit 2
fi
SHA_SHORT="${SHA_FULL:0:12}"

STATUS_DIR="${GATE_STATUS_DIR:-${MERGE_GUARD_STATUS_DIR:-$(orch_project_root)/.orchestration/.gate-status}}"
if ! mkdir -p "$STATUS_DIR"; then
  echo "run-verification: result directory is unavailable." >&2
  exit 2
fi
RESULT_FILE="${STATUS_DIR}/verify-${NAME}-${SHA_FULL}.green"
rm -f "$RESULT_FILE" 2>/dev/null || {
  echo "run-verification: could not invalidate an earlier result." >&2
  exit 2
}

echo "== verification '${NAME}' on ${BRANCH} @ ${SHA_SHORT} =="
echo "Running the configured verification command."

if bash -c "$RUN"; then
  umask 077
  if [ -n "${RUN_VERIFICATION_FORCE_PUBLISH_FAILURE:-}" ]; then
    echo "run-verification: verification passed but result publication failed." >&2
    exit 1
  fi
  RESULT_TMP="$(mktemp "${RESULT_FILE}.tmp.XXXXXX" 2>/dev/null)" || {
    echo "run-verification: verification passed but result publication failed." >&2
    exit 1
  }
  trap 'rm -f "$RESULT_TMP"' EXIT
  trap 'rm -f "$RESULT_TMP"; exit 1' HUP INT TERM
  RESULT_AT="$(date -u +%FT%TZ 2>/dev/null)"
  if ! [[ "$RESULT_AT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
    echo "run-verification: verification passed but timestamp generation failed." >&2
    exit 1
  fi
  if ! printf 'result=GREEN\nname=%s\nbranch=%s\nsha=%s\nat=%s\n' \
    "$NAME" "$BRANCH" "$SHA_FULL" "$RESULT_AT" > "$RESULT_TMP" \
    || ! mv -f "$RESULT_TMP" "$RESULT_FILE"; then
    rm -f "$RESULT_TMP" "$RESULT_FILE" 2>/dev/null || true
    echo "run-verification: verification passed but atomic result publication failed." >&2
    exit 1
  fi
  trap - EXIT HUP INT TERM
  echo
  echo "VERIFICATION '${NAME}': GREEN. Result file: ${RESULT_FILE}"
  echo "To authorize the sanctioned merge wrapper, record the marker (it validates this file's sha vs the PR head):"
  echo "    merge-guard.sh --record-green <pr> ${RESULT_FILE}"
  exit 0
else
  code=$?
  # Never write a .green file on failure: RED must not be confusable with GREEN.
  echo
  echo "VERIFICATION '${NAME}': RED (exit ${code}). Do NOT merge; loop back to the implementer." >&2
  echo "A timeout/abort that stops the run early is also RED, not a pass." >&2
  exit 1
fi
