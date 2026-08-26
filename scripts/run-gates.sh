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
LOG_LINES="$(orch_get gate_log_lines 80)"
if ! [[ "$LOG_LINES" =~ ^[1-9][0-9]*$ ]]; then
  echo "run-gates: gate_log_lines must be a positive integer." >&2
  exit 2
fi
LOG_DIR="$(orch_project_root)/.orchestration/.gate-logs"
mkdir -p "$LOG_DIR"

for i in "${!names[@]}"; do
  name="${names[$i]}"; run="${runs[$i]}"
  safe_name="$(printf '%s' "$name" | tr -cs 'A-Za-z0-9._-' '-')"
  log_file="${LOG_DIR}/${safe_name}-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
  echo "--- ${name} ---"
  echo "\$ ${run}"
  if bash -c "$run" >"$log_file" 2>&1; then
    rm -f "$log_file"
    RESULTS+=("PASS  ${name}"); pass=$((pass + 1))
  else
    code=$?
    chmod 600 "$log_file" 2>/dev/null || true
    echo "FAILED (exit ${code}); last ${LOG_LINES} log lines:" >&2
    tail -n "$LOG_LINES" "$log_file" >&2
    echo "Full failure log: ${log_file}" >&2
    RESULTS+=("FAIL  ${name}"); fail=$((fail + 1))
  fi
  echo
done

echo "==== SELF-CHECK GATE ===="
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo "  ${pass} passed, ${fail} failed"

[ "$fail" -eq 0 ]
