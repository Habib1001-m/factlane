from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import socket
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .adapter import PROFILE_DEFINITIONS, MemoryAdapter, trusted_write_context_for_profile
from .contract import AdapterError, validate_identifier
from .gateway import HostBinding, MemoryGateway, TrustedContextBinding
from .maintenance_coordinator import MaintenanceBarrierStore, initialize_barrier_for_startup
from .public_contract import (
    MemoryGetRequest,
    MemorySearchRequest,
    MemoryStatusRequest,
    MemoryStoreRequest,
    MemoryUpdateRequest,
    TOOL_DESCRIPTIONS,
    render_tool_help,
)
from .runtime_control import (
    PROTOCOL_VERSION,
    RuntimeControlError,
    RuntimeControlServer,
    RuntimeIdentity,
    RuntimeLifecycleController,
    RuntimeRegistry,
    WindowsRuntimeControlServer,
    current_boot_id,
    process_start_ticks,
    windows_runtime_pipe_name,
)
from .windows_maintenance import WindowsMaintenanceError, WindowsRuntimeDBLease
from .windows_security import (
    WindowsSecurityError,
    current_user_sid,
    ensure_owner_private_directory as ensure_windows_owner_private_directory,
    process_identity as windows_process_identity,
    validate_windows_path_no_reparse,
)
from .webui_windows import WindowsWebUIError, windows_boot_id, windows_owner_paths

STDIO_TRANSPORT = "stdio"


@dataclass(frozen=True, slots=True)
class _AdapterLaunchConfig:
    """Immutable inputs required to construct the same governed adapter after maintenance."""

    db_path: str
    profile_name: str
    ollama_url: str
    tokenizer_path: str | None
    trusted_write_context: Any


AdapterFactory = Callable[..., Awaitable[Any]]
LeaseProbe = Callable[[Any], bool]


def _default_lease_probe(adapter: Any) -> bool:
    """Verify that a production adapter still owns an open engine lease."""
    engine = getattr(adapter, "engine", None)
    if engine is None:
        # Narrow injected-adapter seam used by existing server tests. Production
        # MemoryAdapter always exposes its SQLiteVecEngine as ``engine``.
        return True
    if bool(getattr(engine, "_closed", False)):
        return False
    if hasattr(engine, "_maintenance_lease_handle"):
        return getattr(engine, "_maintenance_lease_handle") is not None
    return getattr(engine, "conn", None) is not None or getattr(engine, "storage", None) is not None


class _ReopenableAdapterHolder:
    """Keep FastMCP alive while the real adapter is closed for maintenance."""

    def __init__(
        self,
        config: _AdapterLaunchConfig,
        *,
        adapter_factory: AdapterFactory,
        lease_probe: LeaseProbe = _default_lease_probe,
    ) -> None:
        self._config = config
        self._adapter_factory = adapter_factory
        self._lease_probe = lease_probe
        self._adapter: Any | None = None
        self._controller: RuntimeLifecycleController | None = None
        self._condition = asyncio.Condition()
        self._inflight = 0
        self._closing = False

    def bind_controller(self, controller: RuntimeLifecycleController) -> None:
        if self._controller is not None and self._controller is not controller:
            raise RuntimeError("runtime lifecycle controller is already bound")
        self._controller = controller

    def _accepting(self) -> bool:
        if self._closing or self._adapter is None:
            return False
        controller = self._controller
        return controller is None or controller.admits_normal_requests

    async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        async with self._condition:
            if not self._accepting():
                raise AdapterError(
                    "MAINTENANCE_IN_PROGRESS",
                    "memory is temporarily unavailable while local maintenance is in progress",
                    retry_action="retry after maintenance completes",
                )
            adapter = self._adapter
            assert adapter is not None
            self._inflight += 1
        try:
            result = await adapter.dispatch(operation, request)
            if not isinstance(result, dict):
                raise AdapterError("INVALID_ADAPTER_RESPONSE", "adapter returned an invalid response")
            return result
        finally:
            async with self._condition:
                self._inflight -= 1
                if self._inflight == 0:
                    self._condition.notify_all()

    async def open_adapter(self, generation: int) -> None:
        del generation  # generation binding is enforced by RuntimeLifecycleController/barrier.
        async with self._condition:
            if self._adapter is not None:
                if self._lease_probe(self._adapter):
                    return
                raise AdapterError("BACKEND_UNAVAILABLE", "existing adapter state is not safely reopenable")
            self._closing = True
        try:
            adapter = await self._adapter_factory(
                self._config.db_path,
                self._config.profile_name,
                ollama_url=self._config.ollama_url,
                tokenizer_path=self._config.tokenizer_path,
                trusted_write_context=self._config.trusted_write_context,
            )
            if not self._lease_probe(adapter):
                close = getattr(adapter, "close", None)
                if callable(close):
                    await close()
                raise AdapterError("BACKEND_UNAVAILABLE", "adapter opened without a verified database lease")
        except BaseException:
            async with self._condition:
                self._closing = False
                self._condition.notify_all()
            raise
        async with self._condition:
            self._adapter = adapter
            self._closing = False
            self._condition.notify_all()

    async def close_adapter(self) -> None:
        async with self._condition:
            self._closing = True
            while self._inflight:
                await self._condition.wait()
            adapter = self._adapter
        if adapter is None:
            async with self._condition:
                self._closing = False
                self._condition.notify_all()
            return
        try:
            await adapter.close()
        except BaseException:
            async with self._condition:
                self._closing = False
                self._condition.notify_all()
            raise
        async with self._condition:
            if self._adapter is adapter:
                self._adapter = None
            self._closing = False
            self._condition.notify_all()

    def lease_is_open(self) -> bool:
        adapter = self._adapter
        return adapter is not None and self._lease_probe(adapter)


class _RuntimeSession:
    def __init__(
        self,
        *,
        holder: _ReopenableAdapterHolder,
        registry: RuntimeRegistry,
        barrier: MaintenanceBarrierStore,
        controller: RuntimeLifecycleController,
        control_server: Any,
    ) -> None:
        self.holder = holder
        self.registry = registry
        self.barrier = barrier
        self.controller = controller
        self.control_server = control_server
        self._started = False

    @property
    def identity(self) -> RuntimeIdentity:
        return self.controller.identity

    async def start(self) -> None:
        if self._started:
            return
        await self.control_server.start()
        try:
            loop = asyncio.get_running_loop()
            retry_deadline = loop.time() + 5.0
            while True:
                try:
                    await self.controller.startup_open()
                    break
                except RuntimeControlError as exc:
                    if exc.code != "BARRIER_BUSY" or loop.time() >= retry_deadline:
                        raise
                    await asyncio.sleep(0.01)
        except BaseException:
            await self.control_server.close()
            await self.holder.close_adapter()
            self._remove_registration()
            raise
        self._started = True

    async def close(self) -> None:
        await self.control_server.close()
        try:
            await self.holder.close_adapter()
        finally:
            self._remove_registration()
            self._started = False

    def _remove_registration(self) -> None:
        path = self.registry.registry_root / f"{self.identity.instance_id}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # A stale private registration remains fail-closed and is validated by
            # PID/start/boot identity before any coordinator can rely on it.
            pass


def _windows_process_start_provider(pid: int) -> int | None:
    try:
        return windows_process_identity(pid).creation_time_us
    except WindowsSecurityError:
        return None


def _windows_owner_namespace() -> tuple[str, Any]:
    try:
        owner_sid = current_user_sid()
        paths = windows_owner_paths(owner_sid=owner_sid)
        for raw in (paths.root_dir, paths.runtime_dir, paths.state_dir):
            ensure_windows_owner_private_directory(raw, owner_sid=owner_sid)
        return owner_sid, paths
    except (WindowsSecurityError, WindowsWebUIError) as exc:
        raise AdapterError("BACKEND_UNAVAILABLE", "native Windows runtime custody is unavailable") from exc


def _build_runtime_session(
    config: _AdapterLaunchConfig,
    *,
    runtime_root: str | os.PathLike[str],
    barrier_root: str | os.PathLike[str] | None = None,
    adapter_factory: AdapterFactory = MemoryAdapter.create,
    lease_probe: LeaseProbe = _default_lease_probe,
    holder: _ReopenableAdapterHolder | None = None,
) -> _RuntimeSession:
    root = Path(runtime_root).absolute()
    durable_root = Path(barrier_root).absolute() if barrier_root is not None else root / "maintenance"
    owner_sid: str | None = None
    windows_process = None
    windows_boot: str | None = None
    if os.name == "nt":
        try:
            owner_sid = current_user_sid()
            windows_process = windows_process_identity(os.getpid())
            windows_boot = windows_boot_id()
            validate_windows_path_no_reparse(config.db_path)
            ensure_windows_owner_private_directory(root, owner_sid=owner_sid)
            ensure_windows_owner_private_directory(durable_root, owner_sid=owner_sid)
        except (WindowsSecurityError, WindowsWebUIError) as exc:
            raise AdapterError("BACKEND_UNAVAILABLE", "native Windows runtime path custody is unavailable") from exc
        db_path = os.path.realpath(os.path.abspath(config.db_path))
        if not os.path.exists(db_path):
            anchor: WindowsRuntimeDBLease | None = None
            try:
                anchor = WindowsRuntimeDBLease.open(db_path)
            except WindowsMaintenanceError as exc:
                code = "MAINTENANCE_IN_PROGRESS" if exc.code == "MAINTENANCE_IN_PROGRESS" else "BACKEND_UNAVAILABLE"
                raise AdapterError(code, "database maintenance coordination is unavailable") from exc
            finally:
                if anchor is not None:
                    anchor.close()
        info = os.stat(db_path)
        registry = RuntimeRegistry(
            root,
            owner_uid=0,
            boot_id_provider=windows_boot_id,
            process_start_provider=_windows_process_start_provider,
        )
        barrier = MaintenanceBarrierStore(durable_root, db_path)
    else:
        db_path = os.path.realpath(os.path.abspath(config.db_path))
        info = os.stat(db_path)
        registry = RuntimeRegistry(root, owner_uid=os.getuid())
        barrier = MaintenanceBarrierStore(durable_root, db_path)
    snapshot = initialize_barrier_for_startup(barrier, generation=1)
    if (snapshot.device, snapshot.inode) != (info.st_dev, info.st_ino):
        raise AdapterError(
            "DATABASE_IDENTITY_MISMATCH",
            "database inode changed outside the governed maintenance barrier",
        )
    instance_id = str(uuid.uuid4())
    if os.name == "nt":
        assert owner_sid is not None
        assert windows_process is not None
        assert windows_boot is not None
        start_ticks = windows_process.creation_time_us
        boot_id = windows_boot
        control_socket_path = windows_runtime_pipe_name(instance_id)
    else:
        start_ticks = process_start_ticks(os.getpid())
        if start_ticks is None:
            raise RuntimeError("current process start identity is unavailable")
        boot_id = current_boot_id()
        control_socket_path = str(root / f"{uuid.UUID(instance_id).hex[:16]}.sock")
    identity = RuntimeIdentity(
        protocol_version=PROTOCOL_VERSION,
        instance_id=instance_id,
        owner_uid=registry.owner_uid,
        pid=os.getpid(),
        process_start_ticks=start_ticks,
        boot_id=boot_id,
        database_path=db_path,
        device=snapshot.device if snapshot.device else info.st_dev,
        inode=snapshot.inode if snapshot.inode else info.st_ino,
        generation=snapshot.generation,
        control_socket_path=control_socket_path,
        lifecycle_state="PARKED",
    )
    adapter_holder = holder or _ReopenableAdapterHolder(
        config,
        adapter_factory=adapter_factory,
        lease_probe=lease_probe,
    )
    controller = RuntimeLifecycleController(
        identity,
        barrier=barrier,
        registry=registry,
        open_adapter=adapter_holder.open_adapter,
        close_adapter=adapter_holder.close_adapter,
        lease_is_open=adapter_holder.lease_is_open,
    )
    adapter_holder.bind_controller(controller)
    if os.name == "nt":
        assert owner_sid is not None
        control_server = WindowsRuntimeControlServer(
            identity=identity,
            registry=registry,
            controller=controller,
            owner_sid=owner_sid,
        )
    else:
        control_server = RuntimeControlServer(identity=identity, registry=registry, controller=controller)
    return _RuntimeSession(
        holder=adapter_holder,
        registry=registry,
        barrier=barrier,
        controller=controller,
        control_server=control_server,
    )


def _default_runtime_root(db_path: str) -> Path | None:
    """Return the private per-database runtime root for the current supported platform."""
    if os.name == "nt":
        _owner_sid, paths = _windows_owner_namespace()
        canonical = os.path.realpath(os.path.abspath(db_path))
        database_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return Path(paths.runtime_dir) / database_key
    if os.name != "posix" or not hasattr(socket, "SO_PEERCRED") or not os.path.isfile(db_path):
        return None
    uid = os.getuid()
    configured = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(configured) if configured else Path(f"/run/user/{uid}")
    try:
        info = base.stat()
    except OSError:
        return None
    if not base.is_dir() or info.st_uid != uid:
        return None
    canonical = os.path.realpath(os.path.abspath(db_path))
    database_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return base / "factlane" / database_key


def _default_barrier_root(db_path: str) -> Path:
    """Derive durable per-user maintenance state independently of the ephemeral runtime directory."""
    if os.name == "nt":
        _owner_sid, paths = _windows_owner_namespace()
        canonical = os.path.realpath(os.path.abspath(db_path))
        database_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        return Path(paths.state_dir) / "maintenance" / database_key
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    canonical = os.path.realpath(os.path.abspath(db_path))
    database_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return base / "factlane" / "maintenance" / database_key


def _safe_error_request_id(request: object) -> str | None:
    """Return only a bounded, non-sensitive caller correlation id for an error envelope."""
    if not isinstance(request, dict):
        return None
    value = request.get("request_id")
    if value is None:
        return None
    try:
        return validate_identifier(value, "request_id", required=True)  # type: ignore[arg-type]
    except AdapterError:
        return None


def _governed_error_envelope(
    gateway: MemoryGateway,
    exc: AdapterError,
    request: object,
) -> dict[str, Any]:
    """Project one governed adapter error through the public MCP response envelope."""
    envelope = exc.envelope(request_id=_safe_error_request_id(request))
    binding = gateway.require_transport(STDIO_TRANSPORT)
    envelope["audit"]["host_binding"] = binding.audit_projection()
    if gateway.context_binding is not None:
        envelope["audit"]["context_binding"] = gateway.context_binding.audit_projection()
    return envelope


async def _dispatch_public_tool(
    gateway: MemoryGateway,
    operation: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Preserve internal exception semantics while structuring governed public errors."""
    try:
        return await gateway.dispatch(operation, request)
    except AdapterError as exc:
        return _governed_error_envelope(gateway, exc, request)


class _BoundFastMCP(FastMCP):
    """FastMCP server restricted to the transport bound at construction."""

    def configure_runtime_session(self, session: _RuntimeSession) -> None:
        if hasattr(self, "_runtime_session"):
            raise RuntimeError("runtime session is already configured")
        self._runtime_session = session

    def run(self, transport: str = STDIO_TRANSPORT, mount_path: str | None = None) -> None:
        if transport != STDIO_TRANSPORT:
            raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "selected transport does not match its binding")
        super().run(transport, mount_path)

    async def run_stdio_async(self) -> None:
        session = getattr(self, "_runtime_session", None)
        if session is None:
            await super().run_stdio_async()
            return
        await session.start()
        try:
            await super().run_stdio_async()
        finally:
            await session.close()

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
        return await _dispatch_public_tool(gateway, "memory_search", request)

    @server.tool(name="memory_get", description=TOOL_DESCRIPTIONS["memory_get"])
    async def memory_get(request: MemoryGetRequest) -> dict[str, Any]:
        return await _dispatch_public_tool(gateway, "memory_get", request)

    @server.tool(name="memory_store", description=TOOL_DESCRIPTIONS["memory_store"])
    async def memory_store(request: MemoryStoreRequest) -> dict[str, Any]:
        return await _dispatch_public_tool(gateway, "memory_store", request)

    @server.tool(name="memory_update", description=TOOL_DESCRIPTIONS["memory_update"])
    async def memory_update(request: MemoryUpdateRequest) -> dict[str, Any]:
        return await _dispatch_public_tool(gateway, "memory_update", request)

    @server.tool(name="memory_status", description=TOOL_DESCRIPTIONS["memory_status"])
    async def memory_status(request: MemoryStatusRequest) -> dict[str, Any]:
        return await _dispatch_public_tool(gateway, "memory_status", request)

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
    parser.add_argument("--project-id", default=None, help="trusted exact current project identity, when available")
    parser.add_argument("--workflow-id", default=None, help="trusted exact current workflow identity, when available")
    parser.add_argument("--agent-id", default=None, help="trusted exact current tool/agent identity, when available")
    parser.add_argument("--project-id-source", default=None, help="trusted provenance label for project_id")
    parser.add_argument("--workflow-id-source", default=None, help="trusted provenance label for workflow_id")
    parser.add_argument("--agent-id-source", default=None, help="trusted provenance label for agent_id")
    parser.add_argument("--context-binding-source", default="explicit-launcher")
    parser.add_argument(
        "--write-profile",
        choices=("read-only", "delegated-candidate", "owner-current", "repo-verifier", "automated-verifier"),
        default="read-only",
        help="trusted launcher write profile; omission is fail-closed read-only, while writing agents require an explicit grant",
    )
    parser.add_argument("--help-tools", action="store_true", help="print the complete five-tool request reference")
    parser.add_argument("--help-tool", choices=sorted(TOOL_DESCRIPTIONS), help="print one tool's request reference")
    args = parser.parse_args(raw_argv)
    try:
        binding = HostBinding(args.host_id, STDIO_TRANSPORT, args.binding_source)
        context_binding = None
        if any(value is not None for value in (args.project_id, args.workflow_id, args.agent_id)):
            context_binding = TrustedContextBinding(
                project_id=args.project_id,
                workflow_id=args.workflow_id,
                agent_id=args.agent_id,
                project_id_source=args.project_id_source,
                workflow_id_source=args.workflow_id_source,
                agent_id_source=args.agent_id_source,
                binding_source=args.context_binding_source,
            )
    except AdapterError as exc:
        parser.error(exc.safe_message)
    launch_config = _AdapterLaunchConfig(
        db_path=args.db,
        profile_name=args.profile,
        ollama_url=args.ollama_url,
        tokenizer_path=args.tokenizer_path,
        trusted_write_context=trusted_write_context_for_profile(
            args.write_profile,
            contributor_ref=args.host_id,
            grant_ref=f"launcher:{args.write_profile}:{args.host_id}",
        ),
    )
    holder = _ReopenableAdapterHolder(
        launch_config,
        adapter_factory=MemoryAdapter.create,
    )
    gateway = MemoryGateway(
        holder,
        binding,
        transport_kind=STDIO_TRANSPORT,
        context_binding=context_binding,
    )
    server = build_mcp_server(gateway)
    runtime_root = _default_runtime_root(args.db) if isinstance(server, _BoundFastMCP) else None
    if runtime_root is not None:
        session = _build_runtime_session(
            launch_config,
            runtime_root=runtime_root,
            barrier_root=_default_barrier_root(args.db),
            adapter_factory=MemoryAdapter.create,
            holder=holder,
        )
        server.configure_runtime_session(session)
        server.run(STDIO_TRANSPORT)
        return

    # Unsupported/new-database topology retains the previous compatible stdio
    # behavior. It is intentionally unregistered, so autonomous maintenance must
    # treat this live connection as an exclusion blocker until a later relaunch.
    asyncio.run(holder.open_adapter(0))
    try:
        server.run(STDIO_TRANSPORT)
    finally:
        asyncio.run(holder.close_adapter())


if __name__ == "__main__":
    main()
