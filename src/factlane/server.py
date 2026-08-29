from __future__ import annotations

import argparse
import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP

from .adapter import MemoryAdapter, PROFILE_DEFINITIONS


def build_mcp_server(adapter: MemoryAdapter) -> FastMCP:
    """Build only the five normal agent tools."""
    server = FastMCP(
        "factlane",
        instructions="Supporting memory only; never execution authority.",
        host="127.0.0.1",
        port=8000,
    )

    @server.tool(name="memory_search", description="Search validated memory in one exact scope.")
    async def memory_search(request: dict[str, Any]) -> dict[str, Any]:
        return await adapter.search(**request)

    @server.tool(name="memory_get", description="Get one logical memory record in one exact scope.")
    async def memory_get(request: dict[str, Any]) -> dict[str, Any]:
        return await adapter.get(**request)

    @server.tool(name="memory_store", description="Admit one bounded, provenance-bearing memory candidate.")
    async def memory_store(request: dict[str, Any]) -> dict[str, Any]:
        return await adapter.store(**request)

    @server.tool(name="memory_update", description="Reverify or explicitly replace one logical memory record.")
    async def memory_update(request: dict[str, Any]) -> dict[str, Any]:
        return await adapter.update(**request)

    @server.tool(name="memory_status", description="Return bounded backend/profile status for one scope.")
    async def memory_status(request: dict[str, Any]) -> dict[str, Any]:
        return await adapter.status(**request)

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--profile", choices=sorted(PROFILE_DEFINITIONS), default="nomic-768")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--tokenizer-path", default=None)
    args = parser.parse_args(argv)
    adapter = asyncio.run(
        MemoryAdapter.create(
            args.db,
            args.profile,
            ollama_url=args.ollama_url,
            tokenizer_path=args.tokenizer_path,
        )
    )
    try:
        build_mcp_server(adapter).run("stdio")
    finally:
        asyncio.run(adapter.close())


if __name__ == "__main__":
    main()
