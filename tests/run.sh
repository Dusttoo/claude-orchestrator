#!/usr/bin/env bash
# run.sh -- run every *.test.sh in this directory. Exits non-zero if any fail.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

rc=0
for t in "$HERE"/*.test.sh; do
  echo "########## ${t##*/} ##########"
  bash "$t" || rc=1
  echo
done

if [ "$rc" -eq 0 ]; then echo "== all suites passed =="; else echo "== some suites FAILED =="; fi
exit "$rc"
