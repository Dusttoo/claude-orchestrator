#!/usr/bin/env bash
# merge-guard.sh -- Claude Code / Codex PreToolUse hook (matcher: Bash). Turns
# "never merge on red" from orchestrator discipline into a MECHANISM. Raw
# shell merges are always blocked; only merge-on-green.sh may consume a fresh,
# artifact-backed marker and perform the repository-bound exact-head merge.
#
# Hook contract:
#   stdin  : the PreToolUse JSON (.tool_name, .tool_input.command)
#   exit 0 : ALLOW (the complete payload is proven safe data/non-merge syntax)
#   exit 2 : BLOCK; stderr is fed back to the model as the reason.
#
# Controller modes (host-neutral; called by both Claude Code and Codex):
#   merge-guard.sh --record-green <pr> <result_file>
#       Validate a canonical, fresh GREEN result from run-verification.sh and
#       stamp a marker with that proof plus the exact live PR head/base identity.
#   merge-guard.sh --assert-green <pr> [expected_head_branch]
#       Re-read GitHub and require the marker's plugin version, head name/sha,
#       base name/sha, and freshness to match exactly. The sanctioned merge script
#       calls this directly, so hook registration is never a correctness dependency.
#   merge-guard.sh --clear <pr>          # drop the marker (e.g. after a rebase)
#
# Fail-closed by design: initialized repositories block Bash/unknown hook
# payloads when the dedicated classifier cannot run or prove a safe result.
set -uo pipefail
set -f

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-config.sh
. "$HERE/lib-config.sh"

CONFIG_FILE="$(orch_config_file)"
STATUS_DIR="${MERGE_GUARD_STATUS_DIR:-${GATE_STATUS_DIR:-$(orch_project_root)/.orchestration/.gate-status}}"
MAX_AGE="${MERGE_GUARD_MAX_AGE_SECONDS:-3600}"

resolve_plugin_version() {
  local codex_version claude_version
  codex_version="$(sed -n 's/.*"version":[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$HERE/../.codex-plugin/plugin.json" 2>/dev/null | head -1)"
  claude_version="$(sed -n 's/.*"version":[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$HERE/../.claude-plugin/plugin.json" 2>/dev/null | head -1)"
  [ -n "$codex_version" ] && [ "$codex_version" = "$claude_version" ] || return 1
  printf '%s' "$codex_version"
}

# Exact tab-separated reader contract: head branch, head sha, base branch,
# base sha. GitHub is authoritative; caller-provided environment values are
# never accepted as PR identity.
resolve_pr_identity() {
  local pr="$1" repository="${2:-}"
  [ -n "$repository" ] || return 1
  env -u GH_REPO -u GH_HOST gh pr view "$pr" --repo "$repository" \
    --json headRefName,headRefOid,baseRefName,baseRefOid \
    --jq '[.headRefName,.headRefOid,.baseRefName,.baseRefOid] | @tsv' 2>/dev/null
}

# Deliberately perform the same authoritative lookup again immediately before
# publication so source movement cannot be hidden behind a caller-controlled
# second-read seam.
resolve_pr_identity_again() {
  resolve_pr_identity "$1" "$2"
}

resolve_current_repository() {
  env -u GH_REPO -u GH_HOST gh repo view \
    --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null
}

resolve_current_repository_again() {
  resolve_current_repository
}

marker_value() {
  local marker="$1" key="$2"
  grep -Eo "(^|[[:space:]])${key}=[^[:space:]]+" "$marker" 2>/dev/null \
    | head -1 | sed -E "s/^[[:space:]]*${key}=//"
}


valid_sha() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]]
}

valid_token() {
  [ -n "$1" ] && [[ "$1" != *[[:space:]=]* ]]
}

valid_repository() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$ ]]
}

normalized_repository() {
  printf '%s' "$1" | LC_ALL=C tr '[:upper:]' '[:lower:]'
}

valid_branch() {
  [ -n "$1" ] && [ "${1#-}" = "$1" ] \
    && git check-ref-format --branch "$1" >/dev/null 2>&1
}

valid_max_age() {
  [[ "$MAX_AGE" =~ ^[0-9]+$ ]]
}

canonical_epoch_to_iso() {
  date -u -r "$1" +%FT%TZ 2>/dev/null \
    || date -u -d "@$1" +%FT%TZ 2>/dev/null
}

# Read the artifact once and accept exactly one of each canonical record.
# Values never appear in diagnostics.
validate_result_artifact() {
  local file="$1" head_branch="$2" head_sha="$3"
  local physical_status physical_parent expected line key value lines=0
  local seen_result=0 seen_name=0 seen_branch=0 seen_sha=0 seen_at=0
  local result="" name="" branch="" sha="" at="" epoch now age

  [ -f "$file" ] && [ ! -L "$file" ] && [ -r "$file" ] || return 1
  physical_status="$(mkdir -p "$STATUS_DIR" 2>/dev/null && cd "$STATUS_DIR" 2>/dev/null && pwd -P)" || return 1
  physical_parent="$(cd "$(dirname "$file")" 2>/dev/null && pwd -P)" || return 1
  [ "$physical_parent" = "$physical_status" ] || return 1

  while IFS= read -r line || [ -n "$line" ]; do
    lines=$((lines + 1))
    [ "$lines" -le 5 ] || return 1
    [[ "$line" == *=* ]] || return 1
    key="${line%%=*}"
    value="${line#*=}"
    [ -n "$value" ] || return 1
    case "$value" in *[[:space:]]*) return 1 ;; esac
    case "$lines:$key" in 1:result|2:name|3:branch|4:sha|5:at) ;; *) return 1 ;; esac
    case "$key" in
      result) [ "$seen_result" -eq 0 ] || return 1; seen_result=1; result="$value" ;;
      name) [ "$seen_name" -eq 0 ] || return 1; seen_name=1; name="$value" ;;
      branch) [ "$seen_branch" -eq 0 ] || return 1; seen_branch=1; branch="$value" ;;
      sha) [ "$seen_sha" -eq 0 ] || return 1; seen_sha=1; sha="$value" ;;
      at) [ "$seen_at" -eq 0 ] || return 1; seen_at=1; at="$value" ;;
      *) return 1 ;;
    esac
  done < "$file"
  [ "$lines" -eq 5 ] && [ "$seen_result" -eq 1 ] && [ "$seen_name" -eq 1 ] \
    && [ "$seen_branch" -eq 1 ] && [ "$seen_sha" -eq 1 ] && [ "$seen_at" -eq 1 ] || return 1
  [ "$result" = "GREEN" ] || return 1
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || return 1
  [ -n "$(orch_named verification "$name" run)" ] || return 1
  [ "$branch" = "$head_branch" ] || return 1
  valid_sha "$sha" && [ "$sha" = "$head_sha" ] || return 1
  [[ "$at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || return 1
  epoch="$(iso_to_epoch "$at")"
  [ -n "$epoch" ] && [ "$(canonical_epoch_to_iso "$epoch")" = "$at" ] || return 1
  valid_max_age || return 1
  now="$(date -u +%s)" || return 1
  age=$((now - epoch))
  [ "$age" -ge 0 ] && [ "$age" -le "$MAX_AGE" ] || return 1
  expected="verify-${name}-${sha}.green"
  [ "$(basename "$file")" = "$expected" ] || return 1

  RESULT_NAME="$name"
  RESULT_BRANCH="$branch"
  RESULT_SHA="$sha"
  RESULT_AT="$at"
  RESULT_BASENAME="$expected"
  return 0
}

validate_marker_shape() {
  local marker="$1" line token key value count=0
  local expected='pr repo plugin_version head_branch head_sha base_branch base_sha recorded_at verification_name verification_branch verification_sha verification_at verification_file'
  local seen=' '
  [ -f "$marker" ] && [ ! -L "$marker" ] && [ -r "$marker" ] || return 1
  IFS= read -r line < "$marker" || return 1
  [ "$(wc -l < "$marker" | tr -d ' ')" = "1" ] || return 1
  set -- $line
  [ "${1:-}" = "all-green" ] || return 1
  shift
  for token in "$@"; do
    [[ "$token" == *=* ]] || return 1
    key="${token%%=*}"; value="${token#*=}"
    [ -n "$value" ] || return 1
    case " $expected " in *" $key "*) ;; *) return 1 ;; esac
    case "$seen" in *" $key "*) return 1 ;; esac
    seen="${seen}${key} "; count=$((count + 1))
  done
  [ "$count" -eq 13 ] || return 1
  for key in $expected; do case "$seen" in *" $key "*) ;; *) return 1 ;; esac; done
}

assert_green() {
  local pr="$1" expected_head="${2:-}" snapshot_file="${3:-}" lookup="${4:-$1}" selected_repo="${5:-}" marker identity
  local head_branch head_sha base_branch base_sha target_branch plugin_version repository mark_repo
  local mark_version mark_head_branch mark_head_sha mark_base_branch mark_base_sha mark_at mark_epoch age
  local verification_name verification_branch verification_sha verification_at verification_file

  repository="$(resolve_current_repository)"
  if ! valid_repository "$repository" \
     || { [ -n "$selected_repo" ] \
       && { ! valid_repository "$selected_repo" \
         || [ "$(normalized_repository "$selected_repo")" != "$(normalized_repository "$repository")" ]; }; }; then
    echo "merge-guard: REFUSED: selected repository does not match the authoritative current repository." >&2
    return 2
  fi
  marker="${STATUS_DIR}/pr-${pr}.green"
  if ! validate_marker_shape "$marker"; then
    echo "merge-guard: REFUSED: no all-green marker for PR #$pr." >&2
    return 2
  fi
  identity="$(resolve_pr_identity "$lookup" "$repository")"
  IFS=$'\t' read -r head_branch head_sha base_branch base_sha <<<"$identity"
  if [ -z "$head_branch" ] || [ -z "$head_sha" ] || [ -z "$base_branch" ] || [ -z "$base_sha" ]; then
    echo "merge-guard: REFUSED: could not resolve exact head/base identity for PR #$pr." >&2
    return 2
  fi
  if ! valid_branch "$head_branch" || ! valid_branch "$base_branch" \
     || ! valid_sha "$head_sha" || ! valid_sha "$base_sha" || ! valid_max_age; then
    echo "merge-guard: REFUSED: live identity or freshness policy is invalid." >&2
    return 2
  fi
  target_branch="$(orch_branch_name "${MERGE_TARGET_ROLE:-integration}" 2>/dev/null)"
  plugin_version="$(resolve_plugin_version)"
  if [ -z "$plugin_version" ] || [ -z "$target_branch" ]; then
    echo "merge-guard: REFUSED: could not resolve plugin version or configured target branch." >&2
    return 2
  fi

  mark_version="$(marker_value "$marker" plugin_version)"
  mark_repo="$(marker_value "$marker" repo)"
  mark_head_branch="$(marker_value "$marker" head_branch)"
  mark_head_sha="$(marker_value "$marker" head_sha)"
  mark_base_branch="$(marker_value "$marker" base_branch)"
  mark_base_sha="$(marker_value "$marker" base_sha)"
  if [ "$mark_version" != "$plugin_version" ]; then
    echo "merge-guard: REFUSED: marker plugin version '${mark_version:-missing}' != active '$plugin_version'. Re-gate." >&2
    return 2
  fi
  if ! valid_repository "$mark_repo" \
     || [ "$(normalized_repository "$mark_repo")" != "$(normalized_repository "$repository")" ]; then
    echo "merge-guard: REFUSED: marker repository does not match the authoritative current repository." >&2
    return 2
  fi
  if [ -n "$expected_head" ] && [ "$head_branch" != "$expected_head" ]; then
    echo "merge-guard: REFUSED: PR #$pr head '$head_branch' != expected '$expected_head'." >&2
    return 2
  fi
  if [ "$base_branch" != "$target_branch" ]; then
    echo "merge-guard: REFUSED: PR #$pr base '$base_branch' != configured '$target_branch'." >&2
    return 2
  fi
  if [ "$mark_head_branch" != "$head_branch" ] || [ "$mark_head_sha" != "$head_sha" ] \
     || [ "$mark_base_branch" != "$base_branch" ] || [ "$mark_base_sha" != "$base_sha" ]; then
    echo "merge-guard: REFUSED: PR #$pr head/base identity changed after gating. Re-gate." >&2
    return 2
  fi

  verification_name="$(marker_value "$marker" verification_name)"
  verification_branch="$(marker_value "$marker" verification_branch)"
  verification_sha="$(marker_value "$marker" verification_sha)"
  verification_at="$(marker_value "$marker" verification_at)"
  verification_file="$(marker_value "$marker" verification_file)"
  if [ "$(marker_value "$marker" pr)" != "$pr" ] \
     || [ "$verification_branch" != "$head_branch" ] \
     || [ "$verification_sha" != "$head_sha" ] \
     || ! valid_sha "$verification_sha" \
     || [ "$verification_file" != "verify-${verification_name}-${verification_sha}.green" ]; then
    echo "merge-guard: REFUSED: marker verification proof is inconsistent with PR identity." >&2
    return 2
  fi
  if ! [[ "$verification_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
     || [ -z "$(orch_named verification "$verification_name" run)" ]; then
    echo "merge-guard: REFUSED: marker verification is not configured." >&2
    return 2
  fi
  local verification_epoch verification_age
  verification_epoch="$(iso_to_epoch "$verification_at")"
  [ -n "$verification_epoch" ] && [ "$(canonical_epoch_to_iso "$verification_epoch")" = "$verification_at" ] || {
    echo "merge-guard: REFUSED: marker verification timestamp is invalid." >&2; return 2; }
  verification_age=$(( $(date -u +%s) - verification_epoch ))
  if [ "$verification_age" -lt 0 ] || [ "$verification_age" -gt "$MAX_AGE" ]; then
    echo "merge-guard: REFUSED: marker verification proof is not fresh. Re-gate." >&2
    return 2
  fi

  mark_at="$(marker_value "$marker" recorded_at)"
  mark_epoch="$(iso_to_epoch "$mark_at")"
  if ! [[ "$mark_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
     || [ -z "$mark_epoch" ] || [ "$(canonical_epoch_to_iso "$mark_epoch")" != "$mark_at" ]; then
    echo "merge-guard: REFUSED: marker timestamp is missing or invalid." >&2
    return 2
  fi
  age=$(( $(date -u +%s) - mark_epoch ))
  if [ "$age" -gt "$MAX_AGE" ] || [ "$age" -lt 0 ]; then
    echo "merge-guard: REFUSED: marker for PR #$pr is ${age}s old (max ${MAX_AGE}s). Re-gate." >&2
    return 2
  fi
  if [ -n "$snapshot_file" ]; then
    umask 077
    if [ -L "$snapshot_file" ] || ! printf '%s\n%s\n%s\n%s\n%s\n' \
      "$repository" "$head_branch" "$head_sha" "$base_branch" "$base_sha" > "$snapshot_file"; then
      echo "merge-guard: REFUSED: could not publish the coherent identity snapshot." >&2
      return 2
    fi
  fi
  ASSERT_HEAD_SHA="$head_sha"
  ASSERT_HEAD_BRANCH="$head_branch"
  ASSERT_BASE_BRANCH="$base_branch"
  ASSERT_BASE_SHA="$base_sha"
  echo "merge-guard: all-green identity verified for PR #$pr."
  return 0
}

# Parse an ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ) to epoch seconds.
# GNU date first, BSD/macOS date as fallback. Empty on failure.
iso_to_epoch() {
  date -u -d "$1" +%s 2>/dev/null \
    || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null
}

case "${1:-}" in
  --record-green)
    PR="${2:-}"
    if ! [[ "$PR" =~ ^[1-9][0-9]*$ ]]; then
      echo "merge-guard: REFUSED: PR must be a positive decimal identifier." >&2
      exit 2
    fi
    MARKER="${STATUS_DIR}/pr-${PR}.green"
    if ! rm -f "$MARKER" 2>/dev/null; then
      echo "merge-guard: REFUSED: could not invalidate the previous marker." >&2
      exit 2
    fi
    if [ ! -f "$CONFIG_FILE" ]; then
      echo "merge-guard: REFUSED: .orchestration/config.yaml not found; run orchestration-init first." >&2
      exit 2
    fi
    if ! orch_validate_config; then
      echo "merge-guard: REFUSED: orchestration config is invalid; validate it before recording a green marker." >&2
      exit 2
    fi
    RESULT_FILE="${3:-}"
    if [ -z "$RESULT_FILE" ]; then
      echo "merge-guard: REFUSED: usage: merge-guard.sh --record-green <pr> <result_file>." >&2
      exit 2
    fi
    REPOSITORY="$(resolve_current_repository)"
    if ! valid_repository "$REPOSITORY"; then
      echo "merge-guard: REFUSED: could not resolve the authoritative current repository." >&2
      exit 2
    fi
    IDENTITY="$(resolve_pr_identity "$PR" "$REPOSITORY")"
    IFS=$'\t' read -r HEAD_BRANCH SHA BASE_BRANCH BASE_SHA <<<"$IDENTITY"
    TARGET_BRANCH="$(orch_branch_name "${MERGE_TARGET_ROLE:-integration}" 2>/dev/null)"
    PLUGIN_VERSION="$(resolve_plugin_version)"
    if ! valid_branch "$HEAD_BRANCH" || ! valid_branch "$BASE_BRANCH" \
       || ! valid_sha "$SHA" || ! valid_sha "$BASE_SHA"; then
      echo "merge-guard: REFUSED: could not resolve exact PR #$PR head/base identity." >&2
      exit 2
    fi
    if ! valid_token "$PLUGIN_VERSION"; then
      echo "merge-guard: REFUSED: could not resolve active plugin version." >&2
      exit 2
    fi
    if [ "$BASE_BRANCH" != "$TARGET_BRANCH" ]; then
      echo "merge-guard: REFUSED: PR #$PR base '$BASE_BRANCH' != configured '$TARGET_BRANCH'." >&2
      exit 2
    fi
    if ! validate_result_artifact "$RESULT_FILE" "$HEAD_BRANCH" "$SHA"; then
      echo "merge-guard: REFUSED: verification artifact is missing, unsafe, malformed, inconsistent, or stale." >&2
      exit 2
    fi
    IDENTITY_AGAIN="$(resolve_pr_identity_again "$PR" "$REPOSITORY")"
    REPOSITORY_AGAIN="$(resolve_current_repository_again)"
    if [ "$IDENTITY_AGAIN" != "$IDENTITY" ] \
       || [ "$(normalized_repository "$REPOSITORY_AGAIN")" != "$(normalized_repository "$REPOSITORY")" ]; then
      echo "merge-guard: REFUSED: PR identity changed while verification proof was being recorded." >&2
      exit 2
    fi
    if ! mkdir -p "$STATUS_DIR" 2>/dev/null; then
      echo "merge-guard: REFUSED: marker directory is unavailable." >&2
      exit 2
    fi
    umask 077
    if [ -n "${MERGE_GUARD_FORCE_PUBLISH_FAILURE:-}" ]; then
      echo "merge-guard: REFUSED: marker publication failed." >&2
      exit 2
    fi
    MARKER_TMP="$(mktemp "${MARKER}.tmp.XXXXXX" 2>/dev/null)" || {
      echo "merge-guard: REFUSED: could not prepare an atomic marker." >&2; exit 2; }
    trap 'rm -f "$MARKER_TMP"' EXIT
    trap 'rm -f "$MARKER_TMP"; exit 2' HUP INT TERM
    RECORDED_AT="$(date -u +%FT%TZ 2>/dev/null)"
    if ! [[ "$RECORDED_AT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
      echo "merge-guard: REFUSED: could not generate a canonical marker timestamp." >&2
      exit 2
    fi
    if ! printf 'all-green pr=%s repo=%s plugin_version=%s head_branch=%s head_sha=%s base_branch=%s base_sha=%s recorded_at=%s verification_name=%s verification_branch=%s verification_sha=%s verification_at=%s verification_file=%s\n' \
      "$PR" "$REPOSITORY" "$PLUGIN_VERSION" "$HEAD_BRANCH" "$SHA" "$BASE_BRANCH" "$BASE_SHA" \
      "$RECORDED_AT" "$RESULT_NAME" "$RESULT_BRANCH" "$RESULT_SHA" "$RESULT_AT" "$RESULT_BASENAME" \
      > "$MARKER_TMP" || ! mv -f "$MARKER_TMP" "$MARKER"; then
      rm -f "$MARKER_TMP" "$MARKER" 2>/dev/null || true
      echo "merge-guard: REFUSED: could not publish the atomic marker." >&2
      exit 2
    fi
    trap - EXIT HUP INT TERM
    echo "merge-guard: recorded artifact-backed all-green proof for PR #$PR."
    exit 0
    ;;
  --assert-green)
    PR="${2:?usage: merge-guard.sh --assert-green <pr> [expected_head_branch] [snapshot_file]}"
    if [ ! -f "$CONFIG_FILE" ] || ! orch_validate_config; then
      echo "merge-guard: REFUSED: orchestration config is missing or invalid." >&2
      exit 2
    fi
    assert_green "$PR" "${3:-}" "${4:-}"
    exit $?
    ;;
  --clear)
    PR="${2:?usage: merge-guard.sh --clear <pr>}"
    rm -f "${STATUS_DIR}/pr-${PR}.green"
    echo "merge-guard: cleared green marker for PR #$PR."
    exit 0
    ;;
esac

# ---- hook mode ----------------------------------------------------------------
# Plugin hooks may be enabled globally by Claude Code or Codex. Outside a repo
# that has opted into this harness, the guard must be invisible and must not
# create runtime directories.
if [ ! -f "$CONFIG_FILE" ]; then
  cat >/dev/null 2>&1 || true
  exit 0
fi

PAYLOAD="$(cat)"
CLASSIFIER="$HERE/merge-command-classifier.py"
PARSE_DECISION="block"

# There is intentionally no partial-shell fallback. In an initialized repo,
# helper absence/failure, Python absence, malformed schema, or decode trouble
# blocks the hook payload rather than attempting substring/grep parsing.
if [ -z "${MERGE_GUARD_FORCE_FALLBACK:-}" ] \
   && command -v python3 >/dev/null 2>&1 && [ -f "$CLASSIFIER" ] && [ -r "$CLASSIFIER" ]; then
  CLASSIFIED="$(printf '%s' "$PAYLOAD" | python3 "$CLASSIFIER" 2>/dev/null)"
  CLASSIFIER_STATUS=$?
  if [ "$CLASSIFIER_STATUS" -eq 0 ] && [ -z "${MERGE_GUARD_FORCE_DECODE_FAILURE:-}" ] \
     && [ "$(printf '%s\n' "$CLASSIFIED" | wc -l | tr -d ' ')" = 8 ]; then
    SCHEMA_LINE="$(printf '%s\n' "$CLASSIFIED" | sed -n '1p')"
    DECISION_LINE="$(printf '%s\n' "$CLASSIFIED" | sed -n '2p')"
    REASON_LINE="$(printf '%s\n' "$CLASSIFIED" | sed -n '3p')"
    PR_LINE="$(printf '%s\n' "$CLASSIFIED" | sed -n '4p')"
    REPO_LINE="$(printf '%s\n' "$CLASSIFIED" | sed -n '5p')"
    STRATEGY_LINE="$(printf '%s\n' "$CLASSIFIED" | sed -n '6p')"
    BASE_LINE="$(printf '%s\n' "$CLASSIFIED" | sed -n '7p')"
    HEAD_LINE="$(printf '%s\n' "$CLASSIFIED" | sed -n '8p')"
    if [ "$SCHEMA_LINE" = "schema=merge-command-classifier/v2" ] \
       && [[ "$DECISION_LINE" == decision=* ]] && [[ "$REASON_LINE" == reason=* ]] \
       && [[ "$PR_LINE" == pr=* ]] && [[ "$REPO_LINE" == repo=* ]] \
       && [[ "$STRATEGY_LINE" == strategy=* ]] \
       && [[ "$BASE_LINE" == base=* ]] && [[ "$HEAD_LINE" == head=* ]]; then
      PARSE_DECISION="${DECISION_LINE#decision=}"
      case "$PARSE_DECISION" in allow|merge|block) ;; *) PARSE_DECISION="block" ;; esac
    fi
  fi
fi

[ "$PARSE_DECISION" != "allow" ] || exit 0
if [ "$PARSE_DECISION" = "merge" ]; then
  echo "BLOCKED by merge-guard: raw 'gh pr merge' is never authorized; use merge-on-green.sh." >&2
else
  echo "BLOCKED by merge-guard: hook payload is outside the proven-safe shell subset." >&2
fi
exit 2
