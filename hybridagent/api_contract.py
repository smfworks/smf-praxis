"""Stable, dependency-free contracts for the Praxis versioned HTTP API."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Sequence
from typing import Any, TypeVar

API_VERSION = "v1"
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
MAX_IDEMPOTENCY_KEY_LENGTH = 200
MAX_IDEMPOTENCY_RECEIPTS = 4096
MAX_JSON_BODY_BYTES = 64 * 1024
_T = TypeVar("_T")


def success_envelope(data: Any, *, request_id: str,
                     meta: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"request_id": request_id}
    metadata.update(meta or {})
    return {"api_version": API_VERSION, "data": data, "meta": metadata}


def error_envelope(code: str, message: str, *, request_id: str,
                   details: dict[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"api_version": API_VERSION, "error": error,
            "meta": {"request_id": request_id}}


def encode_cursor(offset: int, *, secret: bytes = b"",
                  snapshot: str = "") -> str:
    if offset < 0:
        raise ValueError("offset must be non-negative")
    body = {"v": 1, "offset": offset, "snapshot": snapshot}
    unsigned = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    body["signature"] = hmac.new(secret, unsigned, hashlib.sha256).hexdigest()
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str | None, *, secret: bytes = b"",
                  snapshot: str = "") -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        offset = payload["offset"]
        signature = payload.pop("signature")
        unsigned = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(secret, unsigned, hashlib.sha256).hexdigest()
        if (
            payload.get("v") != 1
            or payload.get("snapshot") != snapshot
            or not isinstance(offset, int)
            or offset < 0
            or not hmac.compare_digest(str(signature), expected)
        ):
            raise ValueError
        return offset
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError,
            UnicodeDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


def normalize_limit(value: str | int | None) -> int:
    try:
        limit = int(value) if value is not None else DEFAULT_PAGE_LIMIT
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if limit < 1 or limit > MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")
    return limit


def page_items(items: Sequence[_T], *, limit: int, cursor: str | None,
               secret: bytes = b"", snapshot: str = "") -> tuple[list[_T], str | None]:
    offset = decode_cursor(cursor, secret=secret, snapshot=snapshot)
    page = list(items[offset:offset + limit])
    next_offset = offset + len(page)
    next_cursor = (encode_cursor(next_offset, secret=secret, snapshot=snapshot)
                   if next_offset < len(items) else None)
    return page, next_cursor


def resource_version(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"),
                           default=str).encode()
    return hashlib.sha256(canonical).hexdigest()


def etag(version: str) -> str:
    return f'"{version}"'
