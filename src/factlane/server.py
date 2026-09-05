from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from .adapter import PROFILE_DEFINITIONS, MemoryAdapter
from .contract import AdapterError
from .gateway import HostBinding, MemoryGateway
from .public_contract import (
    MemoryGetRequest,
    MemorySearchRequest,
    MemoryStatusRequest,
    MemoryStoreRequest,
    MemoryUpdateRequest,
    TOOL_DESCRIPTIONS,
    render_tool_help,
)

STDIO_TRANSPORT = "stdio"


class _BoundFastMCP(FastMCP):
    """FastMCP server restricted to the transport bound at construction."""

    def run(self, transport: str = STDIO_TRANSPORT, mount_path: str | None = None) -> None:
        if transport != STDIO_TRANSPORT:
            raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "selected transport does not match its binding")
        super().run(transport, mount_path)

    async def run_sse_async(self, mount_path: str | None = None) -> None:
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "SSE is not supported")

    async def run_streamable_http_async(self) -> None:
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "streamable HTTP is not supported")

    def sse_app(self, mount_path: str | None = None) -> Any:
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "SSE is not supported")

    def streamable_http_app(self) -> Any:
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "streamable HTTP is not supported")


def build_mcp_server(gateway: MemoryGateway) -> FastMCP:
    """Build only the five normal agent tools over a bound gateway."""
    if not isinstance(gateway, MemoryGateway):
        raise AdapterError("UNBOUND_GATEWAY", "gateway is not a valid memory gateway")
    gateway.require_transport(STDIO_TRANSPORT)
    server = _BoundFastMCP(
        "factlane",
        instructions="Supporting memory only; never execution authority.",
        host="127.0.0.1",
        port=8000,
    )

    @server.tool(name="memory_search", description=TOOL_DESCRIPTIONS["memory_search"])
    async def memory_search(request: MemorySearchRequest) -> dict[str, Any]:
        return await gateway.dispatch("memory_search", request)

    @server.tool(name="memory_get", description=TOOL_DESCRIPTIONS["memory_get"])
    async def memory_get(request: MemoryGetRequest) -> dict[str, Any]:
        return await gateway.dispatch("memory_get", request)

    @server.tool(name="memory_store", description=TOOL_DESCRIPTIONS["memory_store"])
    async def memory_store(request: MemoryStoreRequest) -> dict[str, Any]:
        return await gateway.dispatch("memory_store", request)

    @server.tool(name="memory_update", description=TOOL_DESCRIPTIONS["memory_update"])
    async def memory_update(request: MemoryUpdateRequest) -> dict[str, Any]:
        return await gateway.dispatch("memory_update", request)

    @server.tool(name="memory_status", description=TOOL_DESCRIPTIONS["memory_status"])
    async def memory_status(request: MemoryStatusRequest) -> dict[str, Any]:
        return await gateway.dispatch("memory_status", request)

    return server


def main(argv: list[str] | None = None) -> None:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    help_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    help_parser.add_argument("--help-tools", action="store_true")
    help_parser.add_argument("--help-tool", choices=sorted(TOOL_DESCRIPTIONS))
    help_args, _ = help_parser.parse_known_args(raw_argv)
    if help_args.help_tools or help_args.help_tool:
        print(render_tool_help(help_args.help_tool))
        return

    parser = argparse.ArgumentParser(
        description="Launch the FactLane stdio MCP server (requires --db and --host-id)."
    )
    parser.add_argument("--db", required=True, help="SQLite database path (required for server launch)")
    parser.add_argument("--profile", choices=sorted(PROFILE_DEFINITIONS), default="nomic-768")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--tokenizer-path", default=None)
    parser.add_argument("--host-id", required=True, help="stable non-secret host label (required for server launch)")
    parser.add_argument("--binding-source", default="explicit-launcher")
    parser.add_argument("--help-tools", action="store_true", help="print the complete five-tool request reference")
    parser.add_argument("--help-tool", choices=sorted(TOOL_DESCRIPTIONS), help="print one tool's request reference")
    args = parser.parse_args(raw_argv)
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
