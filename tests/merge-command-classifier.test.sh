#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLASSIFIER="$HERE/../scripts/merge-command-classifier.py"
fails=0

check() { # description decision command
  local desc="$1" expected="$2" command="$3" output actual
  output="$(printf '%s' "$command" | python3 "$CLASSIFIER" --command 2>/dev/null)"
  actual="$(printf '%s\n' "$output" | sed -n 's/^decision=//p')"
  if [ "$actual" = "$expected" ]; then printf 'ok   %s\n' "$desc"
  else printf 'FAIL %s: want %s got %s\n' "$desc" "$expected" "${actual:-<none>}"; fails=$((fails + 1)); fi
}

check_field() { # description field expected command
  local desc="$1" field="$2" expected="$3" command="$4" output actual
  output="$(printf '%s' "$command" | python3 "$CLASSIFIER" --command 2>/dev/null)"
  actual="$(printf '%s\n' "$output" | sed -n "s/^${field}=//p")"
  if [ "$actual" = "$expected" ]; then printf 'ok   %s\n' "$desc"
  else printf 'FAIL %s: want [%s] got [%s]\n' "$desc" "$expected" "$actual"; fails=$((fails + 1)); fi
}

SCHEMA_OUTPUT="$(printf '%s' 'git status' | python3 "$CLASSIFIER" --command 2>/dev/null)"
check_field "fixed classifier schema is v2" schema merge-command-classifier/v2 'git status'
if [ "$(printf '%s\n' "$SCHEMA_OUTPUT" | wc -l | tr -d ' ')" = 8 ]; then
  printf 'ok   fixed classifier schema has eight records\n'
else
  printf 'FAIL fixed classifier schema does not have eight records\n'; fails=$((fails + 1))
fi

# Historical harmless data and malformed incidental prose remain allowed.
check "commit body is data" allow 'git commit -m "gh pr merge 12 --merge"'
check "PR body is data" allow 'gh pr create --body "gh pr merge 12 --merge"'
check "comment is data" allow 'git status # gh pr merge 12 --merge'
check "echo is data" allow 'echo "gh pr merge 12 --merge'
check "printf is data" allow 'printf "%s" "gh pr merge 12 --merge"'
check "literal harmless redirection is safe data" allow 'printf "%s" ok > result.txt'

# Supported executable forms and normalized arguments.
PIN=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
check "direct merge classified" merge "gh pr merge 12 --merge --match-head-commit $PIN"
check "compound merge classified" merge "true && gh pr merge 12 -m --match-head-commit $PIN"
check "newline merge classified" merge $'true\ngh pr merge 12 --match-head-commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
check "combined shell c is outside safe subset" block "bash -lc 'gh pr merge 12 --merge --match-head-commit $PIN'"
check "exec wrapper classified" merge "exec gh pr merge 12 --merge --match-head-commit $PIN"
check "case arm is outside safe subset" block "case x in x) gh pr merge 12 --merge --match-head-commit $PIN ;; esac"
check "if condition classified" merge "if gh pr merge 12 --merge --match-head-commit $PIN; then true; fi"
check "while body classified" merge "while false; do gh pr merge 12 --merge --match-head-commit $PIN; done"
check_field "short squash normalized" strategy squash "gh pr merge 12 -s --match-head-commit $PIN"
check_field "short rebase normalized" strategy rebase "gh pr merge 12 -r --match-head-commit $PIN"
check_field "attached short base normalized" base 'feat/a=b%+,测试' "gh pr merge 12 -Bfeat/a=b%+,测试 --merge --match-head-commit $PIN"
check_field "canonical URL normalizes PR number" pr 12 "gh pr merge https://github.com/o/r/pull/12/ --merge --match-head-commit $PIN"
check_field "canonical URL normalizes repository" repo o/r "gh pr merge https://github.com/o/r/pull/12/ --merge --match-head-commit $PIN"
check_field "repo option normalizes repository" repo o/r "gh pr merge 12 --repo o/r --merge --match-head-commit $PIN"
check "body option value remains data" merge "gh pr merge 12 --body 'gh pr merge 99 --squash' --merge --match-head-commit $PIN"
check "all data option values are consumed" merge "gh pr merge 12 --body x --body-file f --subject s --author-email a@b --repo o/r --merge --match-head-commit $PIN"
check "assignment command env wrappers classified" merge "A=x command env B=y gh pr merge 12 --merge --match-head-commit $PIN"
check "double-dash wrapper classified" merge "command -- env -- exec -- gh pr merge 12 --merge --match-head-commit $PIN"
check "bash c terminator is outside safe subset" block "bash -c -- 'gh pr merge 12 --merge --match-head-commit $PIN'"
check "sh c terminator is outside safe subset" block "sh -c -- 'gh pr merge 12 --merge --match-head-commit $PIN'"
check "dash c terminator is outside safe subset" block "dash -c -- 'gh pr merge 12 --merge --match-head-commit $PIN'"
check "zsh c terminator is outside safe subset" block "zsh -c -- 'gh pr merge 12 --merge --match-head-commit $PIN'"
check "ksh c terminator is outside safe subset" block "ksh -c -- 'gh pr merge 12 --merge --match-head-commit $PIN'"
check "bash shopt form is outside safe subset" block "bash -c -O extglob 'gh pr merge 12 --merge --match-head-commit $PIN'"
check "bash shell-option form is outside safe subset" block "bash -c -o errexit 'gh pr merge 12 --merge --match-head-commit $PIN'"
check "wrapped bash form is outside safe subset" block "A=x command env B=y exec bash -c -O extglob 'gh pr merge 12 --merge --match-head-commit $PIN'"
check "backslash newline cannot split gh" merge $'g\\\nh pr merge 12 --merge --match-head-commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
check "backslash newline cannot split pr" merge $'gh p\\\nr merge 12 --merge --match-head-commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
check "backslash newline cannot hide merge" merge $'gh pr \\\nmerge 12 --merge --match-head-commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'

# Ambiguous, malformed, unsupported, or over-budget input blocks.
check "multiple merges block" block "gh pr merge 12 --merge --match-head-commit $PIN & gh pr merge 13 --merge --match-head-commit $PIN"
check "duplicate strategy blocks" block "gh pr merge 12 -m -s --match-head-commit $PIN"
check "duplicate base blocks" block "gh pr merge 12 -Bdevelop --base=main --match-head-commit $PIN"
check "missing option value blocks" block 'gh pr merge 12 --base'
check "missing repo option value blocks" block 'gh pr merge 12 --repo'
check "malformed repo option blocks" block "gh pr merge 12 --repo owner --merge --match-head-commit $PIN"
check "duplicate repo options block" block "gh pr merge 12 --repo o/r --repo o/r --merge --match-head-commit $PIN"
check "URL plus repo option blocks" block "gh pr merge https://github.com/o/r/pull/12 --repo o/r --merge --match-head-commit $PIN"
check "cross-host URL blocks" block "gh pr merge https://example.com/o/r/pull/12 --merge --match-head-commit $PIN"
check "unknown flag blocks" block "gh pr merge 12 --wat --match-head-commit $PIN"
check "unsupported selector blocks" block "gh pr merge feature-name --merge --match-head-commit $PIN"
check "unsupported eval merge blocks" block 'eval "gh pr merge 12 --merge"'
check "dynamic eval blocks" block 'eval "$COMMAND"'
check "unsupported execution wrapper blocks" block "xargs gh pr merge 12 --merge --match-head-commit $PIN"
check "unsupported wrapper option with merge blocks" block "env -S 'gh pr merge 12 --merge --match-head-commit $PIN'"
check "missing Bash option operand with merge blocks" block "bash -c -O 'gh pr merge 12 --merge --match-head-commit $PIN'"
check "unknown Bash option with merge blocks" block "bash -c --unknown 'gh pr merge 12 --merge --match-head-commit $PIN'"
check "nested substitution is outside safe subset" block "printf '%s' \"\$(gh pr merge 12 --merge --match-head-commit $PIN)\""
check "malformed executable substitution blocks" block 'x=$(gh pr merge 12 --merge'
check "globbed command position blocks" block '/opt/homebrew/bin/g? pr merge 12 --merge'
check "wrapped globbed command position blocks" block 'command /opt/homebrew/bin/g? pr merge 12 --merge'
check "brace-expanded command position blocks" block 'g{h,x} pr merge 12 --merge'
check "subshell grouping blocks conservatively" block '(git status)'
check "parameter-expanded command position blocks" block '$cmd pr merge 12 --merge'
check "source dispatch blocks" block 'source ./merge-command.sh'
check "dot dispatch blocks" block '. ./merge-command.sh'
check "harmless heredoc blocks conservatively" block $'cat <<\'EOF\'\ngh pr merge 12 --merge\nEOF'
check "executable heredoc blocks conservatively" block $'bash <<\'EOF\'\ngh pr merge 12 --merge\nEOF'
check "here string blocks conservatively" block 'bash <<< "gh pr merge 12 --merge"'
check "process substitution blocks" block 'cat <(printf "%s" "gh pr merge 12 --merge")'
check "redirected merge is denied" block "gh pr merge 12 --merge --match-head-commit $PIN > result.txt"
check "leading redirection is outside safe subset" block '> result.txt gh pr merge 12 --merge'
check "assignment then leading redirection blocks" block 'A=x > result.txt gh pr merge 12 --merge'
check "leading file-descriptor redirection blocks" block '2> result.txt gh pr merge 12 --merge'
check "control-flow leading redirection blocks" block 'if true; then > result.txt gh pr merge 12 --merge; fi'
check "depth limit blocks" block 'x=$(x=$(x=$(x=$(x=$(x=$(x=$(x=$(x=$(gh pr merge 12 --merge))))))))))'
check "token limit blocks" block "$(printf 'x %.0s' {1..5000}) gh pr merge 12 --merge"
check "input limit blocks" block "$(printf 'x%.0s' {1..70000})"

HOOK_OUTPUT="$(printf '%s' '{"tool_name":"Bash","tool_name":"Read","tool_input":{"command":"git status"}}' | python3 "$CLASSIFIER")"
if printf '%s\n' "$HOOK_OUTPUT" | grep -q '^decision=block$'; then
  printf 'ok   duplicate hook JSON keys block\n'
else
  printf 'FAIL duplicate hook JSON keys did not block\n'; fails=$((fails + 1))
fi

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
