#!/usr/bin/env python3
"""Own authenticated provider batch submission and terminal result acquisition."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


TERMINAL = {"ended", "completed", "failed", "cancelled", "expired"}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def origin(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("provider base URL must be an HTTPS URL without credentials")
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{parsed.hostname.lower()}{port}"


class NoCredentialRedirect(HTTPRedirectHandler):
    """Reject redirects before urllib can copy credential headers to a new request."""

    def __init__(self, approved_origin: str):
        super().__init__()
        self.approved_origin = approved_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        destination = origin(newurl)
        if destination != self.approved_origin:
            raise ValueError("provider redirect escaped approved origin")
        raise ValueError(
            "provider redirects are rejected to preserve request authority"
        )


def authenticated_request(
    provider: str,
    base: str,
    path: str,
    key: str,
    *,
    data: bytes | None = None,
    content_type: str | None = None,
    method: str | None = None,
) -> Request:
    approved = origin(base)
    url = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    if origin(url) != approved:
        raise ValueError("provider request escaped approved origin")
    headers = {"Accept": "application/json"}
    if provider == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif provider == "openai":
        headers["Authorization"] = f"Bearer {key}"
    else:
        raise ValueError(f"unsupported batch provider: {provider}")
    if content_type:
        headers["Content-Type"] = content_type
    return Request(url, data=data, headers=headers, method=method)


class NetworkTransport:
    def __init__(self, provider: str):
        self.provider = provider
        env_prefix = provider.upper()
        self.base = os.environ.get(
            f"{env_prefix}_BASE_URL",
            "https://api.anthropic.com/v1"
            if provider == "anthropic"
            else "https://api.openai.com/v1",
        )
        self.approved_origin = origin(self.base)
        self.key = os.environ.get(f"{env_prefix}_API_KEY", "")
        if not self.key:
            raise ValueError(f"{provider} API key is required")
        self.opener = build_opener(NoCredentialRedirect(self.approved_origin))

    def request(
        self,
        path: str,
        *,
        payload: Any | None = None,
        raw: bytes | None = None,
        content_type: str | None = None,
        method: str | None = None,
    ) -> Any:
        data = raw
        if payload is not None:
            data = canonical_bytes(payload)
            content_type = "application/json"
        request = authenticated_request(
            self.provider,
            self.base,
            path,
            self.key,
            data=data,
            content_type=content_type,
            method=method or ("POST" if data is not None else "GET"),
        )
        with self.opener.open(request, timeout=30) as response:
            body = response.read()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return [json.loads(line) for line in body.splitlines() if line.strip()]


def store_raw(raw_dir: Path, value: Any) -> dict[str, str]:
    digest = canonical_sha256(value)
    path = raw_dir / f"sha256-{digest}.json"
    if path.exists():
        if canonical_sha256(json.loads(path.read_text(encoding="utf-8"))) != digest:
            raise ValueError("content-addressed provider page was modified")
    else:
        write_json(path, value)
    return {"path": str(path.resolve()), "sha256": digest}


def verify_request(marker: dict[str, Any]) -> Path:
    path = Path(str(marker.get("request_file") or "")).resolve()
    expected = str(marker.get("request_sha256") or "")
    if not path.is_file() or not expected or file_sha256(path) != expected:
        raise ValueError("prepared batch request digest does not match durable marker")
    return path


def assert_submission_retry_safe(marker: dict[str, Any]) -> None:
    status = str(marker.get("status") or "")
    if status in {"submitting", "upload_submitting", "submission_uncertain"}:
        raise ValueError("provider submission is uncertain and must not be retried")
    if status not in {"pending_submission", "pending_upload", "uploaded", "submitted"}:
        raise ValueError(f"batch cannot be submitted from state {status!r}")


def multipart_file(path: Path) -> tuple[bytes, str]:
    boundary = "orchestration-" + hashlib.sha256(path.read_bytes()).hexdigest()[:24]
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="batch.jsonl"\r\n'
            "Content-Type: application/jsonl\r\n\r\n"
        ).encode()
        + path.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _test_response(transport: dict[str, Any], name: str) -> Any:
    error = transport.get(f"{name}_error")
    if error:
        raise TimeoutError(str(error))
    if name not in transport:
        raise ValueError(f"test transport has no {name} response")
    return transport[name]


def submit(
    marker_path: Path, test_transport: dict[str, Any] | None = None
) -> dict[str, Any]:
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    request_path = verify_request(marker)
    if marker.get("provider_batch_id"):
        if marker.get("status") != "submitted":
            raise ValueError("provider batch id exists outside submitted state")
        return marker["acceptance_receipt"]
    assert_submission_retry_safe(marker)
    provider = str(marker["provider"])
    transport = None if test_transport is not None else NetworkTransport(provider)
    raw_dir = marker_path.parent / "batch-raw"
    try:
        if provider == "openai" and not marker.get("input_file_id"):
            marker["status"] = "upload_submitting"
            write_json(marker_path, marker)
            body, content_type = multipart_file(request_path)
            uploaded = (
                _test_response(test_transport, "upload")
                if test_transport is not None
                else transport.request(
                    "files", raw=body, content_type=content_type, method="POST"
                )
            )
            input_file_id = (
                str(uploaded.get("id") or "") if isinstance(uploaded, dict) else ""
            )
            if not input_file_id:
                raise ValueError("OpenAI upload response has no file id")
            upload_ref = store_raw(raw_dir, uploaded)
            marker.update(
                {
                    "status": "uploaded",
                    "input_file_id": input_file_id,
                    "upload_receipt": upload_ref,
                }
            )
            write_json(marker_path, marker)
        marker["status"] = "submitting"
        write_json(marker_path, marker)
        if provider == "anthropic":
            response = (
                _test_response(test_transport, "submit")
                if test_transport is not None
                else transport.request(
                    "messages/batches",
                    payload=json.loads(request_path.read_text()),
                    method="POST",
                )
            )
        else:
            payload = {
                "input_file_id": marker["input_file_id"],
                "endpoint": "/v1/responses",
                "completion_window": "24h",
            }
            response = (
                _test_response(test_transport, "submit")
                if test_transport is not None
                else transport.request("batches", payload=payload, method="POST")
            )
    except (TimeoutError, socket.timeout, URLError) as exc:
        marker["status"] = "submission_uncertain"
        write_json(marker_path, marker)
        raise ValueError(
            "provider submission outcome is uncertain; reservations remain fenced"
        ) from exc
    provider_id = str(response.get("id") or "") if isinstance(response, dict) else ""
    if not provider_id:
        marker["status"] = "submission_uncertain"
        write_json(marker_path, marker)
        raise ValueError(
            "provider submission returned no batch id; reservations remain fenced"
        )
    raw_ref = store_raw(raw_dir, response)
    receipt = {
        "provider": provider,
        "provider_batch_id": provider_id,
        "request_sha256": marker["request_sha256"],
        "raw": raw_ref,
    }
    receipt["sha256"] = canonical_sha256(receipt)
    marker.update(
        {
            "status": "submitted",
            "provider_batch_id": provider_id,
            "acceptance_receipt": receipt,
        }
    )
    write_json(marker_path, marker)
    return receipt


def _rows(page: Any) -> list[dict[str, Any]]:
    rows = (
        page
        if isinstance(page, list)
        else page.get("data")
        if isinstance(page, dict)
        else None
    )
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("provider result page is invalid")
    return rows


def normalize_results(
    provider: str, rows: list[dict[str, Any]], expected: set[str]
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    normalized = []
    for row in rows:
        custom_id = str(row.get("custom_id") or "")
        if custom_id in seen:
            raise ValueError(f"duplicate provider result custom_id: {custom_id}")
        if custom_id not in expected:
            raise ValueError(f"unknown provider result custom_id: {custom_id}")
        seen.add(custom_id)
        if provider == "anthropic":
            result = row.get("result")
            if not isinstance(result, dict):
                raise ValueError(f"result for {custom_id} has no terminal envelope")
            result_type = str(result.get("type") or "")
            if result_type == "succeeded":
                message = result.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
                response_id = (
                    str(message.get("id") or "") if isinstance(message, dict) else ""
                )
                if not response_id or not isinstance(usage, dict):
                    raise ValueError(
                        f"successful result for {custom_id} lacks response id or usage"
                    )
                normalized_usage = {
                    "input_tokens": int(usage.get("input_tokens", 0)),
                    "cache_write_tokens": int(
                        usage.get("cache_creation_input_tokens", 0)
                    ),
                    "cache_read_tokens": int(usage.get("cache_read_input_tokens", 0)),
                    "output_tokens": int(usage.get("output_tokens", 0)),
                    "reasoning_tokens": 0,
                }
                item = {
                    "custom_id": custom_id,
                    "outcome": "completed",
                    "response_id": response_id,
                    "usage": normalized_usage,
                }
            elif result_type in {"errored", "canceled", "expired"}:
                item = {
                    "custom_id": custom_id,
                    "outcome": "failed",
                    "error": result.get("error") or {"type": result_type},
                }
            else:
                raise ValueError(f"result for {custom_id} is not terminal")
        elif provider == "openai":
            response = row.get("response")
            error = row.get("error")
            if isinstance(response, dict) and response.get("status_code") == 200:
                body = response.get("body")
                usage = body.get("usage") if isinstance(body, dict) else None
                response_id = (
                    str(body.get("id") or "") if isinstance(body, dict) else ""
                )
                if not response_id or not isinstance(usage, dict):
                    raise ValueError(
                        f"successful result for {custom_id} lacks nested response id or usage"
                    )
                input_details = usage.get("input_tokens_details") or {}
                output_details = usage.get("output_tokens_details") or {}
                item = {
                    "custom_id": custom_id,
                    "outcome": "completed",
                    "response_id": response_id,
                    "usage": {
                        "input_tokens": int(usage.get("input_tokens", 0)),
                        "cache_write_tokens": 0,
                        "cache_read_tokens": int(input_details.get("cached_tokens", 0)),
                        "output_tokens": int(usage.get("output_tokens", 0)),
                        "reasoning_tokens": int(
                            output_details.get("reasoning_tokens", 0)
                        ),
                    },
                }
            elif error is not None or isinstance(response, dict):
                item = {
                    "custom_id": custom_id,
                    "outcome": "failed",
                    "error": error or response,
                }
            else:
                raise ValueError(f"result for {custom_id} is not terminal")
        else:
            raise ValueError(f"unsupported batch provider: {provider}")
        normalized.append(item)
    missing = expected - seen
    if missing:
        raise ValueError(
            "missing provider results for custom_ids: " + ", ".join(sorted(missing))
        )
    return sorted(normalized, key=lambda item: item["custom_id"])


def acquire_terminal_bundle(
    marker: dict[str, Any], test_transport: dict[str, Any] | None, output_dir: Path
) -> dict[str, Any]:
    verify_request(marker)
    provider = str(marker["provider"])
    provider_id = str(marker.get("provider_batch_id") or "")
    receipt = marker.get("acceptance_receipt")
    if not provider_id or not isinstance(receipt, dict):
        raise ValueError("batch has no adapter-owned acceptance receipt")
    if receipt.get("provider_batch_id") != provider_id or receipt.get(
        "request_sha256"
    ) != marker.get("request_sha256"):
        raise ValueError(
            "acceptance receipt does not bind provider id and request digest"
        )
    receipt_copy = dict(receipt)
    receipt_digest = str(receipt_copy.pop("sha256", ""))
    if not receipt_digest or canonical_sha256(receipt_copy) != receipt_digest:
        raise ValueError("acceptance receipt digest is invalid")
    acceptance_raw = receipt.get("raw")
    if not isinstance(acceptance_raw, dict):
        raise ValueError("acceptance receipt has no raw provider response")
    acceptance_path = Path(str(acceptance_raw.get("path") or "")).resolve()
    try:
        acceptance_value = json.loads(acceptance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("acceptance receipt raw response is unreadable") from exc
    acceptance_digest = canonical_sha256(acceptance_value)
    if (
        acceptance_raw.get("sha256") != acceptance_digest
        or acceptance_path.name != f"sha256-{acceptance_digest}.json"
        or str(acceptance_value.get("id") or "") != provider_id
    ):
        raise ValueError("acceptance receipt raw response is not content-addressed")
    transport = None if test_transport is not None else NetworkTransport(provider)
    if test_transport is not None:
        status_response = _test_response(test_transport, "status")
        result_pages = test_transport.get("result_pages", [])
    else:
        status_response = transport.request(
            f"messages/batches/{provider_id}"
            if provider == "anthropic"
            else f"batches/{provider_id}"
        )
        result_pages = []
    if (
        not isinstance(status_response, dict)
        or str(status_response.get("id") or "") != provider_id
    ):
        raise ValueError("provider status does not match accepted batch id")
    status = str(
        status_response.get("processing_status") or status_response.get("status") or ""
    )
    if status not in TERMINAL:
        raise ValueError("provider batch is not terminal; uncertainty remains reserved")
    if test_transport is None and status in {"ended", "completed"}:
        if provider == "anthropic":
            result_pages = [
                transport.request(f"messages/batches/{provider_id}/results")
            ]
        else:
            for field in ("output_file_id", "error_file_id"):
                file_id = str(status_response.get(field) or "")
                if file_id:
                    result_pages.append(transport.request(f"files/{file_id}/content"))
    raw_dir = output_dir / "batch-raw"
    raw_pages = [store_raw(raw_dir, status_response)] + [
        store_raw(raw_dir, page) for page in result_pages
    ]
    expected = {str(item["custom_id"]) for item in marker.get("jobs", [])}
    rows = [row for page in result_pages for row in _rows(page)]
    results = (
        normalize_results(provider, rows, expected)
        if status in {"ended", "completed"}
        else []
    )
    usage = {
        key: sum(int(item.get("usage", {}).get(key, 0)) for item in results)
        for key in (
            "input_tokens",
            "cache_write_tokens",
            "cache_read_tokens",
            "output_tokens",
            "reasoning_tokens",
        )
    }
    bundle = {
        "schema_version": 2,
        "adapter": f"{provider}-batch",
        "authority": "test-only" if test_transport is not None else "provider-network",
        "approved_origin": "test-only"
        if test_transport is not None
        else transport.approved_origin,
        "batch_id": marker["batch_id"],
        "provider_batch_id": provider_id,
        "request_sha256": marker["request_sha256"],
        "acceptance_receipt": receipt,
        "acceptance_receipt_sha256": receipt_digest,
        "status": status,
        "job_ids": sorted(expected),
        "raw_pages": raw_pages,
        "results": results,
        "results_sha256": canonical_sha256(results),
        "usage": usage,
    }
    return bundle


def persist_bundle(bundle: dict[str, Any], output_dir: Path) -> dict[str, str]:
    digest = canonical_sha256(bundle)
    path = output_dir / f"batch-{bundle['batch_id']}.terminal.sha256-{digest}.json"
    if path.exists():
        if canonical_sha256(json.loads(path.read_text(encoding="utf-8"))) != digest:
            raise ValueError("immutable terminal bundle was modified")
    else:
        write_json(path, bundle)
    return {"path": str(path.resolve()), "sha256": digest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("submit", "fetch"))
    parser.add_argument("--marker", required=True)
    parser.add_argument("--test-transport", help=argparse.SUPPRESS)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    marker_path = Path(args.marker).resolve()
    test_transport = (
        json.loads(Path(args.test_transport).read_text())
        if args.test_transport
        else None
    )
    if args.action == "submit":
        print(json.dumps(submit(marker_path, test_transport), sort_keys=True))
        return 0
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    output_dir = (
        Path(args.output_dir).resolve() if args.output_dir else marker_path.parent
    )
    bundle = acquire_terminal_bundle(marker, test_transport, output_dir)
    print(json.dumps(persist_bundle(bundle, output_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
