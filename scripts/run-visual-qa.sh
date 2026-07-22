#!/usr/bin/env bash
# run-visual-qa.sh -- the visual-QA entrypoint. Fully headless, no browser
# extension, safe to run unattended.
#
# It captures a ticket's click-path, collects deterministic signals, writes PNGs +
# manifest.json, and returns a hard PASS/FAIL exit code. The visual-QA agent still
# reads the PNGs afterward for design/AC fidelity (the part that needs vision), but
# a broken page now fails the gate by itself, so a lane never merges a 500 or a
# blank screen just because no human looked.
#
# Usage:
#   # start the app first (its own process), then point BASE_URL at it:
#   BASE_URL=http://localhost:3000 \
#     scripts/run-visual-qa.sh .vqa/out /pricing /features
#
#   # authenticated surface (mints a storageState by logging in through the UI):
#   BASE_URL=http://localhost:3000 AUTH=1 \
#     VQA_EMAIL=test@example.com VQA_PASSWORD=*** \
#     scripts/run-visual-qa.sh .vqa/out /admin /admin/members
#
# Routes may also come from ROUTES_FILE (one per line).
set -euo pipefail

OUT="${1:?usage: run-visual-qa.sh <outdir> [route ...]}"
shift || true
BASE_URL="${BASE_URL:-http://localhost:3000}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$OUT"

# Collect routes from args and/or ROUTES_FILE.
ROUTES=("$@")
if [ -n "${ROUTES_FILE:-}" ] && [ -f "$ROUTES_FILE" ]; then
  while IFS= read -r line; do [ -n "$line" ] && ROUTES+=("$line"); done < "$ROUTES_FILE"
fi
if [ "${#ROUTES[@]}" -eq 0 ]; then
  echo "no routes given. Pass routes as args or via ROUTES_FILE." >&2
  exit 2
fi

# Chromium for the Playwright node API (no-op if already installed).
npx playwright install chromium >/dev/null 2>&1 || true

# Fail clearly instead of timing out per route if the app is not up.
if ! curl -sf -o /dev/null --max-time 8 "$BASE_URL"; then
  echo "ERROR: $BASE_URL is not reachable. Start the dev server first (e.g. 'npm run dev &')." >&2
  exit 3
fi

# Optional auth: mint a storageState by logging in through the UI, unless the
# caller already supplied STORAGE_STATE.
if [ "${AUTH:-0}" = "1" ] && [ -z "${STORAGE_STATE:-}" ]; then
  echo "AUTH=1: minting authenticated storageState..."
  STORAGE_STATE="$OUT/state.json"
  BASE_URL="$BASE_URL" STORAGE_STATE="$STORAGE_STATE" node "$SCRIPT_DIR/vqa-login.mjs"
  export STORAGE_STATE
fi

ROUTES_JSON=$(printf '%s\n' "${ROUTES[@]}" | python3 -c 'import sys,json;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')

set +e
BASE_URL="$BASE_URL" OUT="$OUT" ROUTES_JSON="$ROUTES_JSON" \
  ${STORAGE_STATE:+STORAGE_STATE="$STORAGE_STATE"} \
  node "$SCRIPT_DIR/vqa-capture.mjs"
CAPTURE_EXIT=$?
set -e

echo
echo "screenshots + manifest in: $OUT"
echo "Next: the visual-QA agent reads $OUT/manifest.json (deterministic verdict) and"
echo "the PNGs (design + Acceptance-Criteria comparison) before issuing its verdict."

# Propagate the deterministic verdict as the script's exit code.
exit "$CAPTURE_EXIT"
