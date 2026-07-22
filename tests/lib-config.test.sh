#!/usr/bin/env bash
# lib-config.test.sh -- smoke tests for the config parser. No framework: it sets
# up a temp repo with a known config, exercises each reader, and asserts output.
# Run: bash tests/lib-config.test.sh   (exits non-zero on any failure)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HERE/../scripts/lib-config.sh"

fails=0
ok()   { printf 'ok   %s\n' "$1"; }
bad()  { printf 'FAIL %s\n     want: [%s]\n     got:  [%s]\n' "$1" "$2" "$3"; fails=$((fails + 1)); }
eq()   { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/.orchestration"
cat > "$TMP/.orchestration/config.yaml" <<'YAML'
integration_branch: develop
production_branch: main
concurrency_max: 2
ci_checks_integration:
  - TypeScript
  - Vitest
  - Build
gates:
  - code-review
  - security-review
self_check:
  - name: typecheck
    run: npx tsc --noEmit
  - name: no-hex
    run: '! grep -rqn "#[0-9a-fA-F]\{3,\}" src'
verification:
  - name: e2e
    run: npx playwright test
YAML
cd "$TMP" && git init -q >/dev/null

# shellcheck source=../scripts/lib-config.sh
. "$LIB"

eq "scalar: integration_branch" "develop" "$(orch_get integration_branch)"
eq "scalar: production_branch"   "main"    "$(orch_get production_branch)"
eq "scalar: default when absent" "X"       "$(orch_get nope X)"

eq "list: ci_checks_integration count" "3" "$(orch_list ci_checks_integration | grep -c .)"
eq "list: ci_checks first"             "TypeScript" "$(orch_list ci_checks_integration | head -1)"
eq "list: gates stops before self_check" "security-review" "$(orch_list gates | tail -1)"

eq "selfchecks: count"        "2" "$(orch_selfchecks | grep -c .)"
eq "selfchecks: first name"   "typecheck" "$(orch_selfchecks | head -1 | cut -f1)"
eq "selfchecks: first run"    "npx tsc --noEmit" "$(orch_selfchecks | head -1 | cut -f2)"
# The single-quoted literal-backslash run survives verbatim after outer-quote strip.
eq "selfchecks: literal backslash run" '! grep -rqn "#[0-9a-fA-F]\{3,\}" src' \
   "$(orch_selfchecks | sed -n 2p | cut -f2)"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
