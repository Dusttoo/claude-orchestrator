#!/usr/bin/env python3
"""Create controller-consumable Jira inventory evidence from raw REST page responses."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def query_artifact(kind: str, jql: str, responses: list[dict[str, Any]]) -> dict[str, Any]:
    pages = []
    for response in responses:
        issues = response.get("issues")
        if not isinstance(issues, list):
            raise ValueError(f"{kind} Jira response has no issues array")
        pages.append({
            "start_at": response.get("startAt"),
            "count": len(issues),
            "total": response.get("total"),
            "item_keys": [str(issue["key"]).upper() for issue in issues],
            "terminal": bool(response.get("isLast", False)),
        })
    return {"kind": kind, "jql": jql, "pages": pages}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--parent-pages", required=True)
    parser.add_argument("--child-pages", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    parents = json.loads(Path(args.parent_pages).read_text(encoding="utf-8"))
    children = json.loads(Path(args.child_pages).read_text(encoding="utf-8"))
    if not isinstance(parents, list) or not isinstance(children, list):
        raise ValueError("Jira page inputs must be arrays of raw REST responses")
    fetch_id = "jirafetch_" + uuid.uuid4().hex
    relations = []
    child_parents: dict[str, str] = {}
    for response in parents:
        for issue in response.get("issues", []):
            parent = str(issue["key"]).upper()
            for child in ((issue.get("fields") or {}).get("subtasks") or []):
                relations.append({"parent": parent, "child": str(child["key"]).upper()})
    for response in children:
        for issue in response.get("issues", []):
            child = str(issue["key"]).upper()
            parent = str((((issue.get("fields") or {}).get("parent") or {}).get("key")) or "").upper()
            child_parents[child] = parent
    artifact = {
        "schema_version": 1,
        "adapter": "jira-rest-v3",
        "fetch_id": fetch_id,
        "queries": [
            query_artifact("parents", inventory["source_query"], parents),
            query_artifact("children", inventory["subtask_source_query"], children),
        ],
        "relations": sorted(relations, key=lambda item: (item["parent"], item["child"])),
        "child_parents": dict(sorted(child_parents.items())),
    }
    artifact_path = Path(args.artifact).resolve()
    write_json(artifact_path, artifact)
    inventory["fetch_artifact"] = {"path": str(artifact_path), "fetch_id": fetch_id}
    write_json(Path(args.output), inventory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
