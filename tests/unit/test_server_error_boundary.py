from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from factlane.contract import PUBLIC_CONTRACT_REVISION, PUBLIC_TOOL_NAMES, AdapterError
from factlane.gateway import HostBinding, MemoryGateway, TrustedContextBinding
from factlane.server import build_mcp_server


TOOL_REQUESTS: dict[str, dict[str, Any]] = {
    "memory_search": {
        "query": "bounded query",
        "intent_class": "USER_PREFERENCE_OR_DURABLE_FACT",
        "scope": "GLOBAL_USER",
    },
    "memory_get": {
        "memory_id": "00000000-0000-0000-0000-000000000000",
        "scope": "GLOBAL_USER",
    },
    "memory_store": {
        "fact": "bounded fact",
        "memory_type": "USER_FACT",
        "scope": "GLOBAL_USER",
        "source_provenance": {
            "source_class": "TEST",
            "source_ref": "test",
            "source_hash": "a" * 64,
            "review_ref": "test-review",
            "extraction_method": "unit-test",
        },
        "freshness_policy": {"kind": "manual"},
        "idempotency_key": "error-boundary-store",
    },
    "memory_update": {
        "memory_id": "00000000-0000-0000-0000-000000000000",
        "scope": "GLOBAL_USER",
        "expected_revision": 1,
        "mode": "REVERIFY",
        "idempotency_key": "error-boundary-update",
    },
    "memory_status": {"scope": "GLOBAL_USER"},
}


class RaisingAdapter:
    def __init__(self, code: str, message: str = "safe governed message", **details: Any) -> None:
        self.code = code
        self.message = message
        self.details = details
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, request.copy()))
        raise AdapterError(self.code, self.message, **self.details)


class RuntimeFailureAdapter:
    async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("unexpected internal failure")


class SuccessAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, request.copy()))
        return {
            "status": "OK",
            "results": [],
            "degradation": None,
            "audit": {
                "request_id": request.get("request_id"),
                "raw_content_logged": False,
                "host_binding": {"host_id": "adapter-spoof"},
            },
        }


def _gateway(
    adapter: Any,
    *,
    host_id: str = "error-boundary-host",
    context_binding: TrustedContextBinding | None = None,
) -> MemoryGateway:
    return MemoryGateway(
        adapter,
        HostBinding(host_id, "stdio", "unit-test"),
        transport_kind="stdio",
        context_binding=context_binding,
    )


def _call(server: Any, tool_name: str, request: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    result = asyncio.run(server.call_tool(tool_name, {"request": deepcopy(request)}))
    assert isinstance(result, tuple)
    assert len(result) == 2
    content, structured = result
    assert isinstance(content, list)
    assert isinstance(structured, dict)
    return content, structured


def _assert_text_structured_parity(content: list[Any], structured: dict[str, Any]) -> None:
    assert len(content) == 1
    text = getattr(content[0], "text", None)
    assert isinstance(text, str)
    assert json.loads(text) == structured


def test_e01_governed_adapter_error_crosses_mcp_boundary_as_structured_result() -> None:
    adapter = RaisingAdapter("VERSION_CONFLICT", "re-read and retry")
    server = build_mcp_server(_gateway(adapter))

    content, structured = _call(server, "memory_status", TOOL_REQUESTS["memory_status"])

    assert structured["status"] == "BLOCKED"
    assert structured["error_code"] == "VERSION_CONFLICT"
    assert structured["message"] == "re-read and retry"
    assert structured["results"] == []
    assert structured["degradation"] is None
    _assert_text_structured_parity(content, structured)


def test_e02_governed_error_exposes_only_safe_envelope_content() -> None:
    adapter = RaisingAdapter(
        "VERSION_CONFLICT",
        "safe retry guidance",
        raw_sql="UNIQUE constraint failed: private_table.secret",
        private_path="/home/example/.private/control.db",
        raw_fact="do not expose this memory content",
    )
    server = build_mcp_server(_gateway(adapter))

    content, structured = _call(server, "memory_status", TOOL_REQUESTS["memory_status"])
    rendered = json.dumps(structured, sort_keys=True) + getattr(content[0], "text", "")

    assert "safe retry guidance" in rendered
    assert "UNIQUE constraint failed" not in rendered
    assert "/home/example/.private" not in rendered
    assert "do not expose this memory content" not in rendered
    assert "Traceback" not in rendered
    assert "AdapterError" not in rendered
    _assert_text_structured_parity(content, structured)


def test_e03_valid_request_id_is_preserved_and_unbounded_id_is_not_echoed() -> None:
    adapter = RaisingAdapter("VERSION_CONFLICT")
    server = build_mcp_server(_gateway(adapter))

    valid = deepcopy(TOOL_REQUESTS["memory_status"])
    valid["request_id"] = "req-structured-01"
    _, valid_result = _call(server, "memory_status", valid)
    assert valid_result["audit"]["request_id"] == "req-structured-01"

    invalid = deepcopy(TOOL_REQUESTS["memory_status"])
    invalid["request_id"] = "x" * 129
    _, invalid_result = _call(server, "memory_status", invalid)
    assert invalid_result["audit"]["request_id"] is None
    assert "x" * 129 not in json.dumps(invalid_result)


@pytest.mark.parametrize(
    ("code", "expected_retryable"),
    [
        ("BACKEND_BUSY", True),
        ("BACKEND_UNAVAILABLE", True),
        ("TIMEOUT", True),
        ("VERSION_CONFLICT", False),
        ("WRITE_AUTHORIZATION_DENIED", False),
    ],
)
def test_e04_retryability_mapping_is_preserved(code: str, expected_retryable: bool) -> None:
    server = build_mcp_server(_gateway(RaisingAdapter(code)))

    _, structured = _call(server, "memory_status", TOOL_REQUESTS["memory_status"])

    assert structured["audit"]["retryable"] is expected_retryable


def test_e05_trusted_host_binding_is_present_and_adapter_cannot_spoof_it_on_error() -> None:
    server = build_mcp_server(_gateway(RaisingAdapter("VERSION_CONFLICT"), host_id="trusted-host"))

    _, structured = _call(server, "memory_status", TOOL_REQUESTS["memory_status"])
    host_binding = structured["audit"]["host_binding"]

    assert host_binding["host_id"] == "trusted-host"
    assert host_binding["transport"] == "stdio"
    assert host_binding["binding_source"] == "unit-test"
    assert len(host_binding["gateway_instance_id"]) == 32


def test_e06_trusted_context_binding_is_preserved_on_error() -> None:
    context = TrustedContextBinding(
        project_id="factlane",
        agent_id="codex-session",
        project_id_source="git-origin",
        agent_id_source="host-config",
        binding_source="session-bootstrap",
    )
    adapter = RaisingAdapter("VERSION_CONFLICT")
    server = build_mcp_server(_gateway(adapter, context_binding=context))

    _, structured = _call(server, "memory_status", {"scope": "PROJECT"})

    assert adapter.calls == [("memory_status", {"scope": "PROJECT", "project_id": "factlane"})]
    assert structured["audit"]["context_binding"] == {
        "binding_source": "session-bootstrap",
        "project_id": "factlane",
        "project_id_source": "git-origin",
        "agent_id": "codex-session",
        "agent_id_source": "host-config",
    }


@pytest.mark.parametrize("tool_name", PUBLIC_TOOL_NAMES)
def test_e07_all_five_public_tools_share_the_same_structured_error_boundary(tool_name: str) -> None:
    adapter = RaisingAdapter("VERSION_CONFLICT")
    server = build_mcp_server(_gateway(adapter))

    content, structured = _call(server, tool_name, TOOL_REQUESTS[tool_name])

    assert structured["status"] == "BLOCKED"
    assert structured["error_code"] == "VERSION_CONFLICT"
    _assert_text_structured_parity(content, structured)
    assert [tool.name for tool in asyncio.run(server.list_tools())] == list(PUBLIC_TOOL_NAMES)
    assert PUBLIC_CONTRACT_REVISION == 2


def test_e08_success_path_shape_and_trusted_audit_remain_unchanged() -> None:
    adapter = SuccessAdapter()
    server = build_mcp_server(_gateway(adapter, host_id="trusted-host"))
    request = {"scope": "GLOBAL_USER", "request_id": "success-01"}

    content, structured = _call(server, "memory_status", request)

    assert structured == {
        "status": "OK",
        "results": [],
        "degradation": None,
        "audit": {
            "request_id": "success-01",
            "raw_content_logged": False,
            "host_binding": {
                "host_id": "trusted-host",
                "transport": "stdio",
                "gateway_instance_id": structured["audit"]["host_binding"]["gateway_instance_id"],
                "binding_source": "unit-test",
            },
        },
    }
    _assert_text_structured_parity(content, structured)


def test_e09_internal_gateway_and_adapter_error_semantics_remain_exceptions() -> None:
    adapter = RaisingAdapter("VERSION_CONFLICT")
    bound = _gateway(adapter)

    with pytest.raises(AdapterError) as gateway_error:
        asyncio.run(bound.dispatch("memory_status", deepcopy(TOOL_REQUESTS["memory_status"])))
    assert gateway_error.value.code == "VERSION_CONFLICT"

    with pytest.raises(AdapterError) as adapter_error:
        asyncio.run(adapter.dispatch("memory_status", deepcopy(TOOL_REQUESTS["memory_status"])))
    assert adapter_error.value.code == "VERSION_CONFLICT"


def test_e10_authorization_and_context_failures_remain_fail_closed_with_stable_codes() -> None:
    success_adapter = SuccessAdapter()
    server = build_mcp_server(_gateway(success_adapter))
    _, denied = _call(
        server,
        "memory_status",
        {"scope": "PROJECT", "project_id": "factlane", "trusted_write_context": "spoofed"},
    )
    assert denied["status"] == "BLOCKED"
    assert denied["error_code"] == "WRITE_AUTHORIZATION_DENIED"
    assert success_adapter.calls == []

    context = TrustedContextBinding(project_id="bound-project", binding_source="session-bootstrap")
    mismatch_adapter = SuccessAdapter()
    mismatch_server = build_mcp_server(_gateway(mismatch_adapter, context_binding=context))
    _, mismatch = _call(
        mismatch_server,
        "memory_status",
        {"scope": "PROJECT", "project_id": "other-project"},
    )
    assert mismatch["status"] == "BLOCKED"
    assert mismatch["error_code"] == "BOUND_CONTEXT_IDENTITY_MISMATCH"
    assert mismatch_adapter.calls == []

    compatibility_server = build_mcp_server(_gateway(RaisingAdapter("BACKEND_COMPATIBILITY_MISMATCH")))
    _, compatibility = _call(compatibility_server, "memory_status", TOOL_REQUESTS["memory_status"])
    assert compatibility["status"] == "BLOCKED"
    assert compatibility["error_code"] == "BACKEND_COMPATIBILITY_MISMATCH"


def test_unexpected_exceptions_remain_transport_errors_not_governed_results() -> None:
    server = build_mcp_server(_gateway(RuntimeFailureAdapter()))

    with pytest.raises(ToolError, match="unexpected internal failure"):
        asyncio.run(server.call_tool("memory_status", {"request": TOOL_REQUESTS["memory_status"]}))
