from __future__ import annotations

import argparse
import asyncio
from typing import Any, Self

from mcp.server.fastmcp import FastMCP

from .adapter import PROFILE_DEFINITIONS, MemoryAdapter
from .contract import AdapterError
from .gateway import SUPPORTED_TRANSPORT_KIND, HostBinding, MemoryGateway

STDIO_TRANSPORT = SUPPORTED_TRANSPORT_KIND


class _BoundFastMCP(tuple):
    """Small guarded facade that does not expose FastMCP base dispatch paths."""

    __slots__ = ()

    def __new__(cls, gateway: MemoryGateway, inner: FastMCP) -> Self:
        def invoke(method_name: str, *args: Any, **kwargs: Any) -> Any:
            member = getattr(inner, method_name)
            return member(*args, **kwargs) if callable(member) else member

        return tuple.__new__(cls, (gateway, invoke))

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

    @property
    def settings(self) -> Any:
        self._gateway.require_transport("sse")
        return _call_inner(self, "settings")

    @property
    def _session_manager(self) -> Any:
        self._gateway.require_transport("streamable-http")
        return _call_inner(self, "_session_manager")

    @property
    def _mcp_server(self) -> Any:
        self._gateway.require_transport(STDIO_TRANSPORT)
        return _call_inner(self, "_mcp_server")

    def tool_names(self) -> list[str]:
        return sorted(_call_inner(self, "_tool_manager")._tools)

    def _tool_function(self, name: str) -> Any:
        return _call_inner(self, "_tool_manager")._tools[name].fn

    def sse_app(self, mount_path: str | None = None) -> Any:
        self._gateway.require_transport("sse")
        return _call_inner(self, "sse_app", mount_path)

    def streamable_http_app(self) -> Any:
        self._gateway.require_transport("streamable-http")
        return _call_inner(self, "streamable_http_app")

    def _normalize_path(self, mount_path: str, path: str) -> str:
        self._gateway.require_transport("sse")
        return _call_inner(self, "_normalize_path", mount_path, path)

    def run(self, transport: str = STDIO_TRANSPORT, mount_path: str | None = None) -> None:
        self._gateway.require_transport(transport)
        _call_inner(self, "run", transport, mount_path)

    async def run_stdio_async(self) -> None:
        self._gateway.require_transport(STDIO_TRANSPORT)
        await _call_inner(self, "run_stdio_async")

    async def run_sse_async(self, mount_path: str | None = None) -> None:
        self._gateway.require_transport("sse")
        await _call_inner(self, "run_sse_async", mount_path)

    async def run_streamable_http_async(self) -> None:
        self._gateway.require_transport("streamable-http")
        await _call_inner(self, "run_streamable_http_async")


def _call_inner(server: _BoundFastMCP, method_name: str, *args: Any, **kwargs: Any) -> Any:
    invoke = tuple.__getitem__(server, 1)
    return invoke(method_name, *args, **kwargs)


def build_mcp_server(gateway: MemoryGateway) -> _BoundFastMCP:
    """Build only the five normal agent tools over a bound gateway."""
    if type(gateway) is not MemoryGateway:
        raise AdapterError("UNBOUND_GATEWAY", "gateway is not a valid memory gateway")
    gateway.require_transport(STDIO_TRANSPORT)
    inner = FastMCP(
        "factlane",
        instructions="Supporting memory only; never execution authority.",
        host="127.0.0.1",
        port=8000,
    )
    server = _BoundFastMCP(gateway, inner)

    @inner.tool(name="memory_search", description="Search validated memory in one exact scope.")
    async def memory_search(request: dict[str, Any]) -> dict[str, Any]:
        return await gateway.dispatch("memory_search", request)

    @inner.tool(name="memory_get", description="Get one logical memory record in one exact scope.")
    async def memory_get(request: dict[str, Any]) -> dict[str, Any]:
        return await gateway.dispatch("memory_get", request)

    @inner.tool(name="memory_store", description="Admit one bounded, provenance-bearing memory candidate.")
    async def memory_store(request: dict[str, Any]) -> dict[str, Any]:
        return await gateway.dispatch("memory_store", request)

    @inner.tool(name="memory_update", description="Reverify or explicitly replace one logical memory record.")
    async def memory_update(request: dict[str, Any]) -> dict[str, Any]:
        return await gateway.dispatch("memory_update", request)

    @inner.tool(name="memory_status", description="Return bounded backend/profile status for one scope.")
    async def memory_status(request: dict[str, Any]) -> dict[str, Any]:
        return await gateway.dispatch("memory_status", request)

    return server


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
        binding = HostBinding(args.host_id, STDIO_TRANSPORT, args.binding_source)
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
        gateway = MemoryGateway(adapter, binding, transport_kind=STDIO_TRANSPORT)
        build_mcp_server(gateway).run(STDIO_TRANSPORT)
    finally:
        asyncio.run(adapter.close())


if __name__ == "__main__":
    main()
