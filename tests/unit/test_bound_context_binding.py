from __future__ import annotations

import asyncio
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter
from factlane.gateway import HostBinding, MemoryGateway, TrustedContextBinding


class RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
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
            "audit": {},
        }


def bound_gateway(adapter: RecordingAdapter, binding: TrustedContextBinding) -> MemoryGateway:
    return MemoryGateway(
        adapter,
        HostBinding("host-a", "stdio", "trusted-launcher"),
        transport_kind="stdio",
        context_binding=binding,
    )


def test_project_only_binding_preserves_caller_selected_workflow_and_input() -> None:
    adapter = RecordingAdapter()
    request = {"scope": "WORKFLOW", "workflow_id": "caller-selected-workflow"}
    binding = TrustedContextBinding(project_id="project-a", binding_source="session-bootstrap")

    asyncio.run(bound_gateway(adapter, binding).dispatch("memory_status", request))

    assert request == {"scope": "WORKFLOW", "workflow_id": "caller-selected-workflow"}
    assert adapter.calls == [
        (
            "memory_status",
            {
                "scope": "WORKFLOW",
                "workflow_id": "caller-selected-workflow",
                "project_id": "project-a",
            },
        )
    ]


def test_project_binding_does_not_mutate_original_project_request() -> None:
    adapter = RecordingAdapter()
    request = {"scope": "PROJECT"}
    binding = TrustedContextBinding(project_id="project-a", binding_source="session-bootstrap")

    asyncio.run(bound_gateway(adapter, binding).dispatch("memory_status", request))

    assert request == {"scope": "PROJECT"}
    assert adapter.calls[0][1] == {"scope": "PROJECT", "project_id": "project-a"}


@pytest.mark.parametrize("operation", MemoryAdapter.tool_names())
def test_project_binding_applies_to_all_five_public_operations(operation: str) -> None:
    adapter = RecordingAdapter()
    binding = TrustedContextBinding(project_id="project-a", binding_source="session-bootstrap")

    asyncio.run(bound_gateway(adapter, binding).dispatch(operation, {"scope": "PROJECT"}))

    assert adapter.calls == [(operation, {"scope": "PROJECT", "project_id": "project-a"})]


def test_project_binding_does_not_force_identity_free_scopes() -> None:
    adapter = RecordingAdapter()
    binding = TrustedContextBinding(project_id="project-a", binding_source="session-bootstrap")
    gateway = bound_gateway(adapter, binding)

    asyncio.run(gateway.dispatch("memory_status", {"scope": "GLOBAL_USER"}))
    asyncio.run(gateway.dispatch("memory_status", {"scope": "CROSS_PROJECT_WORKFLOW"}))

    assert adapter.calls == [
        ("memory_status", {"scope": "GLOBAL_USER"}),
        ("memory_status", {"scope": "CROSS_PROJECT_WORKFLOW"}),
    ]


def test_project_scope_preserves_caller_directed_agent_filter() -> None:
    adapter = RecordingAdapter()
    binding = TrustedContextBinding(
        project_id="project-a",
        agent_id="CODEX",
        binding_source="session-bootstrap",
    )

    asyncio.run(
        bound_gateway(adapter, binding).dispatch(
            "memory_status",
            {"scope": "PROJECT", "agent_id": "HERMES"},
        )
    )

    assert adapter.calls == [
        ("memory_status", {"scope": "PROJECT", "agent_id": "HERMES", "project_id": "project-a"})
    ]


def test_tool_environment_scope_preserves_caller_directed_project_filter() -> None:
    adapter = RecordingAdapter()
    binding = TrustedContextBinding(
        project_id="project-a",
        agent_id="CODEX",
        binding_source="session-bootstrap",
    )

    asyncio.run(
        bound_gateway(adapter, binding).dispatch(
            "memory_status",
            {"scope": "TOOL_ENVIRONMENT", "project_id": "project-b"},
        )
    )

    assert adapter.calls == [
        ("memory_status", {"scope": "TOOL_ENVIRONMENT", "project_id": "project-b", "agent_id": "CODEX"})
    ]
