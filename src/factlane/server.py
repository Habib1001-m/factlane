from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from mcp.server.fastmcp import FastMCP

from .adapter import PROFILE_DEFINITIONS, MemoryAdapter
from .contract import AdapterError
from .gateway import HostBinding, MemoryGateway, _MemoryGateway


def _reject_transport(kind: str, *args: Any, **kwargs: Any) -> Any:
    raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", f"{kind} is not supported")


def _check_transport(
    require_transport: Callable[[str], Any],
    selected_transport: str,
) -> Any:
    return require_transport(selected_transport)


def _tool_names(specs: tuple[tuple[str, str, str], ...]) -> list[str]:
    return sorted(name for name, _, _ in specs)


def _server_run(
    dispatch: Callable[..., Awaitable[dict[str, Any]]],
    require_transport: Callable[[str], Any],
    specs: tuple[tuple[str, str, str], ...],
    transport: str = "stdio",
    mount_path: str | None = None,
) -> None:
    require_transport(transport)
    inner = FastMCP(
        "factlane",
        instructions="Supporting memory only; never execution authority.",
        host="127.0.0.1",
        port=8000,
    )
    for name, description, operation in specs:
        tool = partial(dispatch, operation)
        tool_metadata = vars(tool)
        tool_metadata["__name__"] = name
        tool_metadata["__doc__"] = description
        inner.tool(name=name, description=description)(tool)
    inner.run(transport, mount_path)


async def _server_run_stdio_async(
    dispatch: Callable[..., Awaitable[dict[str, Any]]],
    require_transport: Callable[[str], Any],
    specs: tuple[tuple[str, str, str], ...],
) -> None:
    require_transport("stdio")
    inner = FastMCP(
        "factlane",
        instructions="Supporting memory only; never execution authority.",
        host="127.0.0.1",
        port=8000,
    )
    for name, description, operation in specs:
        tool = partial(dispatch, operation)
        tool_metadata = vars(tool)
        tool_metadata["__name__"] = name
        tool_metadata["__doc__"] = description
        inner.tool(name=name, description=description)(tool)
    await inner.run_stdio_async()


class _RejectedTransportAccess:
    __slots__ = ("_kind",)

    def __init__(self, kind: str) -> None:
        object.__setattr__(self, "_kind", kind)

    def __getattribute__(self, name: str) -> Any:
        if name == "__class__":
            return object.__getattribute__(self, "__class__")
        kind = object.__getattribute__(self, "_kind")
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", f"{kind} is not supported")


class _BoundFastMCP:
    """Guarded composition facade without retained inner FastMCP state."""

    __slots__ = ("__dict__",)

    def __init__(
        self,
        gateway: _MemoryGateway,
        dispatch: Callable[..., Awaitable[dict[str, Any]]],
        require_transport: Callable[[str], Any],
        specs: tuple[tuple[str, str, str], ...],
    ) -> None:
        state = object.__getattribute__(self, "__dict__")
        state.update(
            {
                "_gateway_value": gateway,
                "_tool_specs_value": specs,
                "_require_transport": partial(_check_transport, require_transport),
                "tool_names": partial(_tool_names, specs),
                "run": partial(_server_run, dispatch, require_transport, specs),
                "run_stdio_async": partial(_server_run_stdio_async, dispatch, require_transport, specs),
                "run_sse_async": partial(_reject_transport, "SSE"),
                "run_streamable_http_async": partial(_reject_transport, "streamable HTTP"),
                "sse_app": partial(_reject_transport, "SSE"),
                "streamable_http_app": partial(_reject_transport, "streamable HTTP"),
                "_normalize_path": partial(_reject_transport, "SSE"),
                "settings": _RejectedTransportAccess("SSE"),
                "_session_manager": _RejectedTransportAccess("streamable HTTP"),
                "_mcp_server": _RejectedTransportAccess("base FastMCP dispatch"),
            }
        )

    @property
    def _gateway(self) -> _MemoryGateway:
        return object.__getattribute__(self, "_gateway_value")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("server state is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("server state is immutable")


def build_mcp_server(gateway: _MemoryGateway) -> _BoundFastMCP:
    """Build only the five normal agent tools over a bound gateway."""
    if type(gateway) is not _MemoryGateway:
        raise AdapterError("UNBOUND_GATEWAY", "gateway is not a valid memory gateway")
    require_transport = object.__getattribute__(gateway, "require_transport")
    require_transport("stdio")
    dispatch = object.__getattribute__(gateway, "dispatch")
    specs = (
        ("memory_search", "Search validated memory in one exact scope.", "memory_search"),
        ("memory_get", "Get one logical memory record in one exact scope.", "memory_get"),
        ("memory_store", "Admit one bounded, provenance-bearing memory candidate.", "memory_store"),
        ("memory_update", "Reverify or explicitly replace one logical memory record.", "memory_update"),
        ("memory_status", "Return bounded backend/profile status for one scope.", "memory_status"),
    )
    return _BoundFastMCP(gateway, dispatch, require_transport, specs)


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
