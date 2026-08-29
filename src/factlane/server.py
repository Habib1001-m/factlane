from __future__ import annotations

import argparse
import asyncio
from functools import partial
from typing import Any, Self

from mcp.server.fastmcp import FastMCP

from .adapter import PROFILE_DEFINITIONS, MemoryAdapter
from .contract import AdapterError
from .gateway import HostBinding, MemoryGateway


def _server_dispatch(server: Any, operation: str, *args: Any, **kwargs: Any) -> Any:
    gateway = tuple.__getitem__(server, 0)
    if type(gateway) is not MemoryGateway:
        raise AdapterError("UNBOUND_GATEWAY", "server gateway is not valid")
    gateway_transport = tuple.__getitem__(gateway, 3)
    if operation == "_gateway":
        return gateway
    if operation == "_tool_specs":
        return tuple.__getitem__(server, 1)
    if operation == "tool_names":
        return sorted(name for name, _, _ in tuple.__getitem__(server, 1))
    if operation == "_require_transport":
        selected_transport = args[0] if args else kwargs.get("selected_transport")
        return gateway_transport(gateway, selected_transport)
    if operation == "settings":
        gateway_transport(gateway, "sse")
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "SSE is not supported")
    if operation == "_session_manager":
        gateway_transport(gateway, "streamable-http")
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "streamable HTTP is not supported")
    if operation == "_mcp_server":
        raise AdapterError("UNBOUND_GATEWAY", "base FastMCP dispatch is not available")
    if operation in {"sse_app", "_normalize_path", "run_sse_async"}:
        gateway_transport(gateway, "sse")
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "SSE is not supported")
    if operation in {"streamable_http_app", "run_streamable_http_async"}:
        gateway_transport(gateway, "streamable-http")
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "streamable HTTP is not supported")
    if operation == "run":
        transport = args[0] if args else kwargs.get("transport", "stdio")
        mount_path = args[1] if len(args) > 1 else kwargs.get("mount_path")
        gateway_transport(gateway, transport)
        inner = FastMCP(
            "factlane",
            instructions="Supporting memory only; never execution authority.",
            host="127.0.0.1",
            port=8000,
        )
        for name, description, operation_name in tuple.__getitem__(server, 1):
            tool = partial(tuple.__getitem__(gateway, 2), gateway, operation_name)
            tool_metadata = vars(tool)
            tool_metadata["__name__"] = name
            tool_metadata["__doc__"] = description
            inner.tool(name=name, description=description)(tool)
        return inner.run(transport, mount_path)
    if operation == "run_stdio_async":
        gateway_transport(gateway, "stdio")
        inner = FastMCP(
            "factlane",
            instructions="Supporting memory only; never execution authority.",
            host="127.0.0.1",
            port=8000,
        )
        for name, description, operation_name in tuple.__getitem__(server, 1):
            tool = partial(tuple.__getitem__(gateway, 2), gateway, operation_name)
            tool_metadata = vars(tool)
            tool_metadata["__name__"] = name
            tool_metadata["__doc__"] = description
            inner.tool(name=name, description=description)(tool)
        return inner.run_stdio_async()
    raise AdapterError("UNBOUND_GATEWAY", "server operation is not available")


class _BoundFastMCP(tuple, metaclass=type(HostBinding)):
    """Small guarded facade that does not expose FastMCP base dispatch paths."""

    __slots__ = ()

    def __new__(cls, gateway: MemoryGateway, tools: tuple[tuple[str, str, str], ...]) -> Self:
        return tuple.__new__(cls, (gateway, tools, _server_dispatch))

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("_BoundFastMCP cannot be subclassed")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("server state is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("server state is immutable")

    def __getattribute__(self, name: str) -> object:
        if name == "_gateway":
            return tuple.__getitem__(self, 0)
        if name == "settings" or name == "_session_manager" or name == "_mcp_server":
            return tuple.__getitem__(self, 2)(self, name)
        if (
            name == "_require_transport"
            or name == "_tool_specs"
            or name == "tool_names"
            or name == "sse_app"
            or name == "streamable_http_app"
            or name == "_normalize_path"
            or name == "run"
            or name == "run_stdio_async"
            or name == "run_sse_async"
            or name == "run_streamable_http_async"
        ):
            return partial(tuple.__getitem__(self, 2), self, name)
        return tuple.__getattribute__(self, name)

    def __getitem__(self, key: object) -> Any:
        raise TypeError("server state is private")

    def __iter__(self) -> Any:
        raise TypeError("server state is private")

    def __repr__(self) -> str:
        return "<_BoundFastMCP>"

    @property
    def _gateway(self) -> MemoryGateway:
        return tuple.__getitem__(self, 2)(self, "_gateway")

    def _require_transport(self, selected_transport: str) -> None:
        tuple.__getitem__(self, 2)(self, "_require_transport", selected_transport)

    def _tool_specs(self) -> tuple[tuple[str, str, str], ...]:
        return tuple.__getitem__(self, 2)(self, "_tool_specs")

    @property
    def settings(self) -> Any:
        return tuple.__getitem__(self, 2)(self, "settings")

    @property
    def _session_manager(self) -> Any:
        return tuple.__getitem__(self, 2)(self, "_session_manager")

    @property
    def _mcp_server(self) -> Any:
        return tuple.__getitem__(self, 2)(self, "_mcp_server")

    def tool_names(self) -> list[str]:
        return tuple.__getitem__(self, 2)(self, "tool_names")

    def sse_app(self, mount_path: str | None = None) -> Any:
        return tuple.__getitem__(self, 2)(self, "sse_app", mount_path)

    def streamable_http_app(self) -> Any:
        return tuple.__getitem__(self, 2)(self, "streamable_http_app")

    def _normalize_path(self, mount_path: str, path: str) -> str:
        return tuple.__getitem__(self, 2)(self, "_normalize_path", mount_path, path)

    def run(self, transport: str = "stdio", mount_path: str | None = None) -> None:
        tuple.__getitem__(self, 2)(self, "run", transport, mount_path)

    async def run_stdio_async(self) -> None:
        await tuple.__getitem__(self, 2)(self, "run_stdio_async")

    async def run_sse_async(self, mount_path: str | None = None) -> None:
        await tuple.__getitem__(self, 2)(self, "run_sse_async", mount_path)

    async def run_streamable_http_async(self) -> None:
        await tuple.__getitem__(self, 2)(self, "run_streamable_http_async")


def build_mcp_server(gateway: MemoryGateway) -> _BoundFastMCP:
    """Build only the five normal agent tools over a bound gateway."""
    if type(gateway) is not MemoryGateway:
        raise AdapterError("UNBOUND_GATEWAY", "gateway is not a valid memory gateway")
    tuple.__getitem__(gateway, 3)(gateway, "stdio")

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
