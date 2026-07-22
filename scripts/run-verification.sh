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
# Run from INSIDE the PR's worktree, AFTER rebasing onto the integration branch,
# so the verification exercises the actual post-merge state.
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
RUN="$(orch_named verification "$NAME" run)"
if [ -z "$RUN" ]; then
  echo "run-verification: no verification named '$NAME' with a 'run:' in $(orch_config_file)." >&2
  exit 2
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
SHA_FULL="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
SHA_SHORT="${SHA_FULL:0:12}"

STATUS_DIR="${GATE_STATUS_DIR:-$(orch_project_root)/.orchestration/.gate-status}"
mkdir -p "$STATUS_DIR"
RESULT_FILE="${STATUS_DIR}/verify-${NAME}-${SHA_FULL}.green"

echo "== verification '${NAME}' on ${BRANCH} @ ${SHA_SHORT} =="
echo "\$ ${RUN}"

if bash -c "$RUN"; then
  printf 'result=GREEN\nname=%s\nbranch=%s\nsha=%s\nat=%s\n' \
    "$NAME" "$BRANCH" "$SHA_FULL" "$(date -u +%FT%TZ)" > "$RESULT_FILE"
  echo
  echo "VERIFICATION '${NAME}': GREEN. Result file: ${RESULT_FILE}"
  echo "To allow the merge, record the marker (it validates this file's sha vs the PR head):"
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
