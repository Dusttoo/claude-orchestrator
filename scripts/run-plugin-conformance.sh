#!/usr/bin/env bash
# run-plugin-conformance.sh -- verify plugin-owned safety mechanisms without
# installing reusable test copies in a target repository.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

rc=0
for suite in merge-guard.test.sh worktree.test.sh; do
  echo "########## plugin conformance: $suite ##########"
  bash "$ROOT/tests/$suite" || rc=1
  echo
done
exit "$rc"
