from __future__ import annotations

import argparse
import asyncio
from typing import Any
from weakref import WeakKeyDictionary

from mcp.server.fastmcp import FastMCP

from .adapter import PROFILE_DEFINITIONS, MemoryAdapter
from .contract import AdapterError
from .gateway import SUPPORTED_TRANSPORT_KIND, HostBinding, MemoryGateway

STDIO_TRANSPORT = SUPPORTED_TRANSPORT_KIND
_BOUND_SERVER_STATE: WeakKeyDictionary[object, tuple[MemoryGateway, FastMCP]] = WeakKeyDictionary()


class _BoundFastMCP:
    """Small guarded facade that does not expose FastMCP base dispatch paths."""

    __slots__ = ("__weakref__",)

    def __init__(self, gateway: MemoryGateway) -> None:
        _BOUND_SERVER_STATE[self] = (
            gateway,
            FastMCP(
                "factlane",
                instructions="Supporting memory only; never execution authority.",
                host="127.0.0.1",
                port=8000,
            ),
        )

    def _state(self) -> tuple[MemoryGateway, FastMCP]:
        try:
            return _BOUND_SERVER_STATE[self]
        except KeyError as exc:
            raise AdapterError("UNBOUND_GATEWAY", "server gateway is not available") from exc

    @property
    def _tool_manager(self) -> Any:
        return self._state()[1]._tool_manager

    @property
    def settings(self) -> Any:
        gateway, server = self._state()
        gateway.require_transport("sse")
        return server.settings

    @property
    def _session_manager(self) -> Any:
        gateway, server = self._state()
        gateway.require_transport("streamable-http")
        return server._session_manager

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        return self._state()[1].tool(*args, **kwargs)

    def sse_app(self, mount_path: str | None = None) -> Any:
        gateway, server = self._state()
        gateway.require_transport("sse")
        return server.sse_app(mount_path)

    def streamable_http_app(self) -> Any:
        gateway, server = self._state()
        gateway.require_transport("streamable-http")
        return server.streamable_http_app()

    def _normalize_path(self, mount_path: str, path: str) -> str:
        gateway, server = self._state()
        gateway.require_transport("sse")
        return server._normalize_path(mount_path, path)

    def run(self, transport: str = STDIO_TRANSPORT, mount_path: str | None = None) -> None:
        gateway, server = self._state()
        gateway.require_transport(transport)
        server.run(transport, mount_path)  # type: ignore[arg-type]

    async def run_stdio_async(self) -> None:
        gateway, server = self._state()
        gateway.require_transport(STDIO_TRANSPORT)
        await server.run_stdio_async()

    async def run_sse_async(self, mount_path: str | None = None) -> None:
        gateway, server = self._state()
        gateway.require_transport("sse")
        await server.run_sse_async(mount_path)

    async def run_streamable_http_async(self) -> None:
        gateway, server = self._state()
        gateway.require_transport("streamable-http")
        await server.run_streamable_http_async()


def build_mcp_server(gateway: MemoryGateway) -> _BoundFastMCP:
    """Build only the five normal agent tools over a bound gateway."""
    if type(gateway) is not MemoryGateway:
        raise AdapterError("UNBOUND_GATEWAY", "gateway is not a valid memory gateway")
    gateway.require_transport(STDIO_TRANSPORT)
    server = _BoundFastMCP(gateway)

    @server.tool(name="memory_search", description="Search validated memory in one exact scope.")
    async def memory_search(request: dict[str, Any]) -> dict[str, Any]:
        return await gateway.dispatch("memory_search", request)

    @server.tool(name="memory_get", description="Get one logical memory record in one exact scope.")
    async def memory_get(request: dict[str, Any]) -> dict[str, Any]:
        return await gateway.dispatch("memory_get", request)

    @server.tool(name="memory_store", description="Admit one bounded, provenance-bearing memory candidate.")
    async def memory_store(request: dict[str, Any]) -> dict[str, Any]:
        return await gateway.dispatch("memory_store", request)

    @server.tool(name="memory_update", description="Reverify or explicitly replace one logical memory record.")
    async def memory_update(request: dict[str, Any]) -> dict[str, Any]:
        return await gateway.dispatch("memory_update", request)

    @server.tool(name="memory_status", description="Return bounded backend/profile status for one scope.")
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
