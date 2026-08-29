from __future__ import annotations

import argparse
import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP

from .adapter import PROFILE_DEFINITIONS, MemoryAdapter
from .contract import AdapterError
from .gateway import SUPPORTED_TRANSPORT_KIND, HostBinding, MemoryGateway

STDIO_TRANSPORT = SUPPORTED_TRANSPORT_KIND


class _BoundFastMCP(FastMCP):
    def __init__(self, gateway: MemoryGateway) -> None:
        self._gateway = gateway
        super().__init__(
            "factlane",
            instructions="Supporting memory only; never execution authority.",
            host="127.0.0.1",
            port=8000,
        )

    def run(self, transport: str = STDIO_TRANSPORT, mount_path: str | None = None) -> None:
        self._gateway.require_transport(transport)
        super().run(transport, mount_path)  # type: ignore[arg-type]


def build_mcp_server(gateway: MemoryGateway) -> FastMCP:
    """Build only the five normal agent tools over a bound gateway."""
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
