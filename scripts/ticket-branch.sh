#!/usr/bin/env bash
# ticket-branch.sh -- resolve one ticket branch from durable caller context.
#
# The run identity is supplied by the caller in ORCH_RUN_ID. The local record
# this script writes is a mirror/checkpoint, not the authority that creates a
# run identity: any host can supply the same opaque ID without Dust knowing that
# host's provider or protocol.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-config.sh
. "$HERE/lib-config.sh"

usage() {
  echo "usage: ticket-branch.sh --ticket-id <id> --ticket-title <title> --source-ref <ref> --source-sha <sha>" >&2
  exit 2
}

TICKET_ID="" TICKET_TITLE="" SOURCE_REF="" SOURCE_SHA=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --ticket-id) TICKET_ID="${2:-}"; shift 2 ;;
    --ticket-title) TICKET_TITLE="${2:-}"; shift 2 ;;
    --source-ref) SOURCE_REF="${2:-}"; shift 2 ;;
    --source-sha) SOURCE_SHA="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$TICKET_ID" ] && [ -n "$TICKET_TITLE" ] && [ -n "$SOURCE_REF" ] && [ -n "$SOURCE_SHA" ] || usage

TEMPLATE="$(orch_get ticket_branch_template)"
[ -n "$TEMPLATE" ] || exit 0

RUN_ID="${ORCH_RUN_ID:-}"
if [ -z "$RUN_ID" ]; then
  echo "REFUSED: ticket_branch_template requires caller-provided ORCH_RUN_ID; do not invent one in a subagent." >&2
  exit 1
fi
if ! [[ "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "REFUSED: ORCH_RUN_ID must be an opaque filesystem-safe identifier." >&2
  exit 1
fi

ROOT="$(orch_project_root)"
RECORD_DIR="$ROOT/.orchestration/runs/$RUN_ID"
RECORD="$RECORD_DIR/ticket-branch.json"

RESULT="$(python3 - "$TEMPLATE" "$RUN_ID" "$TICKET_ID" "$TICKET_TITLE" "$SOURCE_REF" "$SOURCE_SHA" "$RECORD" <<'PY'
import json, os, re, sys, unicodedata

template, run_id, ticket_id, title, source_ref, source_sha, record = sys.argv[1:]
slug = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")[:80].rstrip("-") or "ticket"
values = {"run_id": run_id, "ticket_id": ticket_id, "ticket_slug": slug}
try:
    branch = template.format(**values)
except (KeyError, ValueError) as exc:
    raise SystemExit(f"REFUSED: invalid ticket_branch_template: {exc}")
if re.search(r"\{[^}]+\}", branch):
    raise SystemExit("REFUSED: ticket_branch_template contains an unsupported placeholder")
payload = {
    "run_id": run_id,
    "ticket_id": ticket_id,
    "ticket_slug": slug,
    "source_ref": source_ref,
    "source_sha": source_sha,
    "branch": branch,
}
if os.path.exists(record):
    with open(record, encoding="utf-8") as fh:
        existing = json.load(fh)
    if existing != payload:
        raise SystemExit("REFUSED: durable run mirror disagrees with caller context/template/source identity")
else:
    os.makedirs(os.path.dirname(record), exist_ok=True)
    tmp = record + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, record)
print(branch)
PY
)"

if ! git check-ref-format --branch "$RESULT" >/dev/null 2>&1; then
  echo "REFUSED: ticket_branch_template resolved to invalid git branch '$RESULT'." >&2
  exit 1
fi
printf '%s\n' "$RESULT"
