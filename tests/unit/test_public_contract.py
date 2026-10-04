from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter, trusted_write_context_for_profile
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
from factlane.public_contract import TOOL_REQUEST_TYPES, render_tool_help, request_schema as public_request_schema
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


def _structured_tool_result(server: Any, tool_name: str, request: dict[str, Any]) -> dict[str, Any]:
    content, structured = asyncio.run(server.call_tool(tool_name, {"request": request}))
    assert isinstance(structured, dict)
    assert len(content) == 1
    assert json.loads(content[0].text) == structured
    return structured


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


def test_public_search_schema_excludes_self_suppressing_routing_hints() -> None:
    search = _request_schema("memory_search")
    assert not {"include_graph_links", "direct_truth_available", "user_supplied"}.intersection(search["properties"])


def test_public_store_schema_does_not_ask_agents_to_choose_derived_authority() -> None:
    store = _request_schema("memory_store")
    assert "authority_role" not in store["properties"]


def test_public_mcp_preserves_unknown_fields_for_policy_rejection() -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
            assert operation == "memory_search"
            self.calls.append(request.copy())
            raise AdapterError("INVALID_ENVELOPE", "unknown field reached raw policy")

    adapter = RecordingAdapter()
    server = build_mcp_server(
        MemoryGateway(
            adapter,
            HostBinding("contract-test", "stdio", "unit-test"),
            transport_kind="stdio",
        )
    )
    result = _structured_tool_result(
        server,
        "memory_search",
        {
            "query": "lifecycle policy",
            "intent_class": "WORKFLOW_RULE",
            "scope": "PROJECT",
            "project_id": "factlane",
            "unknown_extra": "must-not-be-stripped",
        },
    )
    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "INVALID_ENVELOPE"
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["unknown_extra"] == "must-not-be-stripped"


class _PublicBoundaryAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, request.copy()))
        return {"status": "OK", "results": [], "audit": {}}


@pytest.mark.parametrize("claim", ["authority_role", "trusted_write_context", "write_capability"])
def test_public_mcp_reserved_claims_are_rejected_before_adapter(claim: str) -> None:
    adapter = _PublicBoundaryAdapter()
    server = build_mcp_server(
        MemoryGateway(
            adapter,
            HostBinding("contract-test", "stdio", "unit-test"),
            transport_kind="stdio",
        )
    )
    result = _structured_tool_result(
        server,
        "memory_status",
        {"scope": "PROJECT", "project_id": "factlane", claim: "spoofed"},
    )
    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "WRITE_AUTHORIZATION_DENIED"
    assert adapter.calls == []


def test_public_mcp_preserves_explicit_null_identity_for_cross_scope_policy_error() -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
            assert operation == "memory_status"
            self.calls.append(request.copy())
            raise AdapterError("CROSS_SCOPE_DENIED", "explicit null identity key is still present")

    adapter = RecordingAdapter()
    server = build_mcp_server(
        MemoryGateway(
            adapter,
            HostBinding("contract-test", "stdio", "unit-test"),
            transport_kind="stdio",
        )
    )
    result = _structured_tool_result(
        server,
        "memory_status",
        {"scope": "CROSS_PROJECT_WORKFLOW", "project_id": None},
    )
    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "CROSS_SCOPE_DENIED"
    assert result["message"] == "explicit null identity key is still present"
    assert adapter.calls == [{"scope": "CROSS_PROJECT_WORKFLOW", "project_id": None}]


def test_public_request_schemas_are_closed_even_when_runtime_preserves_raw_extras() -> None:
    for name in TOOL_NAMES:
        schema = _request_schema(name)
        assert schema.get("additionalProperties") is False
        assert schema.get("x-factlane-public-contract-revision") == 2


def test_help_documents_cross_project_search_and_freshness_matrix() -> None:
    help_text = render_tool_help()
    assert "CROSS_PROJECT_WORKFLOW search matrix" in help_text
    assert "GENERAL_TASK_NO_MEMORY_REQUIRED -> NO_MEMORY_NEEDED after scope-shape validation" in help_text
    assert "on_change requires non-empty recheck_ref and source_fingerprint" in help_text
    assert "source_fingerprint must equal source_provenance.source_hash" in help_text
    assert "status=BLOCKED" in help_text
    assert "stable error_code" in help_text
    assert "Unexpected internal exceptions remain transport errors" in help_text


def test_store_and_update_contracts_explain_governed_write_fields() -> None:
    store = _request_schema("memory_store")
    update = _request_schema("memory_update")
    assert "source_provenance" in store["properties"]
    assert "source_provenance" in store["required"]
    assert "freshness_policy" in store["properties"]
    assert "idempotency_key" in store["required"]
    assert "authority_role" not in store["properties"]
    assert "expected_revision" in update["required"]
    assert set(update["properties"]["mode"]["enum"]) == {"REVERIFY", "REPLACE"}
    assert "required for replace" in update["properties"]["replacement"]["description"].casefold()
    raw_update = public_request_schema("memory_update")
    verification = raw_update["$defs"]["VerificationPayload"]["properties"]
    replacement = raw_update["$defs"]["ReplacementPayload"]["properties"]
    assert "cannot change the existing memory type" in verification["memory_type"]["description"]
    assert "cannot change the existing subject" in verification["subject"]["description"]
    assert "cannot change" not in replacement["memory_type"]["description"].casefold()
    assert "for the replacement" in replacement["subject"]["description"].casefold()


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


def test_gateway_operation_surface_uses_the_canonical_tool_names() -> None:
    assert tuple(MemoryGateway.tool_names()) == PUBLIC_TOOL_NAMES


def test_public_contract_runtime_dependencies_are_declared() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(project["project"]["dependencies"])
    assert "pydantic==2.13.5" in dependencies
    assert "typing-extensions==4.16.0" in dependencies


def test_normal_cli_help_states_server_arguments_are_required() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from factlane.server import main; main(['--help'])"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "requires --db and --host-id" in result.stdout
    assert "--help-tools" in result.stdout


def test_public_docs_discover_offline_help_and_optional_skill() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    quickstart = Path("docs/QUICKSTART.md").read_text(encoding="utf-8")
    assert "--help-tools" in readme
    assert "--help-tools" in quickstart
    assert "using-factlane" in readme
    assert "using-factlane" in quickstart


def test_public_docs_declare_sqlite_runtime_floor() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    quickstart = Path("docs/QUICKSTART.md").read_text(encoding="utf-8")
    environment = Path("docs/ENVIRONMENT.md").read_text(encoding="utf-8")
    provenance = Path("environment-provenance.json").read_text(encoding="utf-8")
    for text in (readme, quickstart, environment, provenance):
        assert "3.42.0" in text


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
    adapter = MemoryAdapter(
        object(), object(), trusted_write_context=trusted_write_context_for_profile("owner-current")
    )  # validation stops before engine/provider use
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


def test_public_scope_authority_claim_is_denied_before_role_derivation() -> None:
    adapter = MemoryAdapter(
        object(), object(), trusted_write_context=trusted_write_context_for_profile("owner-current")
    )
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

    assert error.value.code == "WRITE_AUTHORIZATION_DENIED"
    assert "trusted or derived authority" in error.value.safe_message


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

    adapter = MemoryAdapter(
        ExistingRecordEngine(), object(), trusted_write_context=trusted_write_context_for_profile("owner-current")
    )
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
