from __future__ import annotations

import asyncio
import os
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from factlane.adapter import trusted_write_context_for_profile
from factlane.contract import AdapterError
from factlane.gateway import HostBinding, MemoryGateway, TrustedContextBinding
from factlane.runtime_control import PROTOCOL_VERSION, RuntimeControlClient, RuntimeControlRequest
from factlane.server import (
    STDIO_TRANSPORT,
    _AdapterLaunchConfig,
    _BoundFastMCP,
    _build_runtime_session,
    build_mcp_server,
)


class _FakeAdapter:
    def __init__(self, marker: int) -> None:
        self.marker = marker
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        if self.closed:
            raise AssertionError("closed adapter must never receive memory dispatch")
        self.calls.append((operation, dict(request)))
        return {
            "status": "OK",
            "results": [],
            "degradation": None,
            "audit": {"request_id": request.get("request_id"), "raw_content_logged": False},
        }

    async def close(self) -> None:
        self.closed = True


def _request(identity, *, command: str, operation_id: str, generation: int, drain_deadline: float | None = None):
    payload: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": str(uuid.uuid4()),
        "operation_id": operation_id,
        "instance_id": identity.instance_id,
        "expected_generation": generation,
        "command": command,
    }
    if drain_deadline is not None:
        payload["drain_deadline"] = drain_deadline
    return RuntimeControlRequest.from_mapping(payload)


def _call_status(server: FastMCP) -> dict[str, Any]:
    result = asyncio.run(server.call_tool("memory_status", {"request": {"scope": "GLOBAL_USER"}}))
    assert isinstance(result, tuple)
    _, structured = result
    assert isinstance(structured, dict)
    return structured


def test_runtime_managed_stdio_registers_before_open_and_reopens_original_launch_config(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "memory.db"
        db_path.touch()
        runtime_root = tmp_path / "runtime"
        created: list[_FakeAdapter] = []
        factory_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        registered_states: list[str] = []
        session_box: dict[str, Any] = {}

        async def adapter_factory(*args: Any, **kwargs: Any) -> _FakeAdapter:
            session = session_box["session"]
            registered = session.registry.load(session.identity.instance_id)
            registered_states.append(registered.lifecycle_state)
            factory_calls.append((args, dict(kwargs)))
            adapter = _FakeAdapter(len(created) + 1)
            created.append(adapter)
            return adapter

        config = _AdapterLaunchConfig(
            db_path=str(db_path),
            profile_name="nomic-768",
            ollama_url="http://127.0.0.1:11434",
            tokenizer_path=str(tmp_path / "tokenizer.json"),
            trusted_write_context=trusted_write_context_for_profile(
                "read-only",
                contributor_ref="runtime-host",
                grant_ref="launcher:read-only:runtime-host",
            ),
        )
        binding = HostBinding("runtime-host", STDIO_TRANSPORT, "integration-test")
        context = TrustedContextBinding(project_id="project-a", binding_source="integration-test")
        session = _build_runtime_session(
            config,
            runtime_root=runtime_root,
            barrier_root=tmp_path / "state",
            adapter_factory=adapter_factory,
            lease_probe=lambda adapter: not adapter.closed,
        )
        session_box["session"] = session
        gateway = MemoryGateway(
            session.holder,
            binding,
            transport_kind=STDIO_TRANSPORT,
            context_binding=context,
        )
        server = build_mcp_server(gateway)
        assert isinstance(server, _BoundFastMCP)
        assert sorted(server._tool_manager._tools) == [  # type: ignore[attr-defined]
            "memory_get",
            "memory_search",
            "memory_status",
            "memory_store",
            "memory_update",
        ]

        await session.start()
        try:
            assert session.barrier.state_root == (tmp_path / "state").absolute()
            assert stat.S_IMODE(runtime_root.stat().st_mode) == 0o700
            socket_path = Path(session.identity.control_socket_path)
            assert socket_path.exists()
            assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
            assert session.controller.admits_normal_requests is True
            assert len(created) == 1

            first = await gateway.dispatch("memory_status", {"scope": "GLOBAL_USER"})
            assert first["status"] == "OK"
            assert created[0].calls == [("memory_status", {"scope": "GLOBAL_USER"})]

            client = RuntimeControlClient(session.registry)
            operation_1 = str(uuid.uuid4())
            session.barrier.begin_prepare(operation_1, expected_generation=1)
            closed = await client.call(
                session.controller.identity,
                _request(
                    session.controller.identity,
                    command="PREPARE_CLOSE",
                    operation_id=operation_1,
                    generation=1,
                    drain_deadline=time.time() + 30,
                ),
            )
            assert closed["state"] == "CLOSED"
            assert created[0].closed is True

            blocked = await server.call_tool("memory_status", {"request": {"scope": "GLOBAL_USER"}})
            _, structured = blocked
            assert structured["status"] == "BLOCKED"
            assert structured["error_code"] == "MAINTENANCE_IN_PROGRESS"
            assert created[0].calls == [("memory_status", {"scope": "GLOBAL_USER"})]

            session.barrier.publish_abort(operation_1)
            aborted = await client.call(
                session.controller.identity,
                _request(
                    session.controller.identity,
                    command="ABORT",
                    operation_id=operation_1,
                    generation=1,
                ),
            )
            assert aborted["state"] == "OPEN"
            assert len(created) == 2
            session.barrier.publish_running(operation_1, generation=1)

            operation_2 = str(uuid.uuid4())
            session.barrier.begin_prepare(operation_2, expected_generation=1)
            await client.call(
                session.controller.identity,
                _request(
                    session.controller.identity,
                    command="PREPARE_CLOSE",
                    operation_id=operation_2,
                    generation=1,
                    drain_deadline=time.time() + 30,
                ),
            )
            session.barrier.begin_activation(operation_2, generation=2)
            resumed = await client.call(
                session.controller.identity,
                _request(
                    session.controller.identity,
                    command="RESUME",
                    operation_id=operation_2,
                    generation=2,
                ),
            )
            assert resumed["state"] == "READY"
            assert resumed["actual_generation"] == 2
            assert len(created) == 3
            activating_blocked = await server.call_tool(
                "memory_status",
                {"request": {"scope": "GLOBAL_USER"}},
            )
            _, activating_structured = activating_blocked
            assert activating_structured["error_code"] == "MAINTENANCE_IN_PROGRESS"
            assert created[2].calls == []
            assert [call[0] for call in factory_calls] == [
                (str(db_path), "nomic-768"),
                (str(db_path), "nomic-768"),
                (str(db_path), "nomic-768"),
            ]
            assert all(call[1]["ollama_url"] == config.ollama_url for call in factory_calls)
            assert all(call[1]["tokenizer_path"] == config.tokenizer_path for call in factory_calls)
            assert all(call[1]["trusted_write_context"] is config.trusted_write_context for call in factory_calls)
            assert registered_states == ["OPENING", "REOPENING", "REOPENING"]

            session.barrier.publish_running(operation_2, generation=2)

            rebound = await gateway.dispatch("memory_status", {"scope": "PROJECT"})
            assert rebound["status"] == "OK"
            assert created[2].calls[-1] == ("memory_status", {"scope": "PROJECT", "project_id": "project-a"})

            operation_3 = str(uuid.uuid4())
            session.barrier.begin_prepare(operation_3, expected_generation=2)
            await client.call(
                session.controller.identity,
                _request(
                    session.controller.identity,
                    command="PREPARE_CLOSE",
                    operation_id=operation_3,
                    generation=2,
                    drain_deadline=time.time() + 30,
                ),
            )
            session.barrier.begin_activation(operation_3, generation=3)
            await client.call(
                session.controller.identity,
                _request(
                    session.controller.identity,
                    command="RESUME",
                    operation_id=operation_3,
                    generation=3,
                ),
            )
            session.barrier.mark_recovery_required(operation_3)
            recovery_blocked = await server.call_tool(
                "memory_status",
                {"request": {"scope": "GLOBAL_USER"}},
            )
            _, recovery_structured = recovery_blocked
            assert recovery_structured["error_code"] == "MAINTENANCE_IN_PROGRESS"
        finally:
            await session.close()

        assert not Path(session.identity.control_socket_path).exists()

    asyncio.run(run())


def test_parked_startup_registers_without_opening_db_and_public_tool_is_governed_block(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "memory.db"
        db_path.touch()
        runtime_root = tmp_path / "runtime"
        create_calls = 0

        async def adapter_factory(*args: Any, **kwargs: Any) -> _FakeAdapter:
            nonlocal create_calls
            create_calls += 1
            return _FakeAdapter(create_calls)

        config = _AdapterLaunchConfig(
            db_path=str(db_path),
            profile_name="nomic-768",
            ollama_url="http://127.0.0.1:11434",
            tokenizer_path=None,
            trusted_write_context=trusted_write_context_for_profile(
                "read-only",
                contributor_ref="runtime-host",
                grant_ref="launcher:read-only:runtime-host",
            ),
        )
        session = _build_runtime_session(
            config,
            runtime_root=runtime_root,
            adapter_factory=adapter_factory,
            lease_probe=lambda adapter: not adapter.closed,
        )
        operation_id = str(uuid.uuid4())
        session.barrier.begin_prepare(operation_id, expected_generation=1)
        gateway = MemoryGateway(
            session.holder,
            HostBinding("runtime-host", STDIO_TRANSPORT, "integration-test"),
            transport_kind=STDIO_TRANSPORT,
        )
        server = build_mcp_server(gateway)

        await session.start()
        try:
            registered = session.registry.load(session.identity.instance_id)
            assert registered.lifecycle_state == "PARKED"
            assert create_calls == 0
            result = await server.call_tool("memory_status", {"request": {"scope": "GLOBAL_USER"}})
            _, structured = result
            assert structured["error_code"] == "MAINTENANCE_IN_PROGRESS"
            assert create_calls == 0
        finally:
            await session.close()

    asyncio.run(run())


def test_prepare_close_drains_inflight_and_rejects_new_public_dispatch_while_stdio_stays_live(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "memory.db"
        db_path.touch()
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingAdapter(_FakeAdapter):
            async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
                self.calls.append((operation, dict(request)))
                started.set()
                await release.wait()
                return {
                    "status": "OK",
                    "results": [],
                    "degradation": None,
                    "audit": {"request_id": request.get("request_id"), "raw_content_logged": False},
                }

        adapter_box: list[BlockingAdapter] = []

        async def adapter_factory(*args: Any, **kwargs: Any) -> BlockingAdapter:
            adapter = BlockingAdapter(1)
            adapter_box.append(adapter)
            return adapter

        config = _AdapterLaunchConfig(
            db_path=str(db_path),
            profile_name="nomic-768",
            ollama_url="http://127.0.0.1:11434",
            tokenizer_path=None,
            trusted_write_context=trusted_write_context_for_profile(
                "read-only",
                contributor_ref="runtime-host",
                grant_ref="launcher:read-only:runtime-host",
            ),
        )
        session = _build_runtime_session(
            config,
            runtime_root=tmp_path / "runtime",
            adapter_factory=adapter_factory,
            lease_probe=lambda adapter: not adapter.closed,
        )
        gateway = MemoryGateway(
            session.holder,
            HostBinding("runtime-host", STDIO_TRANSPORT, "integration-test"),
            transport_kind=STDIO_TRANSPORT,
        )
        server = build_mcp_server(gateway)
        await session.start()
        try:
            inflight = asyncio.create_task(gateway.dispatch("memory_status", {"scope": "GLOBAL_USER"}))
            await started.wait()

            operation_id = str(uuid.uuid4())
            session.barrier.begin_prepare(operation_id, expected_generation=1)
            client = RuntimeControlClient(session.registry)
            closing = asyncio.create_task(
                client.call(
                    session.controller.identity,
                    _request(
                        session.controller.identity,
                        command="PREPARE_CLOSE",
                        operation_id=operation_id,
                        generation=1,
                        drain_deadline=time.time() + 30,
                    ),
                )
            )
            for _ in range(100):
                if session.controller.identity.lifecycle_state == "PREPARING":
                    break
                await asyncio.sleep(0)
            assert session.controller.identity.lifecycle_state == "PREPARING"
            assert closing.done() is False

            blocked = await server.call_tool("memory_status", {"request": {"scope": "GLOBAL_USER"}})
            _, structured = blocked
            assert structured["error_code"] == "MAINTENANCE_IN_PROGRESS"
            assert len(adapter_box[0].calls) == 1

            release.set()
            assert (await inflight)["status"] == "OK"
            closed = await closing
            assert closed["state"] == "CLOSED"
            assert adapter_box[0].closed is True
        finally:
            await session.close()

    asyncio.run(run())


def test_bound_fastmcp_runtime_session_wraps_real_stdio_async_lifetime(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "memory.db"
    db_path.touch()
    runtime_root = tmp_path / "runtime"
    active: list[str] = []

    async def adapter_factory(*args: Any, **kwargs: Any) -> _FakeAdapter:
        active.append("open")
        return _FakeAdapter(len(active))

    config = _AdapterLaunchConfig(
        db_path=str(db_path),
        profile_name="nomic-768",
        ollama_url="http://127.0.0.1:11434",
        tokenizer_path=None,
        trusted_write_context=trusted_write_context_for_profile(
            "read-only",
            contributor_ref="runtime-host",
            grant_ref="launcher:read-only:runtime-host",
        ),
    )
    # Construct synchronously, exactly as main() does before FastMCP creates its
    # async stdio loop. Async locks/conditions must bind lazily to that loop.
    session = _build_runtime_session(
        config,
        runtime_root=runtime_root,
        barrier_root=tmp_path / "state",
        adapter_factory=adapter_factory,
        lease_probe=lambda adapter: not adapter.closed,
    )
    gateway = MemoryGateway(
        session.holder,
        HostBinding("runtime-host", STDIO_TRANSPORT, "integration-test"),
        transport_kind=STDIO_TRANSPORT,
    )
    server = build_mcp_server(gateway)
    assert isinstance(server, _BoundFastMCP)
    server.configure_runtime_session(session)

    async def fake_stdio(self) -> None:
        assert session.controller.admits_normal_requests is True
        assert Path(session.identity.control_socket_path).exists()
        active.append("stdio")

    monkeypatch.setattr(FastMCP, "run_stdio_async", fake_stdio)
    asyncio.run(server.run_stdio_async())

    assert active == ["open", "stdio"]
    assert not Path(session.identity.control_socket_path).exists()


def test_runtime_session_rejects_database_inode_drift_against_durable_barrier(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    db_path.write_bytes(b"first")
    barrier_root = tmp_path / "state"
    config = _AdapterLaunchConfig(
        db_path=str(db_path),
        profile_name="nomic-768",
        ollama_url="http://127.0.0.1:11434",
        tokenizer_path=None,
        trusted_write_context=trusted_write_context_for_profile(
            "read-only",
            contributor_ref="runtime-host",
            grant_ref="launcher:read-only:runtime-host",
        ),
    )
    _build_runtime_session(
        config,
        runtime_root=tmp_path / "runtime-a",
        barrier_root=barrier_root,
        adapter_factory=lambda *args, **kwargs: None,  # type: ignore[arg-type]
    )
    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"second")
    os.replace(replacement, db_path)

    try:
        _build_runtime_session(
            config,
            runtime_root=tmp_path / "runtime-b",
            barrier_root=barrier_root,
            adapter_factory=lambda *args, **kwargs: None,  # type: ignore[arg-type]
        )
    except AdapterError as exc:
        assert exc.code == "DATABASE_IDENTITY_MISMATCH"
    else:
        raise AssertionError("runtime startup must reject a DB inode that drifted outside the barrier protocol")


def test_runtime_start_retries_transient_barrier_busy_then_opens(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    db_path.touch()
    adapters: list[_FakeAdapter] = []

    async def adapter_factory(*args: Any, **kwargs: Any) -> _FakeAdapter:
        adapter = _FakeAdapter(1)
        adapters.append(adapter)
        return adapter

    config = _AdapterLaunchConfig(
        db_path=str(db_path),
        profile_name="nomic-768",
        ollama_url="http://127.0.0.1:11434",
        tokenizer_path=None,
        trusted_write_context=trusted_write_context_for_profile(
            "read-only",
            contributor_ref="runtime-host",
            grant_ref="launcher:read-only:runtime-host",
        ),
    )
    session = _build_runtime_session(
        config,
        runtime_root=tmp_path / "runtime",
        barrier_root=tmp_path / "state",
        adapter_factory=adapter_factory,
        lease_probe=lambda adapter: not adapter.closed,
    )
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_barrier() -> None:
        with session.barrier.guard():
            lock_acquired.set()
            release_lock.wait(timeout=2)

    thread = threading.Thread(target=hold_barrier, daemon=True)
    thread.start()
    assert lock_acquired.wait(timeout=1)

    async def run() -> None:
        starting = asyncio.create_task(session.start())
        await asyncio.sleep(0.03)
        assert starting.done() is False
        release_lock.set()
        await starting
        try:
            assert session.controller.identity.lifecycle_state == "OPEN"
            assert len(adapters) == 1
        finally:
            await session.close()

    asyncio.run(run())
    thread.join(timeout=1)
