from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from factlane.adapter import MemoryAdapter
from factlane.contract import AdapterError
from factlane.embeddings import EmbeddingProfile
from factlane.gateway import HostBinding, MemoryGateway
from factlane.server import build_mcp_server
from factlane.storage import SQLiteVecEngine


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def search(self, **request: Any) -> dict[str, Any]:
        return self._response("memory_search", request)

    async def get(self, **request: Any) -> dict[str, Any]:
        return self._response("memory_get", request)

    async def store(self, **request: Any) -> dict[str, Any]:
        return self._response("memory_store", request)

    async def update(self, **request: Any) -> dict[str, Any]:
        return self._response("memory_update", request)

    async def status(self, **request: Any) -> dict[str, Any]:
        return self._response("memory_status", request)

    def _response(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, request.copy()))
        return {
            "status": "OK",
            "scope": {
                "scope": request.get("scope"),
                "project_id": request.get("project_id"),
                "worktree_id": request.get("worktree_id"),
                "workflow_id": request.get("workflow_id"),
                "agent_id": request.get("agent_id"),
            },
            "results": [],
            "audit": {
                "request_id": request.get("request_id"),
                "raw_content_logged": False,
                "host_binding": {"host_id": "adapter-claim"},
            },
        }


def gateway(*, host_id: str = "codex-disposable", adapter: Any | None = None) -> MemoryGateway:
    return MemoryGateway(
        adapter or FakeAdapter(),
        HostBinding(host_id, "stdio", "trusted-launcher"),
        transport_kind="stdio",
    )


def test_unbound_gateway_fails_closed() -> None:
    unbound = MemoryGateway(FakeAdapter(), None, transport_kind="stdio")

    with pytest.raises(AdapterError) as error:
        asyncio.run(unbound.dispatch("memory_status", {"scope": "PROJECT", "project_id": "p"}))

    assert error.value.code == "UNBOUND_GATEWAY"


def test_binding_validation_and_immutability() -> None:
    binding = HostBinding("codex-disposable", "stdio", "trusted-launcher")
    bound = gateway()

    assert binding.bound_host_id == "codex-disposable"
    assert binding.transport_kind == "stdio"
    assert binding.binding_source == "trusted-launcher"
    assert len(binding.gateway_instance_id) == 32
    assert bound.binding is not None
    with pytest.raises(FrozenInstanceError):
        binding.bound_host_id = "spoofed"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        bound.binding = binding  # type: ignore[misc]


def test_gateway_backing_binding_cannot_be_reassigned() -> None:
    bound = gateway()
    original = bound.binding
    replacement = HostBinding("hermes-disposable", "stdio", "trusted-launcher")

    with pytest.raises(AttributeError):
        del bound._binding  # type: ignore[misc]

    with pytest.raises(AttributeError):
        bound._binding = replacement  # type: ignore[misc]

    assert bound.binding is original


def test_selected_transport_is_required() -> None:
    binding = HostBinding("codex-disposable", "stdio", "trusted-launcher")

    with pytest.raises(TypeError):
        MemoryGateway(FakeAdapter(), binding)  # type: ignore[call-arg]

    with pytest.raises(AdapterError) as error:
        MemoryGateway(FakeAdapter(), binding, transport_kind=None)  # type: ignore[arg-type]

    assert error.value.code == "INVALID_HOST_BINDING"


@pytest.mark.parametrize(
    "bound_host_id",
    [None, "", " ", "codex\ndisposable", "codex\x00disposable", "\ud800", "x" * 129, "/private/path", "secret:" + "a" * 32],
)
def test_invalid_host_binding_fails_closed(bound_host_id: object) -> None:
    with pytest.raises(AdapterError) as error:
        HostBinding(bound_host_id, "stdio", "trusted-launcher")  # type: ignore[arg-type]

    assert error.value.code == "INVALID_HOST_BINDING"


@pytest.mark.parametrize("field", ["transport_kind", "binding_source"])
def test_invalid_transport_or_source_binding_fails_closed(field: str) -> None:
    values: dict[str, object] = {
        "transport_kind": "stdio",
        "binding_source": "trusted-launcher",
    }
    values[field] = "invalid\nvalue"

    with pytest.raises(AdapterError) as error:
        HostBinding("codex-disposable", values["transport_kind"], values["binding_source"])  # type: ignore[arg-type]

    assert error.value.code == "INVALID_HOST_BINDING"


@pytest.mark.parametrize(
    "claim",
    [
        "host_id",
        "bound_host_id",
        "host_identity",
        "transport_identity",
        "gateway_instance_id",
        "runtime_agent_id",
        "transport_kind",
    ],
)
def test_request_identity_claim_is_denied(claim: str) -> None:
    adapter = FakeAdapter()
    request = {"scope": "PROJECT", "project_id": "p", claim: "codex-disposable"}

    with pytest.raises(AdapterError) as error:
        asyncio.run(gateway(adapter=adapter).dispatch("memory_status", request))

    assert error.value.code == "HOST_IDENTITY_CLAIM_DENIED"
    assert adapter.calls == []


def test_matching_request_side_identity_is_still_not_authoritative() -> None:
    adapter = FakeAdapter()
    request = {"scope": "PROJECT", "project_id": "p", "host_id": "codex-disposable"}

    with pytest.raises(AdapterError) as error:
        asyncio.run(gateway(adapter=adapter).dispatch("memory_status", request))

    assert error.value.code == "HOST_IDENTITY_CLAIM_DENIED"
    assert adapter.calls == []


def test_selected_transport_must_match_bound_transport() -> None:
    binding = HostBinding("codex-disposable", "stdio", "trusted-launcher")

    with pytest.raises(AdapterError) as error:
        MemoryGateway(FakeAdapter(), binding, transport_kind="http")

    assert error.value.code == "HOST_TRANSPORT_IDENTITY_MISMATCH"


def test_server_rejects_duck_typed_gateway() -> None:
    class DuckGateway:
        def require_transport(self, selected_transport: str) -> None:
            return None

        async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
            return {"status": "OK"}

    with pytest.raises(AdapterError) as error:
        build_mcp_server(DuckGateway())  # type: ignore[arg-type]

    assert error.value.code == "UNBOUND_GATEWAY"


def test_server_gateway_cannot_be_reassigned() -> None:
    server = build_mcp_server(gateway())
    replacement = gateway(host_id="hermes-disposable")

    with pytest.raises(AttributeError):
        server._gateway = replacement  # type: ignore[misc]

    with pytest.raises(AttributeError):
        del server._gateway  # type: ignore[misc]


def test_server_run_transport_must_match_gateway_binding(monkeypatch) -> None:
    server = build_mcp_server(gateway())

    def unexpected_run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("mismatched transport must be rejected before FastMCP runs")

    monkeypatch.setattr(FastMCP, "run", unexpected_run)

    with pytest.raises(AdapterError) as error:
        server.run("streamable-http")

    assert error.value.code == "HOST_TRANSPORT_IDENTITY_MISMATCH"


def test_base_fastmcp_run_cannot_bypass_gateway_transport(monkeypatch) -> None:
    server = build_mcp_server(gateway())

    async def unexpected_transport_run(self: FastMCP) -> None:
        pytest.fail("base FastMCP.run must not bypass gateway transport validation")

    monkeypatch.setattr(FastMCP, "run_streamable_http_async", unexpected_transport_run)

    with pytest.raises(AdapterError) as error:
        FastMCP.run(server, "streamable-http")

    assert error.value.code == "HOST_TRANSPORT_IDENTITY_MISMATCH"


def test_non_stdio_gateway_transport_fails_closed() -> None:
    binding = HostBinding("codex-disposable", "http", "trusted-launcher")

    with pytest.raises(AdapterError) as error:
        MemoryGateway(FakeAdapter(), binding, transport_kind="http")

    assert error.value.code == "HOST_TRANSPORT_IDENTITY_MISMATCH"


def test_gateway_instances_have_distinct_internal_identities() -> None:
    first = gateway()
    second = gateway(host_id="hermes-disposable")

    assert first.binding is not None
    assert second.binding is not None
    assert first.binding.gateway_instance_id != second.binding.gateway_instance_id


def test_host_binding_does_not_rewrite_scope_agent_id() -> None:
    response = asyncio.run(
        gateway().dispatch(
            "memory_status",
            {"scope": "TOOL_ENVIRONMENT", "agent_id": "scope-agent"},
        )
    )

    assert response["scope"]["agent_id"] == "scope-agent"
    assert response["audit"]["host_binding"]["host_id"] == "codex-disposable"
    assert response["scope"]["agent_id"] != response["audit"]["host_binding"]["host_id"]


def test_audit_binding_is_gateway_owned_bounded_and_immutable() -> None:
    bound = gateway()
    first = asyncio.run(bound.dispatch("memory_status", {"scope": "PROJECT", "project_id": "p"}))
    first["audit"]["host_binding"]["host_id"] = "spoofed"
    second = asyncio.run(bound.dispatch("memory_status", {"scope": "PROJECT", "project_id": "p"}))

    assert second["audit"]["host_binding"] == {
        "host_id": "codex-disposable",
        "transport": "stdio",
        "gateway_instance_id": bound.binding.gateway_instance_id if bound.binding else None,
        "binding_source": "trusted-launcher",
    }
    assert "adapter-claim" not in str(second["audit"]["host_binding"])
    assert all(len(str(value)) <= 128 for value in second["audit"]["host_binding"].values())
    assert "HOME" not in str(second["audit"]["host_binding"])
    assert "token" not in str(second["audit"]["host_binding"]).casefold()


def test_public_mcp_tool_set_remains_exactly_five() -> None:
    server = build_mcp_server(gateway())
    names = sorted(server._tool_manager._tools)  # type: ignore[attr-defined]

    assert names == sorted(MemoryAdapter.tool_names())
    assert len(names) == 5
    assert "memory_identity" not in names
    assert "memory_admin" not in names


def test_unbound_server_fails_closed() -> None:
    with pytest.raises(AdapterError) as error:
        build_mcp_server(MemoryGateway(FakeAdapter(), None, transport_kind="stdio"))

    assert error.value.code == "UNBOUND_GATEWAY"


def test_server_requires_explicit_host_id_before_adapter_start(monkeypatch, tmp_path: Path) -> None:
    def adapter_must_not_start(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("adapter startup must not run without an explicit host binding")

    monkeypatch.setattr("factlane.server.MemoryAdapter.create", adapter_must_not_start)

    with pytest.raises(SystemExit) as error:
        from factlane.server import main

        main(["--db", str(tmp_path / "not-created.sqlite")])

    assert error.value.code == 2


def test_registered_server_handler_dispatches_through_gateway() -> None:
    adapter = FakeAdapter()
    server = build_mcp_server(gateway(adapter=adapter))
    tool = server._tool_manager._tools["memory_status"]  # type: ignore[attr-defined]

    response = asyncio.run(tool.fn({"scope": "PROJECT", "project_id": "p"}))

    assert response["audit"]["host_binding"]["host_id"] == "codex-disposable"
    assert adapter.calls == [("memory_status", {"scope": "PROJECT", "project_id": "p"})]


def test_disposable_mcp_probe_passes_explicit_host_identity() -> None:
    text = Path("tests/acceptance/s6b4b_pilot.py").read_text(encoding="utf-8")

    assert '"--host-id", "acceptance-disposable"' in text


class DeterministicProvider:
    def __init__(self) -> None:
        self.profile = EmbeddingProfile(
            profile_id="gateway-test-2",
            provider_kind="OLLAMA_LOCAL",
            base_model_identity="nomic-embed-text:latest",
            model_digest="0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
            source_dimension=2,
            output_dimension=2,
            normalization_policy="TEST_DETERMINISTIC",
            distance_metric="cosine",
            projection_version="test-v1",
            document_prefix="",
            query_prefix="",
        )
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return [1.0, 0.0]

    def provider_status(self) -> dict[str, object]:
        return {"local_only": True, "profile": self.profile.profile_id}


def disposable_store_request() -> dict[str, Any]:
    return {
        "fact": "A committed disposable FactLane record.",
        "scope": "TOOL_ENVIRONMENT",
        "agent_id": "scope-agent",
        "memory_type": "TOOL_ENVIRONMENT_FACT",
        "source_provenance": {
            "source_class": "TEST",
            "source_ref": "gateway-proof",
            "source_hash": "a" * 64,
            "review_ref": "gateway-proof",
            "extraction_method": "deterministic-test",
        },
        "freshness_policy": {
            "kind": "manual",
            "ttl_seconds": None,
            "recheck_ref": None,
            "source_fingerprint": None,
        },
        "idempotency_key": "gateway-proof-a",
        "requested_lifecycle_state": "VALIDATED_CURRENT",
        "source_timestamp": "2026-08-29T00:00:00Z",
        "last_verified_at": "2026-08-29T00:00:00Z",
        "verified_by": "AUTOMATED_CHECK",
        "request_id": "gateway-request-a",
    }


def test_sequential_cross_gateway_visibility_uses_one_disposable_store(tmp_path: Path) -> None:
    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        provider_a = DeterministicProvider()
        provider_b = DeterministicProvider()
        db_path = tmp_path / "shared-disposable.sqlite"
        engine_a = SQLiteVecEngine(str(db_path), provider_a.profile)
        engine_b = SQLiteVecEngine(str(db_path), provider_b.profile)
        await engine_a.open()
        await engine_b.open()
        adapter_a = MemoryAdapter(engine_a, provider_a)
        adapter_b = MemoryAdapter(engine_b, provider_b)
        gateway_a = MemoryGateway(
            adapter_a,
            HostBinding("codex-disposable", "stdio", "trusted-launcher"),
            transport_kind="stdio",
        )
        gateway_b = MemoryGateway(
            adapter_b,
            HostBinding("hermes-disposable", "stdio", "trusted-launcher"),
            transport_kind="stdio",
        )
        try:
            write = await gateway_a.dispatch("memory_store", disposable_store_request())
            memory_id = write["results"][0]["memory_id"]
            read = await gateway_b.dispatch(
                "memory_get",
                {
                    "memory_id": memory_id,
                    "scope": "TOOL_ENVIRONMENT",
                    "agent_id": "scope-agent",
                    "request_id": "gateway-request-b",
                },
            )
            return write, read
        finally:
            await adapter_b.close()
            await adapter_a.close()

    write, read = asyncio.run(run())

    assert write["status"] == "OK"
    assert write["scope"]["agent_id"] == "scope-agent"
    assert write["audit"]["host_binding"]["host_id"] == "codex-disposable"
    assert read["results"][0]["memory_id"] == write["results"][0]["memory_id"]
    assert read["scope"]["agent_id"] == "scope-agent"
    assert read["audit"]["host_binding"]["host_id"] == "hermes-disposable"
    assert write["audit"]["host_binding"]["gateway_instance_id"] != read["audit"]["host_binding"]["gateway_instance_id"]


def test_gateway_rejects_unknown_operation() -> None:
    with pytest.raises(AdapterError) as error:
        asyncio.run(gateway().dispatch("memory_identity", {}))

    assert error.value.code == "INVALID_OPERATION"
