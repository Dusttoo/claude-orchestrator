#!/usr/bin/env bash
# merge-guard.test.sh -- behavioural tests for the merge-guard hook.
#
# The guard is the enforcement centrepiece, so every gate path is exercised:
# pass-through, shell precision, unconditional raw-merge denial, marker
# recording/assertion, moved-sha, expired marker, and the
# fail-closed no-python3 fallback.
#
# Isolation: MERGE_GUARD_STATUS_DIR redirects the marker dir, while a controlled
# fake `gh` executable supplies authoritative PR identities without network
# access. Each marker-sensitive case uses its own PR number so the tests do not
# couple through shared marker state. exit 0 = ALLOW, exit 2 = BLOCK.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fails=0
assert_exit() { # <desc> <expected-code> <actual-code>
  if [ "$2" = "$3" ]; then printf 'ok   %s (exit %s)\n' "$1" "$3"
  else printf 'FAIL %s: want exit %s, got %s\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi
}
assert_true() { # <desc> <cond-cmd...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then printf 'ok   %s\n' "$desc"
  else printf 'FAIL %s\n' "$desc"; fails=$((fails + 1)); fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export MERGE_GUARD_STATUS_DIR="$TMP/markers"
export TEST_GH_HEAD_BRANCH="feat/test"
export TEST_GH_BASE_BRANCH="develop"
export TEST_GH_BASE_SHA="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
export TEST_GH_STATE_DIR="$TMP/fake-gh-state"
mkdir -p "$MERGE_GUARD_STATUS_DIR"
mkdir -p "$TMP/repo/.orchestration" "$TMP/.codex-plugin" \
  "$TMP/.claude-plugin" "$TMP/fake-gh-bin" "$TEST_GH_STATE_DIR"
cp "$HERE/../scripts/lib-config.sh" "$HERE/../scripts/merge-guard.sh" \
   "$HERE/../scripts/merge-command-classifier.py" \
   "$HERE/../scripts/orchestration-engine.py" "$TMP/repo/"
cp "$HERE/../.codex-plugin/plugin.json" "$TMP/.codex-plugin/plugin.json"
cp "$HERE/../.claude-plugin/plugin.json" "$TMP/.claude-plugin/plugin.json"
cat > "$TMP/fake-gh-bin/gh" <<'SH'
#!/usr/bin/env bash
if [ -n "${TEST_GH_FAIL:-}" ]; then exit 1; fi
if [ "${1:-} ${2:-}" = "repo view" ]; then
  [ -z "${TEST_GH_REPO_FAIL:-}" ] || exit 1
  repo_count_file="${TEST_GH_STATE_DIR}/repo-calls"
  repo_count="$(cat "$repo_count_file" 2>/dev/null || printf 0)"
  repo_count=$((repo_count + 1))
  printf '%s\n' "$repo_count" > "$repo_count_file"
  repository="${GH_REPO:-${TEST_GH_REPO:-o/r}}"
  if [ -n "${GH_HOST:-}" ]; then repository=other/r; fi
  if [ "$repo_count" -ge 2 ] && [ -n "${TEST_GH_REPO_SECOND:-}" ]; then
    repository="$TEST_GH_REPO_SECOND"
  fi
  printf '%s\n' "$repository"
  exit 0
fi
pr="${3:-unknown}"
pr="$(printf '%s' "$pr" | tr '/:' '__')"
count_file="${TEST_GH_STATE_DIR}/calls-${pr}"
count="$(cat "$count_file" 2>/dev/null || printf 0)"
count=$((count + 1))
printf '%s\n' "$count" > "$count_file"
head_branch="${TEST_GH_HEAD_BRANCH:-feat/test}"
head_sha="${TEST_GH_HEAD_SHA:-abc1234def5678abc1234def5678abc1234def56}"
base_branch="${TEST_GH_BASE_BRANCH:-develop}"
base_sha="${TEST_GH_BASE_SHA:-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}"
if [ "$count" -ge 2 ] && [ -n "${TEST_GH_HEAD_SHA_SECOND:-}" ]; then
  head_branch="${TEST_GH_HEAD_BRANCH_SECOND:-$head_branch}"
  head_sha="$TEST_GH_HEAD_SHA_SECOND"
  base_branch="${TEST_GH_BASE_BRANCH_SECOND:-$base_branch}"
  base_sha="${TEST_GH_BASE_SHA_SECOND:-$base_sha}"
fi
printf '%s\t%s\t%s\t%s\n' "$head_branch" "$head_sha" "$base_branch" "$base_sha"
SH
chmod +x "$TMP/fake-gh-bin/gh"
export PATH="$TMP/fake-gh-bin:$PATH"
printf 'integration_branch: develop\nproduction_branch: main\n' > "$TMP/repo/.orchestration/config.yaml"
cat >> "$TMP/repo/.orchestration/config.yaml" <<'YAML'
verification:
  - name: full-suite
    run: 'true'
  - name: second-suite
    run: 'true'
YAML
cd "$TMP/repo" && git init -q
GUARD="$TMP/repo/merge-guard.sh"

payload() {
  printf '{"tool_name":"%s","tool_input":{"command":%s}}' \
    "$1" "$(printf '%s' "$2" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
}
# run <tool> <command> ; echoes the payload into the guard and returns its exit
# code. Env stubs are read from the (exported) environment, so callers wanting a
# stub run inside a subshell that `export`s it -- see below.
run() { payload "$1" "$2" | bash "$GUARD" >/dev/null 2>&1; echo $?; }
make_result() { # <name> <sha> [branch] [at] [result]
  local name="$1" sha="$2" branch="${3:-feat/test}" at="${4:-$(date -u +%FT%TZ)}" result="${5:-GREEN}"
  local file="$MERGE_GUARD_STATUS_DIR/verify-${name}-${sha}.green"
  printf 'result=%s\nname=%s\nbranch=%s\nsha=%s\nat=%s\n' \
    "$result" "$name" "$branch" "$sha" "$at" > "$file"
  printf '%s' "$file"
}
record_green() {
  local result
  result="$(make_result full-suite "$2")"
  TEST_GH_HEAD_SHA="$2" bash "$GUARD" --record-green "$1" "$result" >/dev/null 2>&1
}

HEAD="abc1234def5678abc1234def5678abc1234def56"

# 1. A non-merge Bash command passes straight through.
assert_exit "non-merge command allowed" 0 "$(run Bash 'git status')"

# 2. shlex precision: a commit whose TEXT contains 'gh pr merge' is NOT a merge.
assert_exit "commit body mentioning gh pr merge allowed" 0 \
  "$(run Bash 'git commit -m "will gh pr merge after review"')"
assert_exit "PR-create body mentioning gh pr merge allowed" 0 \
  "$(run Bash 'gh pr create --body "run gh pr merge 42 later"')"
assert_exit "shell comment mentioning gh pr merge allowed" 0 \
  "$(run Bash 'git status # gh pr merge 42 --merge')"
assert_exit "comment-only gh pr merge phrase allowed" 0 \
  "$(run Bash '# gh pr merge 42 --merge')"
assert_exit "printf payload mentioning gh pr merge allowed" 0 \
  "$(run Bash 'printf "%s" "gh pr merge 42 --merge"')"
assert_exit "echo payload mentioning gh pr merge allowed" 0 \
  "$(run Bash 'echo "gh pr merge 42 --merge"')"
assert_exit "single-quoted command substitution text allowed" 0 \
  "$(run Bash "printf '%s' '\$(gh pr merge 42 --merge)'")"
assert_exit "malformed echo quote remains incidental" 0 \
  "$(run Bash 'echo "gh pr merge 42 --merge')"
assert_exit "malformed git commit quote remains incidental" 0 \
  "$(run Bash 'git commit -m "gh pr merge 42 --merge')"
assert_exit "malformed PR-create quote remains incidental" 0 \
  "$(run Bash 'gh pr create --body "gh pr merge 42 --merge')"
assert_exit "malformed printf quote remains incidental" 0 \
  "$(run Bash 'printf "%s gh pr merge 42 --merge')"
assert_exit "malformed leading merge fails closed" 2 \
  "$(run Bash 'gh pr merge 42 --merge "unterminated')"

# Executable merge commands are guarded at every supported shell command
# boundary, not only when the payload starts with `gh pr merge`.
assert_exit "merge after AND boundary blocked" 2 \
  "$(run Bash 'git status && gh pr merge 999 --merge')"
assert_exit "merge after OR boundary blocked" 2 \
  "$(run Bash 'false || gh pr merge 999 --merge')"
assert_exit "merge after semicolon boundary blocked" 2 \
  "$(run Bash 'git status; gh pr merge 999 --merge')"
assert_exit "merge after newline boundary blocked" 2 \
  "$(run Bash $'git status\ngh pr merge 999 --merge')"
assert_exit "merge after AND plus newline boundary blocked" 2 \
  "$(run Bash $'git status &&\ngh pr merge 999 --merge')"
assert_exit "merge after OR plus newline boundary blocked" 2 \
  "$(run Bash $'false ||\ngh pr merge 999 --merge')"
assert_exit "merge after semicolon plus newline boundary blocked" 2 \
  "$(run Bash $'git status;\ngh pr merge 999 --merge')"
assert_exit "merge in pipeline blocked" 2 \
  "$(run Bash 'printf x | gh pr merge 999 --merge')"
assert_exit "merge after pipeline plus newline boundary blocked" 2 \
  "$(run Bash $'printf x |\ngh pr merge 999 --merge')"
assert_exit "merge in subshell blocked" 2 \
  "$(run Bash '(gh pr merge 999 --merge)')"
assert_exit "merge in command substitution blocked" 2 \
  "$(run Bash 'value=$(gh pr merge 999 --merge)')"
assert_exit "merge in quoted command substitution blocked" 2 \
  "$(run Bash 'printf "%s" "$(gh pr merge 999 --merge)"')"
assert_exit "merge after apostrophe in quoted substitution blocked" 2 \
  "$(run Bash 'printf "%s" "apostrophe '\'' $(gh pr merge 999 --merge)"')"
assert_exit "merge in backtick substitution blocked" 2 \
  "$(run Bash 'value=`gh pr merge 999 --merge`')"
assert_exit "malformed command substitution merge fails closed" 2 \
  "$(run Bash 'value=$(gh pr merge 999 --merge')"
assert_exit "malformed quoted command substitution merge fails closed" 2 \
  "$(run Bash 'printf "%s" "$(gh pr merge 999 --merge')"
assert_exit "malformed backtick substitution merge fails closed" 2 \
  "$(run Bash 'value=`gh pr merge 999 --merge')"
assert_exit "merge in bash interpreter command blocked" 2 \
  "$(run Bash 'bash -c "gh pr merge 999 --merge"')"
assert_exit "merge in sh interpreter command blocked" 2 \
  "$(run Bash "sh -c 'gh pr merge 999 --merge'")"
assert_exit "merge in bash combined login-c command blocked" 2 \
  "$(run Bash 'bash -lc "gh pr merge 999 --merge"')"
assert_exit "merge in sh combined trace-c command blocked" 2 \
  "$(run Bash "sh -xc 'gh pr merge 999 --merge'")"
assert_exit "merge in bash c terminator blocked" 2 \
  "$(run Bash "bash -c -- 'gh pr merge 999 --merge'")"
assert_exit "merge in sh c terminator blocked" 2 \
  "$(run Bash "sh -c -- 'gh pr merge 999 --merge'")"
assert_exit "merge in dash c terminator blocked" 2 \
  "$(run Bash "dash -c -- 'gh pr merge 999 --merge'")"
assert_exit "merge in zsh c terminator blocked" 2 \
  "$(run Bash "zsh -c -- 'gh pr merge 999 --merge'")"
assert_exit "merge in ksh c terminator blocked" 2 \
  "$(run Bash "ksh -c -- 'gh pr merge 999 --merge'")"
assert_exit "merge after Bash shopt operand blocked" 2 \
  "$(run Bash "bash -c -O extglob 'gh pr merge 999 --merge'")"
assert_exit "merge after Bash shell-option operand blocked" 2 \
  "$(run Bash "A=x command env B=y exec bash -c -o errexit 'gh pr merge 999 --merge'")"
assert_exit "merge through exec wrapper blocked" 2 \
  "$(run Bash 'exec gh pr merge 999 --merge')"
assert_exit "merge in case arm blocked" 2 \
  "$(run Bash 'case x in x) gh pr merge 999 --merge ;; esac')"
assert_exit "merge after background boundary blocked" 2 \
  "$(run Bash 'sleep 1 & gh pr merge 999 --merge')"
assert_exit "merge after pipe-and boundary blocked" 2 \
  "$(run Bash 'printf x |& gh pr merge 999 --merge')"
assert_exit "multiple executable merges fail closed" 2 \
  "$(run Bash 'gh pr merge 998 --merge; gh pr merge 999 --merge')"
assert_exit "unsupported eval wrapper with merge-like input blocked" 2 \
  "$(run Bash 'eval "gh pr merge 999 --merge"')"
assert_exit "backslash newline cannot split gh" 2 \
  "$(run Bash $'g\\\nh pr merge 999 --merge')"
assert_exit "backslash newline cannot split pr" 2 \
  "$(run Bash $'gh p\\\nr merge 999 --merge')"
assert_exit "backslash newline cannot hide merge" 2 \
  "$(run Bash $'gh pr \\\nmerge 999 --merge')"
assert_exit "globbed command position blocks" 2 \
  "$(run Bash '/opt/homebrew/bin/g? pr merge 999 --merge')"
assert_exit "brace-expanded command position blocks" 2 \
  "$(run Bash 'g{h,x} pr merge 999 --merge')"
assert_exit "parameter-expanded command position blocks" 2 \
  "$(run Bash '$cmd pr merge 999 --merge')"
assert_exit "harmless heredoc blocks conservatively" 2 \
  "$(run Bash $'cat <<\'EOF\'\ngh pr merge 999 --merge\nEOF')"
assert_exit "executable heredoc blocks conservatively" 2 \
  "$(run Bash $'bash <<\'EOF\'\ngh pr merge 999 --merge\nEOF')"
assert_exit "here string blocks conservatively" 2 \
  "$(run Bash 'bash <<< "gh pr merge 999 --merge"')"
assert_exit "process substitution blocks" 2 \
  "$(run Bash 'cat <(printf "%s" "gh pr merge 999 --merge")')"
assert_exit "literal harmless redirection remains allowed" 0 \
  "$(run Bash 'printf "%s" ok > result.txt')"
assert_exit "redirected merge blocks" 2 \
  "$(run Bash 'gh pr merge 999 --merge > result.txt')"
assert_exit "leading redirection merge blocks" 2 \
  "$(run Bash '> result.txt gh pr merge 999 --merge')"
assert_exit "assignment then leading redirection merge blocks" 2 \
  "$(run Bash 'A=x > result.txt gh pr merge 999 --merge')"
assert_exit "leading FD redirection merge blocks" 2 \
  "$(run Bash '2> result.txt gh pr merge 999 --merge')"

# 3. A real merge with no marker is blocked.
assert_exit "merge without marker blocked" 2 "$(run Bash 'gh pr merge 42 --merge')"
assert_exit "record-green refuses wrong configured base" 2 \
  "$(RESULT="$(make_result full-suite "$HEAD")"; TEST_GH_HEAD_SHA="$HEAD" TEST_GH_BASE_BRANCH=main bash "$GUARD" --record-green 42 "$RESULT" >/dev/null 2>&1; echo $?)"

# Mandatory proof: bare, missing, non-regular, unreadable, malformed, or unsafe
# artifacts all fail and cannot retain a pre-existing marker.
printf 'all-green old-marker\n' > "$MERGE_GUARD_STATUS_DIR/pr-20.green"
assert_exit "bare record-green rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 20 >/dev/null 2>&1; echo $?)"
assert_true "bare record-green clears old marker" test ! -e "$MERGE_GUARD_STATUS_DIR/pr-20.green"
assert_exit "missing result path rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 21 "$TMP/missing" >/dev/null 2>&1; echo $?)"
mkdir -p "$MERGE_GUARD_STATUS_DIR/verify-full-suite-${HEAD}.green.dir"
assert_exit "result directory rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 22 "$MERGE_GUARD_STATUS_DIR/verify-full-suite-${HEAD}.green.dir" >/dev/null 2>&1; echo $?)"
ln -s "$(make_result full-suite "$HEAD")" "$TMP/result-link"
assert_exit "result symlink rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 23 "$TMP/result-link" >/dev/null 2>&1; echo $?)"
mkfifo "$TMP/result-fifo"
assert_exit "result FIFO rejected without reading" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 24 "$TMP/result-fifo" >/dev/null 2>&1; echo $?)"
FILE="$(make_result full-suite "$HEAD")"; chmod 000 "$FILE"
assert_exit "unreadable result rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 241 "$FILE" >/dev/null 2>&1; echo $?)"
chmod 600 "$FILE"
MALFORMED="$MERGE_GUARD_STATUS_DIR/verify-full-suite-${HEAD}.green"
printf 'not-an-artifact\n' > "$MALFORMED"
assert_exit "malformed result rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 25 "$MALFORMED" >/dev/null 2>&1; echo $?)"

for key in result name branch sha at; do
  FILE="$(make_result full-suite "$HEAD")"
  sed "/^${key}=/d" "$FILE" > "$FILE.tmp" && mv "$FILE.tmp" "$FILE"
  assert_exit "missing artifact field $key rejected" 2 \
    "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 26 "$FILE" >/dev/null 2>&1; echo $?)"
done

FILE="$(make_result full-suite "$HEAD" feat/test '' RED)"
assert_exit "RED artifact rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 27 "$FILE" >/dev/null 2>&1; echo $?)"
FILE="$(make_result full-suite "$HEAD" feat/test '' UNKNOWN)"
assert_exit "UNKNOWN artifact rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 28 "$FILE" >/dev/null 2>&1; echo $?)"

for key in result name branch sha at; do
  FILE="$(make_result full-suite "$HEAD")"
  value="$(sed -n "s/^${key}=//p" "$FILE")"
  printf '%s=%s\n' "$key" "$value" >> "$FILE"
  assert_exit "duplicate artifact field $key rejected" 2 \
    "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 291 "$FILE" >/dev/null 2>&1; echo $?)"
done

for mutation in unknown blank malformed whitespace; do
  FILE="$(make_result full-suite "$HEAD")"
  case "$mutation" in
    unknown) printf 'token=do-not-print-me\n' >> "$FILE" ;;
    blank) printf '\n' >> "$FILE" ;;
    malformed) printf 'broken-record\n' >> "$FILE" ;;
    whitespace) printf 'name=full suite\n' >> "$FILE" ;;
  esac
  assert_exit "$mutation artifact record rejected" 2 \
    "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 29 "$FILE" >/dev/null 2>&1; echo $?)"
done

FILE="$(make_result full-suite 'ABC1234DEF5678ABC1234DEF5678ABC1234DEF56')"
assert_exit "uppercase SHA rejected" 2 \
  "$(TEST_GH_HEAD_SHA='ABC1234DEF5678ABC1234DEF5678ABC1234DEF56' bash "$GUARD" --record-green 30 "$FILE" >/dev/null 2>&1; echo $?)"
FILE="$(make_result full-suite 'abc123')"
assert_exit "truncated SHA rejected" 2 \
  "$(TEST_GH_HEAD_SHA='abc123' bash "$GUARD" --record-green 31 "$FILE" >/dev/null 2>&1; echo $?)"
NONHEX="gggggggggggggggggggggggggggggggggggggggg"
FILE="$(make_result full-suite "$NONHEX")"
assert_exit "nonhex SHA rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$NONHEX" bash "$GUARD" --record-green 311 "$FILE" >/dev/null 2>&1; echo $?)"
FILE="$(make_result full-suite "$HEAD" 'feat/other')"
assert_exit "artifact branch mismatch rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 32 "$FILE" >/dev/null 2>&1; echo $?)"
FILE="$(make_result unknown-suite "$HEAD")"
assert_exit "unconfigured verification rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 33 "$FILE" >/dev/null 2>&1; echo $?)"
FILE="$(make_result 'bad name' "$HEAD")"
assert_exit "unsafe verification name rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 331 "$FILE" >/dev/null 2>&1; echo $?)"
FILE="$(make_result full-suite "$HEAD")"; cp "$FILE" "$TMP/wrong-location"
assert_exit "artifact outside canonical status directory rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 34 "$TMP/wrong-location" >/dev/null 2>&1; echo $?)"
FILE="$(make_result full-suite "$HEAD")"; cp "$FILE" "$MERGE_GUARD_STATUS_DIR/wrong-name.green"
assert_exit "artifact with noncanonical basename rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 35 "$MERGE_GUARD_STATUS_DIR/wrong-name.green" >/dev/null 2>&1; echo $?)"

STALE_AT="$(date -u -r $(( $(date -u +%s) - 3601 )) +%FT%TZ 2>/dev/null || date -u -d '@'$(( $(date -u +%s) - 3601 )) +%FT%TZ)"
FILE="$(make_result full-suite "$HEAD" feat/test "$STALE_AT")"
assert_exit "stale GREEN artifact rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 36 "$FILE" >/dev/null 2>&1; echo $?)"
FILE="$(make_result full-suite "$HEAD" feat/test 'not-a-time')"
assert_exit "invalid artifact timestamp rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 37 "$FILE" >/dev/null 2>&1; echo $?)"
FUTURE_AT="$(date -u -r $(( $(date -u +%s) + 120 )) +%FT%TZ 2>/dev/null || date -u -d '@'$(( $(date -u +%s) + 120 )) +%FT%TZ)"
FILE="$(make_result full-suite "$HEAD" feat/test "$FUTURE_AT")"
assert_exit "future artifact timestamp rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 38 "$FILE" >/dev/null 2>&1; echo $?)"
FIXED_NOW=2000000000
BOUNDARY_AT="$(date -u -r $((FIXED_NOW - 3600)) +%FT%TZ 2>/dev/null || date -u -d '@'$((FIXED_NOW - 3600)) +%FT%TZ)"
FILE="$(make_result full-suite "$HEAD" feat/test "$BOUNDARY_AT")"
mkdir -p "$TMP/fake-bin"
cat > "$TMP/fake-bin/date" <<'SH'
#!/usr/bin/env bash
if [ "$#" -eq 2 ] && [ "$1" = "-u" ] && [ "$2" = "+%s" ]; then
  printf '2000000000\n'
else
  exec /bin/date "$@"
fi
SH
chmod +x "$TMP/fake-bin/date"
assert_exit "freshness exact max-age boundary accepted" 0 \
  "$(PATH="$TMP/fake-bin:$PATH" TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 39 "$FILE" >/dev/null 2>&1; echo $?)"
assert_exit "invalid freshness policy rejected" 2 \
  "$(MERGE_GUARD_MAX_AGE_SECONDS=oops TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 391 "$FILE" >/dev/null 2>&1; echo $?)"
printf 'keep\n' > "$MERGE_GUARD_STATUS_DIR/pr-999.green"
assert_exit "unsafe PR traversal rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green '../999' "$FILE" >/dev/null 2>&1; echo $?)"
assert_true "unsafe PR does not touch unrelated marker" grep -q '^keep$' "$MERGE_GUARD_STATUS_DIR/pr-999.green"
printf 'all-green old-marker\n' > "$MERGE_GUARD_STATUS_DIR/pr-392.green"
printf 'broken\n' > "$FILE"
assert_exit "failed artifact validation clears old marker" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 392 "$FILE" >/dev/null 2>&1; echo $?)"
assert_true "failed artifact leaves no marker" test ! -e "$MERGE_GUARD_STATUS_DIR/pr-392.green"
FILE="$(make_result full-suite "$HEAD")"
assert_exit "PR movement between identity reads rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" TEST_GH_HEAD_SHA_SECOND='1111111111111111111111111111111111111111' bash "$GUARD" --record-green 393 "$FILE" >/dev/null 2>&1; echo $?)"
assert_true "movement during record leaves no marker" test ! -e "$MERGE_GUARD_STATUS_DIR/pr-393.green"
: > "$TEST_GH_STATE_DIR/repo-calls"
FILE="$(make_result full-suite "$HEAD")"
assert_exit "repository movement between identity reads rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" TEST_GH_REPO_SECOND=other/r bash "$GUARD" --record-green 394 "$FILE" >/dev/null 2>&1; echo $?)"
assert_true "repository movement leaves no marker" test ! -e "$MERGE_GUARD_STATUS_DIR/pr-394.green"
FILE2="$(make_result second-suite "$HEAD")"
assert_exit "one canonical artifact for another configured verification accepted" 0 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 394 "$FILE2" >/dev/null 2>&1; echo $?)"
FILE="$(make_result full-suite "$HEAD")"
assert_exit "caller plugin version cannot override manifests" 0 \
  "$(MERGE_GUARD_PLUGIN_VERSION='bad version' TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 395 "$FILE" >/dev/null 2>&1; echo $?)"
assert_true "recorded plugin version comes from manifests" grep -q 'plugin_version=1.0.0' "$MERGE_GUARD_STATUS_DIR/pr-395.green"
assert_exit "marker publication failure leaves no marker" 2 \
  "$(MERGE_GUARD_FORCE_PUBLISH_FAILURE=1 TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 396 "$FILE" >/dev/null 2>&1; echo $?)"
assert_true "failed marker publication is absent" test ! -e "$MERGE_GUARD_STATUS_DIR/pr-396.green"
mkdir -p "$TMP/fake-gh"; cat > "$TMP/fake-gh/gh" <<'SH'
#!/usr/bin/env bash
exit 1
SH
chmod +x "$TMP/fake-gh/gh"
assert_exit "GitHub identity lookup failure rejected" 2 \
  "$(env -u TEST_GH_HEAD_SHA PATH="$TMP/fake-gh:$PATH" bash "$GUARD" --record-green 397 "$FILE" >/dev/null 2>&1; echo $?)"

# A caller-controlled identity must never replace the GitHub response. The
# fake gh is the established lookup seam and deliberately disagrees with the
# legacy MERGE_GUARD_PR_* environment values.
cat > "$TMP/fake-gh/gh" <<'SH'
#!/usr/bin/env bash
printf 'feat/authoritative\t1111111111111111111111111111111111111111\tdevelop\tbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n'
SH
chmod +x "$TMP/fake-gh/gh"
FILE="$(make_result full-suite "$HEAD")"
assert_exit "caller PR identity cannot override GitHub" 2 \
  "$(PATH="$TMP/fake-gh:$PATH" MERGE_GUARD_PR_HEAD_BRANCH=feat/test MERGE_GUARD_PR_HEAD_SHA="$HEAD" MERGE_GUARD_PR_BASE_BRANCH=develop MERGE_GUARD_PR_BASE_SHA=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb bash "$GUARD" --record-green 3971 "$FILE" >/dev/null 2>&1; echo $?)"
cp "$TMP/repo/.orchestration/config.yaml" "$TMP/config.good"
printf 'schema_version: 99\n' > "$TMP/repo/.orchestration/config.yaml"
assert_exit "invalid config blocks recording" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 398 "$FILE" >/dev/null 2>&1; echo $?)"
assert_exit "invalid config blocks hook merge" 2 "$(run Bash 'gh pr merge 398 --merge')"
mv "$TMP/config.good" "$TMP/repo/.orchestration/config.yaml"

# 4. A fresh marker is valid evidence for the sanctioned wrapper, but never
#    authorizes a raw shell merge.
record_green 43 "$HEAD"
assert_exit "host-neutral assertion accepts exact identity" 0 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --assert-green 43 feat/test >/dev/null 2>&1; echo $?)"
assert_exit "merge with valid fresh marker still blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 43 --merge --match-head-commit abc1234def5678abc1234def5678abc1234def56')"

# 5. Same marker, but the branch has moved (HEAD sha differs) -> blocked.
record_green 44 "$HEAD"
assert_exit "merge with moved sha blocked" 2 \
  "$(export TEST_GH_HEAD_SHA='9999999999999999999999999999999999999999'; run Bash 'gh pr merge 44 --merge')"

# 6. Marker sha matches but it is older than MAX_AGE -> blocked.
record_green 45 "$HEAD"
sed -e 's/recorded_at=[^ ]*/recorded_at=2020-01-01T00:00:00Z/' \
    -e 's/verification_at=[^ ]*/verification_at=2020-01-01T00:00:00Z/' \
  "$MERGE_GUARD_STATUS_DIR/pr-45.green" > "$TMP/expired-marker"
mv "$TMP/expired-marker" "$MERGE_GUARD_STATUS_DIR/pr-45.green"
assert_exit "merge with expired marker blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 45 --merge')"

# Exact identity and plugin binding: any reader change invalidates the marker.
record_green 47 "$HEAD"
assert_exit "changed head branch blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD" TEST_GH_HEAD_BRANCH='feat/other'; run Bash 'gh pr merge 47 --merge')"
assert_exit "changed base sha blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD" TEST_GH_BASE_SHA='cccccccccccccccccccccccccccccccccccccccc'; run Bash 'gh pr merge 47 --merge')"
assert_exit "changed plugin version blocked" 2 \
  "$(sed 's/\"version\": \"1.0.0\"/\"version\": \"1.0.1\"/' "$TMP/.codex-plugin/plugin.json" > "$TMP/codex-next.json"; sed 's/\"version\": \"1.0.0\"/\"version\": \"1.0.1\"/' "$TMP/.claude-plugin/plugin.json" > "$TMP/claude-next.json"; mv "$TMP/codex-next.json" "$TMP/.codex-plugin/plugin.json"; mv "$TMP/claude-next.json" "$TMP/.claude-plugin/plugin.json"; export TEST_GH_HEAD_SHA="$HEAD"; code="$(run Bash 'gh pr merge 47 --merge')"; cp "$HERE/../.codex-plugin/plugin.json" "$TMP/.codex-plugin/plugin.json"; cp "$HERE/../.claude-plugin/plugin.json" "$TMP/.claude-plugin/plugin.json"; echo "$code")"

# 7. Every raw merge form is blocked, marker or not.
record_green 46 "$HEAD"
assert_exit "merge --base main blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --base main --merge')"
assert_exit "merge --base=main blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --base=main --merge')"
assert_exit "merge --squash blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --squash --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "merge short -s blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 -s --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "actual supported --merge blocked with marker" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --merge --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "canonical PR URL with exact pin blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge https://github.com/o/r/pull/46/ -m --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "same current repo option with exact pin blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --repo o/r -m --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "same current repo comparison remains blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge https://github.com/O/R/pull/46 -m --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "caller GH_REPO raw merge remains blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD" GH_REPO=other/r; run Bash 'gh pr merge https://github.com/o/r/pull/46 -m --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "caller GH_HOST raw merge remains blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD" GH_HOST=example.com; run Bash 'gh pr merge https://github.com/o/r/pull/46 -m --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "same-number cross-repo URL blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge https://github.com/other/r/pull/46 -m --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "same-number cross-repo option blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --repo other/r -m --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "repository identity lookup failure blocks" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD" TEST_GH_REPO_FAIL=1; run Bash 'gh pr merge 46 --repo o/r -m --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "attached short base matching live base remains blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 -Bdevelop -m --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "leading merge before trailing operator blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --merge --match-head-commit abc1234def5678abc1234def5678abc1234def56 && echo done')"
assert_exit "quoted policy-like payload cannot authorize raw merge" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --merge --subject "--squash --base main" --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "raw merge without head pin blocked despite marker" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --merge')"
assert_exit "raw merge with wrong head pin blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --merge --match-head-commit 1111111111111111111111111111111111111111')"
assert_exit "raw merge with duplicate head pin blocked" 2 \
  "$(export TEST_GH_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --merge --match-head-commit abc1234def5678abc1234def5678abc1234def56 --match-head-commit abc1234def5678abc1234def5678abc1234def56')"
assert_exit "inline GH_REPO raw merge blocked without lookup" 2 \
  "$(run Bash 'GH_REPO=other/r gh pr merge 46 --merge')"
assert_exit "env GH_HOST raw merge blocked without lookup" 2 \
  "$(run Bash 'env GH_HOST=example.com gh pr merge 46 --merge')"
assert_exit "preceding cd raw merge blocked" 2 \
  "$(run Bash 'cd /tmp && gh pr merge 46 --merge')"

# Git-valid ref characters are data, not marker separators. The guard validates
# branch fields with Git's own ref-format rules.
export TEST_GH_HEAD_BRANCH='feat/a=b%+,测试'
FILE="$(make_result full-suite "$HEAD" 'feat/a=b%+,测试')"
assert_exit "valid equals percent plus comma Unicode branch records" 0 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 461 "$FILE" >/dev/null 2>&1; echo $?)"
export TEST_GH_HEAD_BRANCH='feat/test'

# Strict marker snapshots reject malformed, duplicate, unknown, or inconsistent
# provenance without rereading the source artifact.
record_green 60 "$HEAD"
cp "$MERGE_GUARD_STATUS_DIR/pr-60.green" "$TMP/valid-marker"
assert_true "marker snapshots authoritative repository" grep -q ' repo=o/r ' "$TMP/valid-marker"
printf 'all-green broken\n' > "$MERGE_GUARD_STATUS_DIR/pr-60.green"
assert_exit "malformed marker rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --assert-green 60 >/dev/null 2>&1; echo $?)"
cp "$TMP/valid-marker" "$MERGE_GUARD_STATUS_DIR/pr-60.green"; sed 's/ pr=/ extra=x pr=/' "$TMP/valid-marker" > "$MERGE_GUARD_STATUS_DIR/pr-60.green"
assert_exit "unknown marker field rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --assert-green 60 >/dev/null 2>&1; echo $?)"
cp "$TMP/valid-marker" "$MERGE_GUARD_STATUS_DIR/pr-60.green"; sed 's/ pr=/ pr=60 pr=/' "$TMP/valid-marker" > "$MERGE_GUARD_STATUS_DIR/pr-60.green"
assert_exit "duplicate marker field rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --assert-green 60 >/dev/null 2>&1; echo $?)"
cp "$TMP/valid-marker" "$MERGE_GUARD_STATUS_DIR/pr-60.green"; sed 's/ repo=o\/r / repo=other\/r /' "$TMP/valid-marker" > "$MERGE_GUARD_STATUS_DIR/pr-60.green"
assert_exit "cross-repository marker rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --assert-green 60 >/dev/null 2>&1; echo $?)"
cp "$TMP/valid-marker" "$MERGE_GUARD_STATUS_DIR/pr-60.green"; sed 's/verification_sha=[^ ]*/verification_sha=1111111111111111111111111111111111111111/' "$TMP/valid-marker" > "$MERGE_GUARD_STATUS_DIR/pr-60.green"
assert_exit "artifact-inconsistent marker rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --assert-green 60 >/dev/null 2>&1; echo $?)"
cp "$TMP/valid-marker" "$MERGE_GUARD_STATUS_DIR/pr-60.green"; sed 's/recorded_at=[^ ]*/recorded_at=2020-01-01T00:00:00+00:00/' "$TMP/valid-marker" > "$MERGE_GUARD_STATUS_DIR/pr-60.green"
assert_exit "noncanonical marker timestamp rejected" 2 \
  "$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --assert-green 60 >/dev/null 2>&1; echo $?)"

FILE="$(make_result full-suite "$HEAD")"; printf 'secret=super-sensitive-value\n' >> "$FILE"
DIAG="$(TEST_GH_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 61 "$FILE" 2>&1 || true)"
assert_true "artifact rejection diagnostics do not expose contents" sh -c '! printf "%s" "$1" | grep -q super-sensitive-value' sh "$DIAG"

# 8. A non-Bash tool is ignored.
assert_exit "non-Bash tool ignored" 0 "$(run Read 'gh pr merge 42')"

# 9. Fail-closed fallback (no python3): still blocks a no-marker merge (PR 99,
#    which has no marker), still allows a plainly non-merge command.
assert_exit "fallback blocks no-marker merge" 2 \
  "$(export MERGE_GUARD_FORCE_FALLBACK=1; run Bash 'gh pr merge 99 --merge')"
assert_exit "fallback blocks Bash even for apparent non-merge" 2 \
  "$(export MERGE_GUARD_FORCE_FALLBACK=1; run Bash 'git status')"
assert_exit "fallback blocks quoted nested interpreter merge" 2 \
  "$(export MERGE_GUARD_FORCE_FALLBACK=1; run Bash 'bash -lc "gh pr merge 99 --merge"')"
assert_exit "fallback blocks malformed JSON" 2 \
  "$(printf '{bad json' | MERGE_GUARD_FORCE_FALLBACK=1 bash "$GUARD" >/dev/null 2>&1; echo $?)"
mv "$TMP/repo/merge-command-classifier.py" "$TMP/classifier.saved"
assert_exit "missing classifier blocks initialized hook" 2 "$(run Bash 'git status')"
printf '#!/usr/bin/env python3\nprint("schema=wrong")\n' > "$TMP/repo/merge-command-classifier.py"
assert_exit "malformed classifier output blocks initialized hook" 2 "$(run Bash 'git status')"
printf '#!/usr/bin/env python3\nraise SystemExit(9)\n' > "$TMP/repo/merge-command-classifier.py"
assert_exit "classifier failure blocks initialized hook" 2 "$(run Bash 'git status')"
mv "$TMP/classifier.saved" "$TMP/repo/merge-command-classifier.py"
assert_exit "invalid hook JSON fails closed" 2 \
  "$(printf '{bad json' | bash "$GUARD" >/dev/null 2>&1; echo $?)"
assert_exit "missing tool_input fails closed" 2 \
  "$(printf '{"tool_name":"Bash"}' | bash "$GUARD" >/dev/null 2>&1; echo $?)"
assert_exit "non-string command fails closed" 2 \
  "$(printf '{"tool_name":"Bash","tool_input":{"command":7}}' | bash "$GUARD" >/dev/null 2>&1; echo $?)"
assert_exit "unknown tool identity fails closed" 2 \
  "$(printf '{"tool_input":{"command":"git status"}}' | bash "$GUARD" >/dev/null 2>&1; echo $?)"
assert_exit "forced decode failure fails closed" 2 \
  "$(export MERGE_GUARD_FORCE_DECODE_FAILURE=1; payload Bash 'git status' | bash "$GUARD" >/dev/null 2>&1; echo $?)"

# 10. Plugin hook mode is inert until a repo opts in with orchestration config.
EMPTY="$TMP/empty-repo"
mkdir -p "$EMPTY"
git -C "$EMPTY" init -q
unset MERGE_GUARD_STATUS_DIR
payload Bash 'git status' | (cd "$EMPTY" && bash "$HERE/../scripts/merge-guard.sh") >/dev/null 2>&1
assert_exit "uninitialized repo non-merge hook no-ops" 0 "$?"
payload Bash 'gh pr merge 123 --merge' | (cd "$EMPTY" && bash "$HERE/../scripts/merge-guard.sh") >/dev/null 2>&1
assert_exit "uninitialized repo merge hook no-ops" 0 "$?"
assert_true "uninitialized repo hook creates no .orchestration dir" test ! -e "$EMPTY/.orchestration"
export MERGE_GUARD_STATUS_DIR="$TMP/markers"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
