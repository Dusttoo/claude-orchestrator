#!/usr/bin/env bash
# merge-guard.sh -- Claude Code PreToolUse hook (matcher: Bash). Turns "never
# merge on red" from orchestrator discipline into a MECHANISM: a raw
# `gh pr merge` is blocked unless the gate pipeline recorded an all-green marker
# whose sha matches the PR's current head AND is recent, and a direct merge to
# the production branch is always blocked (releases go through a human-gated
# release command).
#
# Hook contract:
#   stdin  : the PreToolUse JSON (.tool_name, .tool_input.command)
#   exit 0 : ALLOW (not a merge, or a valid green marker exists)
#   exit 2 : BLOCK; stderr is fed back to the model as the reason.
#
# Recorder modes (called by the orchestrator ONLY after all gates PASS + CI green):
#   merge-guard.sh --record-green <pr> [result_file]
#       Stamp a marker with the PR's current head sha. If a result_file from
#       run-verification.sh is given, its embedded sha MUST match the PR head, so
#       a marker cannot be recorded without the verification actually having run
#       on the current commit.
#   merge-guard.sh --clear <pr>          # drop the marker (e.g. after a rebase)
#
# Fail-closed by design: if the payload cannot be parsed (e.g. python3 absent),
# the guard treats anything that looks like `gh pr merge` as a merge and blocks
# it, rather than silently disabling itself.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-config.sh
. "$HERE/lib-config.sh"

PROD_BRANCH="$(orch_get production_branch main)"
STATUS_DIR="${MERGE_GUARD_STATUS_DIR:-$(orch_project_root)/.orchestration/.gate-status}"
mkdir -p "$STATUS_DIR" 2>/dev/null || true
MAX_AGE="${MERGE_GUARD_MAX_AGE_SECONDS:-3600}"

resolve_pr_head_sha() {
  if [ -n "${MERGE_GUARD_PR_HEAD_SHA:-}" ]; then printf '%s' "$MERGE_GUARD_PR_HEAD_SHA"; return 0; fi
  gh pr view "$1" --json headRefOid -q .headRefOid 2>/dev/null
}

# Parse an ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ) to epoch seconds.
# GNU date first, BSD/macOS date as fallback. Empty on failure.
iso_to_epoch() {
  date -u -d "$1" +%s 2>/dev/null \
    || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null
}

case "${1:-}" in
  --record-green)
    PR="${2:?usage: merge-guard.sh --record-green <pr> [result_file]}"
    RESULT_FILE="${3:-}"
    SHA="$(resolve_pr_head_sha "$PR")"
    if [ -z "$SHA" ]; then
      echo "merge-guard: REFUSED: could not resolve PR #$PR head sha (gh authed? PR number right?)." >&2
      exit 2
    fi
    # If a verification result file is supplied, its sha must match the PR head,
    # so a marker cannot be stamped without the verification having run on this
    # exact commit.
    VERIFIED_BY=""
    if [ -n "$RESULT_FILE" ]; then
      if [ ! -f "$RESULT_FILE" ]; then
        echo "merge-guard: REFUSED: result file '$RESULT_FILE' not found." >&2
        exit 2
      fi
      if ! grep -q '^result=GREEN' "$RESULT_FILE"; then
        echo "merge-guard: REFUSED: result file '$RESULT_FILE' is not GREEN." >&2
        exit 2
      fi
      FILE_SHA="$(grep -Eo '^sha=[^[:space:]]+' "$RESULT_FILE" | head -1 | cut -d= -f2)"
      if [ "$FILE_SHA" != "$SHA" ]; then
        echo "merge-guard: REFUSED: result file sha ($FILE_SHA) != PR #$PR head ($SHA)." >&2
        echo "The branch moved after the verification ran. Rebase, re-run it, and retry." >&2
        exit 2
      fi
      VERIFIED_BY=" verified_by=${RESULT_FILE##*/}"
    fi
    printf 'all-green pr=%s sha=%s recorded_at=%s%s\n' \
      "$PR" "$SHA" "$(date -u +%FT%TZ)" "$VERIFIED_BY" > "${STATUS_DIR}/pr-${PR}.green"
    echo "merge-guard: recorded all-green for PR #$PR (sha=$SHA)${VERIFIED_BY:+, $VERIFIED_BY}."
    exit 0
    ;;
  --clear)
    PR="${2:?usage: merge-guard.sh --clear <pr>}"
    rm -f "${STATUS_DIR}/pr-${PR}.green"
    echo "merge-guard: cleared green marker for PR #$PR."
    exit 0
    ;;
esac

# ---- hook mode ----------------------------------------------------------------
PAYLOAD="$(cat)"

# argv-shape check (shlex) so we fire ONLY on a literal `gh pr merge`, never on a
# command whose TEXT merely contains those words (a commit body, a --body string,
# a shell comment). python3 is the precise path; a bash fallback keeps the guard
# fail-closed if python3 is unavailable.
TOOL=""; IS_MERGE="0"; CMD=""
# MERGE_GUARD_FORCE_FALLBACK=1 exercises the no-python3 path in tests.
if [ -z "${MERGE_GUARD_FORCE_FALLBACK:-}" ] && command -v python3 >/dev/null 2>&1; then
  PARSED="$(printf '%s' "$PAYLOAD" | python3 -c '
import sys, json, base64, shlex
try:
    d = json.load(sys.stdin)
except Exception:
    print("\t\t"); sys.exit(0)
tn = d.get("tool_name", "") or ""
cmd = (d.get("tool_input", {}) or {}).get("command", "") or ""
is_merge = "0"
try:
    argv = shlex.split(cmd, posix=True, comments=True)
    if len(argv) >= 3 and argv[0] == "gh" and argv[1] == "pr" and argv[2] == "merge":
        is_merge = "1"
except Exception:
    pass
print(tn + "\t" + is_merge + "\t" + base64.b64encode(cmd.encode()).decode())
' 2>/dev/null)"
  TOOL="${PARSED%%$'\t'*}"
  REST="${PARSED#*$'\t'}"
  IS_MERGE="${REST%%$'\t'*}"
  CMD_B64="${REST#*$'\t'}"
  CMD="$(printf '%s' "$CMD_B64" | base64 -d 2>/dev/null || true)"
else
  # Fail-closed fallback: no precise parse available. Pull tool_name and command
  # with a best-effort grep and flag anything resembling `gh pr merge`.
  TOOL="$(printf '%s' "$PAYLOAD" | grep -Eo '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*"([^"]*)"$/\1/')"
  CMD="$(printf '%s' "$PAYLOAD" | grep -Eo '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/^"command"[[:space:]]*:[[:space:]]*"//; s/"$//')"
  if printf '%s' "$CMD" | grep -Eq 'gh[[:space:]]+pr[[:space:]]+merge'; then IS_MERGE="1"; fi
  [ -n "$TOOL" ] || TOOL="Bash"   # assume Bash if we cannot read it (fail-closed)
fi

[ "$TOOL" = "Bash" ] || exit 0
[ "$IS_MERGE" = "1" ] || exit 0

# A squash, or any merge targeting the production branch, is never auto-performed.
if printf '%s' "$CMD" | grep -Eq '\-\-squash' \
   || printf '%s' "$CMD" | grep -Eq -- "--base[[:space:]=]+${PROD_BRANCH}([[:space:]]|$)" \
   || printf '%s' "$CMD" | grep -Eq "[[:space:]]${PROD_BRANCH}([[:space:]]|\$)"; then
  {
    echo "BLOCKED by merge-guard: direct 'gh pr merge --squash' or a merge to '${PROD_BRANCH}' is out of scope."
    echo "Release to '${PROD_BRANCH}' via the human-gated release command, not a direct merge."
  } >&2
  exit 2
fi

# PR id = first token after 'merge', strip any URL prefix.
PR="$(printf '%s' "$CMD" | grep -Eo 'gh[[:space:]]+pr[[:space:]]+merge[[:space:]]+[^[:space:]]+' | head -1 | awk '{print $4}')"
PR="${PR##*/}"

MARKER="${STATUS_DIR}/pr-${PR}.green"
if [ -n "$PR" ] && [ -f "$MARKER" ] && grep -q '^all-green' "$MARKER"; then
  MARK_SHA="$(grep -Eo 'sha=[^[:space:]]+' "$MARKER" | head -1 | cut -d= -f2)"
  HEAD_SHA="$(resolve_pr_head_sha "$PR")"
  if [ -n "$HEAD_SHA" ] && [ "$MARK_SHA" != "$HEAD_SHA" ]; then
    echo "BLOCKED by merge-guard: green marker sha ($MARK_SHA) != PR #$PR head ($HEAD_SHA). Branch moved; re-gate." >&2
    exit 2
  fi
  # Recency: a marker older than MAX_AGE is stale even if the sha still matches
  # (the tree may be fine, but CI state and base branch have moved on). Re-gate.
  MARK_AT="$(grep -Eo 'recorded_at=[^[:space:]]+' "$MARKER" | head -1 | cut -d= -f2)"
  MARK_EPOCH="$(iso_to_epoch "$MARK_AT")"
  if [ -n "$MARK_EPOCH" ]; then
    AGE=$(( $(date -u +%s) - MARK_EPOCH ))
    if [ "$AGE" -gt "$MAX_AGE" ] || [ "$AGE" -lt "-60" ]; then
      echo "BLOCKED by merge-guard: green marker for PR #$PR is ${AGE}s old (max ${MAX_AGE}s). Re-gate." >&2
      exit 2
    fi
  fi
  echo "merge-guard: all-green marker present for PR #$PR (sha matches, fresh) -- allowing direct merge." >&2
  exit 0
fi

{
  echo "BLOCKED by merge-guard: refusing direct 'gh pr merge' for PR #${PR:-?} -- no all-green marker."
  echo "Run the gate pipeline first; it records the marker after all gates PASS and CI is green."
  echo "To merge by hand after that, the marker's sha must match the PR's current head and be recent"
  echo "(a rebase or a long delay invalidates it -- re-gate)."
} >&2
exit 2
