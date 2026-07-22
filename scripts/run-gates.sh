#!/usr/bin/env bash
# run-gates.sh -- the self-check gate. Runs every `self_check` command declared in
# .orchestration/config.yaml, in order, inside the current worktree. Reports a
# per-check result and exits 0 only if every check passes.
#
# This gate is BLOCKING: any failing check exits non-zero, which stops the
# pipeline before code review. It runs every check first (rather than failing
# fast) so a single invocation surfaces all failures at once.
#
# The checks are entirely config-driven; the framework hardcodes none of them.
# See templates/config.yaml -> self_check.
set -uo pipefail   # NOT -e: we run every check and report all failures

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-config.sh
. "$HERE/lib-config.sh"

# Read the checks into parallel name/run arrays (tab-separated from the parser).
names=(); runs=()
while IFS=$'\t' read -r name run; do
  [ -n "$name" ] || continue
  names+=("$name"); runs+=("$run")
done < <(orch_selfchecks)

if [ "${#names[@]}" -eq 0 ]; then
  echo "run-gates: no self_check entries in $(orch_config_file)." >&2
  echo "run-gates: define at least one check (typecheck/build/unit) before gating." >&2
  exit 2
fi

pass=0; fail=0
declare -a RESULTS

for i in "${!names[@]}"; do
  name="${names[$i]}"; run="${runs[$i]}"
  echo "--- ${name} ---"
  echo "\$ ${run}"
  if bash -c "$run"; then
    RESULTS+=("PASS  ${name}"); pass=$((pass + 1))
  else
    RESULTS+=("FAIL  ${name}"); fail=$((fail + 1))
  fi
  echo
done

echo "==== SELF-CHECK GATE ===="
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo "  ${pass} passed, ${fail} failed"

[ "$fail" -eq 0 ]
