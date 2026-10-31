from __future__ import annotations

import pytest

from factlane.contract import AdapterError, validate_scope
from factlane.owner_contract import (
    MAX_OWNER_PAGE_SIZE,
    MAX_STATUS_EVENT_ROWS,
    MAX_STATUS_RESPONSE_BYTES,
    MAX_STATUS_SCOPE_ROWS,
    decode_page_cursor,
    encode_page_cursor,
    validate_owner_page_size,
    validate_owner_reason,
)


def test_owner_page_size_is_bounded_at_100() -> None:
    assert validate_owner_page_size(1) == 1
    assert validate_owner_page_size(MAX_OWNER_PAGE_SIZE) == 100
    for invalid in (0, 101, True, "10"):
        with pytest.raises(AdapterError) as exc_info:
            validate_owner_page_size(invalid)  # type: ignore[arg-type]
        assert exc_info.value.code == "INVALID_ENVELOPE"


def test_owner_retention_budgets_are_frozen() -> None:
    assert MAX_STATUS_SCOPE_ROWS == 100_000
    assert MAX_STATUS_EVENT_ROWS == 200_000
    assert MAX_STATUS_RESPONSE_BYTES == 16_384


def test_owner_page_cursor_is_bound_to_exact_scope_and_filters() -> None:
    scope = validate_scope("PROJECT", project_id="project-a")
    filters = {"lifecycle_state": "CANDIDATE", "memory_type": None, "contradiction_state": None}
    cursor = encode_page_cursor(
        scope,
        filters,
        created_at="2026-10-31T20:00:00Z",
        record_id="11111111-1111-4111-8111-111111111111",
    )
    assert decode_page_cursor(cursor, scope, filters) == (
        "2026-10-31T20:00:00Z",
        "11111111-1111-4111-8111-111111111111",
    )

    other_scope = validate_scope("PROJECT", project_id="project-b")
    with pytest.raises(AdapterError) as scope_error:
        decode_page_cursor(cursor, other_scope, filters)
    assert scope_error.value.code == "INVALID_CURSOR"

    changed_filters = dict(filters, lifecycle_state="VALIDATED_CURRENT")
    with pytest.raises(AdapterError) as filter_error:
        decode_page_cursor(cursor, scope, changed_filters)
    assert filter_error.value.code == "INVALID_CURSOR"


def test_owner_reason_codes_are_action_specific() -> None:
    assert validate_owner_reason("REVOKE", "OUTDATED") == "OUTDATED"
    assert validate_owner_reason("REJECT", "INSUFFICIENT_EVIDENCE") == "INSUFFICIENT_EVIDENCE"
    assert validate_owner_reason("RESOLVE", "STRONGER_EVIDENCE") == "STRONGER_EVIDENCE"

    with pytest.raises(AdapterError) as exc_info:
        validate_owner_reason("RESOLVE", "OUTDATED")
    assert exc_info.value.code == "INVALID_ENUM"
