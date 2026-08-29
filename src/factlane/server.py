from __future__ import annotations

import argparse
import asyncio
from typing import Any, Self

from mcp.server.fastmcp import FastMCP

from .adapter import PROFILE_DEFINITIONS, MemoryAdapter
from .contract import AdapterError
from .gateway import HostBinding, MemoryGateway


class _GatewayTool(tuple, metaclass=type(HostBinding)):
    __slots__ = ()
    __name__ = "memory_tool"
    __doc__ = "Bound FactLane memory operation."

    def __new__(cls, gateway: MemoryGateway, operation: str) -> Self:
        return tuple.__new__(cls, (gateway, operation))

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("_GatewayTool cannot be subclassed")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("tool state is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("tool state is immutable")

    async def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        gateway = tuple.__getitem__(self, 0)
        if type(gateway) is not MemoryGateway:
            raise AdapterError("UNBOUND_GATEWAY", "server gateway is not valid")
        return await MemoryGateway.dispatch(gateway, tuple.__getitem__(self, 1), request)


class _BoundFastMCP(tuple, metaclass=type(HostBinding)):
    """Small guarded facade that does not expose FastMCP base dispatch paths."""

    __slots__ = ()

    def __new__(cls, gateway: MemoryGateway, tools: tuple[tuple[str, str, str], ...]) -> Self:
        return tuple.__new__(cls, (gateway, tools))

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("_BoundFastMCP cannot be subclassed")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("server state is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("server state is immutable")

    def __getitem__(self, key: object) -> Any:
        raise TypeError("server state is private")

    def __iter__(self) -> Any:
        raise TypeError("server state is private")

    def __repr__(self) -> str:
        return "<_BoundFastMCP>"

    @property
    def _gateway(self) -> MemoryGateway:
        return tuple.__getitem__(self, 0)

    def _require_transport(self, selected_transport: str) -> None:
        gateway = self._gateway
        if type(gateway) is not MemoryGateway:
            raise AdapterError("UNBOUND_GATEWAY", "server gateway is not valid")
        gateway.require_transport(selected_transport)

    def _tool_specs(self) -> tuple[tuple[str, str, str], ...]:
        return tuple.__getitem__(self, 1)

    @property
    def settings(self) -> Any:
        self._require_transport("sse")
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "SSE is not supported")

    @property
    def _session_manager(self) -> Any:
        self._require_transport("streamable-http")
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "streamable HTTP is not supported")

    @property
    def _mcp_server(self) -> Any:
        raise AdapterError("UNBOUND_GATEWAY", "base FastMCP dispatch is not available")

    def tool_names(self) -> list[str]:
        return sorted(name for name, _, _ in self._tool_specs())

    def sse_app(self, mount_path: str | None = None) -> Any:
        self._require_transport("sse")
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "SSE is not supported")

    def streamable_http_app(self) -> Any:
        self._require_transport("streamable-http")
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "streamable HTTP is not supported")

    def _normalize_path(self, mount_path: str, path: str) -> str:
        self._require_transport("sse")
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "SSE is not supported")

    def run(self, transport: str = "stdio", mount_path: str | None = None) -> None:
        self._require_transport(transport)
        inner = FastMCP(
            "factlane",
            instructions="Supporting memory only; never execution authority.",
            host="127.0.0.1",
            port=8000,
        )
        for name, description, operation in self._tool_specs():
            inner.tool(name=name, description=description)(_GatewayTool(self._gateway, operation))
        inner.run(transport, mount_path)

    async def run_stdio_async(self) -> None:
        self._require_transport("stdio")
        inner = FastMCP(
            "factlane",
            instructions="Supporting memory only; never execution authority.",
            host="127.0.0.1",
            port=8000,
        )
        for name, description, operation in self._tool_specs():
            inner.tool(name=name, description=description)(_GatewayTool(self._gateway, operation))
        await inner.run_stdio_async()

    async def run_sse_async(self, mount_path: str | None = None) -> None:
        self._require_transport("sse")
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "SSE is not supported")

    async def run_streamable_http_async(self) -> None:
        self._require_transport("streamable-http")
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "streamable HTTP is not supported")


def build_mcp_server(gateway: MemoryGateway) -> _BoundFastMCP:
    """Build only the five normal agent tools over a bound gateway."""
    if type(gateway) is not MemoryGateway:
        raise AdapterError("UNBOUND_GATEWAY", "gateway is not a valid memory gateway")
    gateway.require_transport("stdio")

    return _BoundFastMCP(
        gateway,
        (
            ("memory_search", "Search validated memory in one exact scope.", "memory_search"),
            ("memory_get", "Get one logical memory record in one exact scope.", "memory_get"),
            ("memory_store", "Admit one bounded, provenance-bearing memory candidate.", "memory_store"),
            ("memory_update", "Reverify or explicitly replace one logical memory record.", "memory_update"),
            ("memory_status", "Return bounded backend/profile status for one scope.", "memory_status"),
        ),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--profile", choices=sorted(PROFILE_DEFINITIONS), default="nomic-768")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--tokenizer-path", default=None)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--binding-source", default="explicit-launcher")
    args = parser.parse_args(argv)
    try:
        binding = HostBinding(args.host_id, "stdio", args.binding_source)
    except AdapterError as exc:
        parser.error(exc.safe_message)
    adapter = asyncio.run(
        MemoryAdapter.create(
            args.db,
            args.profile,
            ollama_url=args.ollama_url,
            tokenizer_path=args.tokenizer_path,
        )
    )
    try:
        gateway = MemoryGateway(adapter, binding, transport_kind="stdio")
        build_mcp_server(gateway).run("stdio")
    finally:
        asyncio.run(adapter.close())


if __name__ == "__main__":
    main()
