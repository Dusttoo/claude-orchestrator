#!/usr/bin/env python3
"""Bind a sanitized, fully paginated Jira fetch to a sprint inventory receipt.

This adapter is intentionally separate from the sprint controller: connector
code writes the sanitized inventory and page metadata, then this program seals
the exact query/result boundary consumed by `sprint-controller.py sync`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--pages", required=True, help="JSON array of {start_at,count} pages")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    pages = json.loads(Path(args.pages).read_text(encoding="utf-8"))
    payload = {
        "source": "jira-client",
        "source_query": inventory["source_query"],
        "subtask_source_query": inventory["subtask_source_query"],
        "parent_keys": sorted(str(item["key"]).upper() for item in inventory["tickets"]),
        "child_keys": sorted(str(key).upper() for key in inventory["subtask_keys"]),
        "pages": pages,
    }
    inventory["fetch_receipt"] = {
        **payload,
        "sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(inventory, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
