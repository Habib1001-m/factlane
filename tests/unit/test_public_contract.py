from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter
from factlane.contract import (
    INTENT_CLASSES,
    PUBLIC_TOOL_NAMES,
    RETRIEVAL_MODE_KINDS,
    RETRIEVAL_MODES,
    UPDATE_MODES,
    AdapterError,
    validate_freshness,
    validate_provenance,
    validate_scope,
)
from factlane.gateway import HostBinding, MemoryGateway
from factlane.public_contract import TOOL_REQUEST_TYPES, render_tool_help
from factlane.router import TruthRouter
from factlane.server import build_mcp_server


TOOL_NAMES = {
    "memory_search",
    "memory_get",
    "memory_store",
    "memory_update",
    "memory_status",
}


def _server() -> Any:
    return build_mcp_server(
        MemoryGateway(
            object(),
            HostBinding("contract-test", "stdio", "unit-test"),
            transport_kind="stdio",
        )
    )


def _request_schema(tool_name: str) -> dict[str, Any]:
    parameters = _server()._tool_manager._tools[tool_name].parameters  # type: ignore[attr-defined]
    request = parameters["properties"]["request"]
    return parameters["$defs"][request["$ref"].rsplit("/", 1)[-1]]


def test_mcp_contract_exposes_exactly_five_typed_request_envelopes() -> None:
    server = _server()
    assert set(server._tool_manager._tools) == TOOL_NAMES  # type: ignore[attr-defined]

    search = _request_schema("memory_search")
    assert {"query", "intent_class", "scope"}.issubset(search["properties"])
    assert "intent_class" in search["required"]
    assert "scope" in search["required"]


def test_mcp_contract_exposes_finite_choices_and_defaults() -> None:
    search = _request_schema("memory_search")
    assert set(search["properties"]["intent_class"]["enum"]) == {
        "CURRENT_PROJECT_STATE",
        "PROJECT_DESIGN_RATIONALE",
        "USER_PREFERENCE_OR_DURABLE_FACT",
        "WORKFLOW_RULE",
        "TOOL_ENVIRONMENT_STATE",
        "HISTORICAL_QUESTION",
        "GENERAL_TASK_NO_MEMORY_REQUIRED",
    }
    assert set(search["properties"]["retrieval_mode_kind"]["enum"]) == {
        "EXACT",
        "KEYWORD",
        "SEMANTIC",
        "HYBRID",
    }
    assert search["properties"]["retrieval_mode"]["default"] == "CURRENT"
    assert search["properties"]["retrieval_mode_kind"]["default"] == "SEMANTIC"


def test_store_and_update_contracts_explain_governed_write_fields() -> None:
    store = _request_schema("memory_store")
    update = _request_schema("memory_update")
    assert "source_provenance" in store["properties"]
    assert "source_provenance" in store["required"]
    assert "freshness_policy" in store["properties"]
    assert "idempotency_key" in store["required"]
    assert "PROJECT_CURRENT" in store["properties"]["authority_role"]["description"]
    assert "WORKFLOW_CURRENT" in store["properties"]["authority_role"]["description"]
    assert "expected_revision" in update["required"]
    assert set(update["properties"]["mode"]["enum"]) == {"REVERIFY", "REPLACE"}
    assert "required for replace" in update["properties"]["replacement"]["description"].casefold()


def test_runtime_constants_cli_help_and_mcp_schema_stay_in_parity() -> None:
    assert tuple(TOOL_REQUEST_TYPES) == PUBLIC_TOOL_NAMES
    search = _request_schema("memory_search")
    assert search["properties"]["intent_class"]["enum"] == sorted(INTENT_CLASSES)
    assert search["properties"]["retrieval_mode"]["enum"] == sorted(RETRIEVAL_MODES)
    assert search["properties"]["retrieval_mode_kind"]["enum"] == sorted(RETRIEVAL_MODE_KINDS)
    assert _request_schema("memory_update")["properties"]["mode"]["enum"] == sorted(UPDATE_MODES)

    help_text = render_tool_help()
    for value in (*PUBLIC_TOOL_NAMES, *INTENT_CLASSES, *RETRIEVAL_MODES, *RETRIEVAL_MODE_KINDS, *UPDATE_MODES):
        assert value in help_text


def test_invalid_intent_error_lists_safe_supported_choices() -> None:
    with pytest.raises(AdapterError) as error:
        TruthRouter().decide(
            intent_class="workflow",
            operation="memory_search",
            scope=None,
        )

    assert "WORKFLOW_RULE" in error.value.safe_message
    assert "supported" in error.value.safe_message.casefold()


def test_scope_error_names_exact_identity_requirements() -> None:
    with pytest.raises(AdapterError) as error:
        validate_scope("WORKFLOW", project_id="project-alpha")

    assert "project_id" in error.value.safe_message
    assert "workflow_id" in error.value.safe_message


def test_retrieval_and_update_errors_list_safe_choices() -> None:
    adapter = MemoryAdapter(object(), object())  # validation stops before engine/provider use
    with pytest.raises(AdapterError) as retrieval_error:
        asyncio.run(
            adapter.search(
                query="x",
                intent_class="CURRENT_PROJECT_STATE",
                scope="PROJECT",
                project_id="p",
                retrieval_mode_kind="invalid",
            )
        )
    with pytest.raises(AdapterError) as update_error:
        asyncio.run(
            adapter.update(
                memory_id="00000000-0000-0000-0000-000000000000",
                scope="PROJECT",
                project_id="p",
                expected_revision=1,
                mode="invalid",
                idempotency_key="update-key",
            )
        )

    assert "HYBRID" in retrieval_error.value.safe_message
    assert "REVERIFY" in update_error.value.safe_message


def test_write_validation_errors_explain_required_governance_fields() -> None:
    with pytest.raises(AdapterError) as provenance_error:
        validate_provenance({})
    with pytest.raises(AdapterError) as freshness_error:
        validate_freshness({"kind": "invalid"})

    assert "source_provenance" in provenance_error.value.safe_message
    assert "source_class" in provenance_error.value.safe_message
    assert "manual" in freshness_error.value.safe_message


def test_scope_authority_error_names_the_derived_role() -> None:
    adapter = MemoryAdapter(object(), object())
    with pytest.raises(AdapterError) as error:
        asyncio.run(
            adapter.store(
                fact="bounded fact",
                scope="PROJECT",
                project_id="p",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance={
                    "source_class": "TEST",
                    "source_ref": "test",
                    "source_hash": "a" * 64,
                    "review_ref": "test",
                    "extraction_method": "test",
                },
                freshness_policy={"kind": "manual"},
                idempotency_key="authority-role-test",
                requested_lifecycle_state="VALIDATED_CURRENT",
                source_timestamp="2026-01-01T00:00:00Z",
                last_verified_at="2026-01-01T00:00:00Z",
                verified_by="OWNER",
                authority_role="OWNER_CURRENT",
            )
        )

    assert "PROJECT_CURRENT" in error.value.safe_message


def test_replace_error_explains_conditional_replacement_requirements() -> None:
    class ExistingRecordEngine:
        async def find_idempotency(self, key: str) -> None:
            return None

        async def get_record(self, memory_id: str, scope: object, history: bool = False) -> list[dict[str, Any]]:
            return [
                {
                    "revision": 1,
                    "record_id": "old-record",
                    "source_provenance": json.dumps({"source_class": "TEST", "source_ref": "test", "source_hash": "a" * 64, "review_ref": "test", "extraction_method": "test"}),
                    "freshness_policy": json.dumps({"kind": "manual", "ttl_seconds": None, "recheck_ref": None, "source_fingerprint": None}),
                    "source_timestamp": "2026-01-01T00:00:00Z",
                    "fact": "old fact",
                    "contradiction_key": "contradiction",
                    "memory_type": "PROJECT_LEARNED_FACT",
                    "confidence": 1.0,
                    "tags": "[]",
                }
            ]

    adapter = MemoryAdapter(ExistingRecordEngine(), object())
    with pytest.raises(AdapterError) as error:
        asyncio.run(
            adapter.update(
                memory_id="00000000-0000-0000-0000-000000000000",
                scope="PROJECT",
                project_id="p",
                expected_revision=1,
                mode="REPLACE",
                idempotency_key="replace-requirements-test",
                replacement={"fact": "new fact"},
            )
        )

    assert "replacement.source_provenance" in error.value.safe_message
    assert "replacement.freshness_policy" in error.value.safe_message


def test_wrong_store_provenance_field_returns_corrective_adapter_error() -> None:
    class StoreAdapter:
        async def store(self, *, source_provenance: dict[str, Any], **request: Any) -> dict[str, Any]:
            return {"status": "OK", "results": [], "audit": {}}

    gateway = MemoryGateway(
        StoreAdapter(),
        HostBinding("contract-test", "stdio", "unit-test"),
        transport_kind="stdio",
    )
    with pytest.raises(AdapterError) as error:
        asyncio.run(
            gateway.dispatch(
                "memory_store",
                {"fact": "x", "provenance": {"source_hash": "a" * 64}},
            )
        )

    assert "source_provenance" in error.value.safe_message


def test_cli_tool_help_is_available_without_starting_mcp() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from factlane.server import main; main(['--help-tools'])"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert set(name for name in TOOL_NAMES if name in result.stdout) == TOOL_NAMES
    assert "source_provenance" in result.stdout
    assert "source_hash" in result.stdout
    assert "ttl_seconds" in result.stdout
    assert "expected_revision" in result.stdout
    assert "supporting evidence" in result.stdout.casefold()


def test_public_skill_is_compact_and_self_describing() -> None:
    path = Path("skills/using-factlane/SKILL.md")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: using-factlane" in text
    description = next(line for line in text.splitlines() if line.startswith("description:"))
    assert description.removeprefix("description: ").strip().startswith("Use when")
    assert len(text.split()) < 500
    normalized = " ".join(text.split())
    for marker in (
        "source_provenance",
        "REVIEW_HISTORY",
        "expected_revision",
        "supporting evidence",
        "never guess",
    ):
        assert marker.casefold() in normalized.casefold()
