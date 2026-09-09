#!/usr/bin/env python3
"""Fetch provider-owned terminal batch state and complete result content."""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def origin(url: str) -> str:
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ValueError("provider base URL must be an HTTPS origin")
    return f"{p.scheme}://{p.hostname.lower()}{':' + str(p.port) if p.port else ''}"


def request_json(base: str, path: str, provider: str) -> Any:
    approved = origin(base)
    url = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    if origin(url) != approved:
        raise ValueError("provider request escaped approved origin")
    key = os.environ.get(
        "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY", ""
    )
    if not key:
        raise ValueError(f"{provider} API key is required")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if provider == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    with urlopen(Request(url, headers=headers), timeout=30) as response:  # nosec B310 -- origin checked before/after
        if origin(response.geturl()) != approved:
            raise ValueError("provider redirect escaped approved origin")
        body = response.read()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return [json.loads(line) for line in body.splitlines() if line.strip()]


def store_raw(raw_dir: Path, value: Any) -> dict[str, str]:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = raw_dir / f"sha256-{digest}.json"
    if not path.exists():
        write_json(path, value)
    return {"path": str(path.resolve()), "sha256": digest}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--marker", required=True)
    p.add_argument("--bundle", required=True)
    p.add_argument("--test-transport", help=argparse.SUPPRESS)
    a = p.parse_args()
    marker = json.loads(Path(a.marker).read_text())
    provider = marker["provider"]
    provider_id = str(marker.get("provider_batch_id") or "")
    if not provider_id:
        raise ValueError("submitted marker requires provider_batch_id")
    raw_dir = Path(a.bundle).resolve().parent / "batch-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    authority = "provider-network"
    if a.test_transport:
        supplied = json.loads(Path(a.test_transport).read_text())
        status_response = supplied["status"]
        result_pages = supplied.get("result_pages", [])
        approved = "test-only"
        authority = "test-only"
    else:
        base = (
            os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
            if provider == "anthropic"
            else os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        )
        approved = origin(base)
        status_response = request_json(
            base,
            f"messages/batches/{provider_id}"
            if provider == "anthropic"
            else f"batches/{provider_id}",
            provider,
        )
        result_pages = []
        status = str(
            status_response.get("processing_status")
            or status_response.get("status")
            or ""
        )
        if status in {"ended", "completed"}:
            if provider == "anthropic":
                result_pages = [
                    request_json(
                        base, f"messages/batches/{provider_id}/results", provider
                    )
                ]
            else:
                file_id = str(status_response.get("output_file_id") or "")
                if not file_id:
                    raise ValueError("completed OpenAI batch has no output_file_id")
                result_pages = [
                    request_json(base, f"files/{file_id}/content", provider)
                ]
    status = str(
        status_response.get("processing_status") or status_response.get("status") or ""
    )
    if str(status_response.get("id") or "") != provider_id:
        raise ValueError("provider response does not match the requested batch id")
    if status not in {"ended", "completed", "failed", "cancelled", "expired"}:
        raise ValueError("provider batch is not terminal; uncertainty remains reserved")
    rows = []
    for page in result_pages:
        page_rows = (
            page
            if isinstance(page, list)
            else page.get("data", [])
            if isinstance(page, dict)
            else []
        )
        if not isinstance(page_rows, list):
            raise ValueError("provider result page is invalid")
        rows.extend(page_rows)
    raw = [
        store_raw(raw_dir, status_response),
        *[store_raw(raw_dir, page) for page in result_pages],
    ]
    bundle = {
        "schema_version": 1,
        "adapter": f"{provider}-batch",
        "authority": authority,
        "approved_origin": approved,
        "batch_id": marker["batch_id"],
        "provider_batch_id": provider_id,
        "status": status,
        "job_ids": sorted(x["custom_id"] for x in marker["jobs"]),
        "results": rows,
        "raw": raw,
    }
    write_json(Path(a.bundle), bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
