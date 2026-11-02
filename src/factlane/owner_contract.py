from __future__ import annotations

import base64
import json
from typing import Any

from .contract import AdapterError, ScopeContext, canonical_json, digest

MAX_OWNER_PAGE_SIZE = 100
MAX_STATUS_SCOPE_ROWS = 100_000
MAX_STATUS_EVENT_ROWS = 200_000
MAX_STATUS_RESPONSE_BYTES = 16 * 1024
_MAX_CURSOR_BYTES = 1024

OWNER_REASON_CODES: dict[str, frozenset[str]] = {
    "REVOKE": frozenset({"INCORRECT", "OUTDATED", "WITHDRAWN", "POLICY_REVOKED"}),
    "REJECT": frozenset(
        {"INCORRECT", "DUPLICATE", "CONTRADICTED", "INSUFFICIENT_EVIDENCE", "POLICY_REJECTED"}
    ),
    "RESOLVE": frozenset(
        {"OWNER_ADJUDICATION", "STRONGER_EVIDENCE", "SOURCE_PRECEDENCE", "NEWER_VERIFIED_TRUTH"}
    ),
}


def validate_owner_page_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_OWNER_PAGE_SIZE:
        raise AdapterError(
            "INVALID_ENVELOPE",
            f"Owner read page size must be an integer from 1 to {MAX_OWNER_PAGE_SIZE}",
        )
    return value


def validate_owner_reason(action: str, reason_code: str) -> str:
    allowed = OWNER_REASON_CODES.get(action)
    if allowed is None:
        raise AdapterError("INVALID_ENUM", "Owner lifecycle action is not supported")
    if reason_code not in allowed:
        raise AdapterError(
            "INVALID_ENUM",
            f"{action} reason_code is invalid; choose one of: {', '.join(sorted(allowed))}",
        )
    return reason_code


def validate_group_token(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in value)
    ):
        raise AdapterError("INVALID_ENVELOPE", "observed_group_token must be exact 64-hex")
    return value.lower()


def _cursor_binding(scope: ScopeContext, filters: dict[str, Any]) -> str:
    return digest({"scope": scope.to_dict(), "filters": filters})


def encode_page_cursor(
    scope: ScopeContext,
    filters: dict[str, Any],
    *,
    created_at: str,
    record_id: str,
) -> str:
    payload = {
        "v": 1,
        "binding": _cursor_binding(scope, filters),
        "created_at": created_at,
        "record_id": record_id,
    }
    encoded = base64.urlsafe_b64encode(canonical_json(payload).encode("utf-8")).decode("ascii").rstrip("=")
    if len(encoded.encode("ascii")) > _MAX_CURSOR_BYTES:
        raise AdapterError("INVALID_CURSOR", "Owner pagination cursor is oversized")
    return encoded


def decode_page_cursor(
    cursor: str | None,
    scope: ScopeContext,
    filters: dict[str, Any],
) -> tuple[str, str] | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor or len(cursor.encode("utf-8")) > _MAX_CURSOR_BYTES:
        raise AdapterError("INVALID_CURSOR", "Owner pagination cursor is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("INVALID_CURSOR", "Owner pagination cursor is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"v", "binding", "created_at", "record_id"}:
        raise AdapterError("INVALID_CURSOR", "Owner pagination cursor is invalid")
    if payload.get("v") != 1 or payload.get("binding") != _cursor_binding(scope, filters):
        raise AdapterError("INVALID_CURSOR", "Owner pagination cursor does not match exact scope and filters")
    created_at = payload.get("created_at")
    record_id = payload.get("record_id")
    if not isinstance(created_at, str) or not created_at or not isinstance(record_id, str) or not record_id:
        raise AdapterError("INVALID_CURSOR", "Owner pagination cursor is invalid")
    return created_at, record_id
