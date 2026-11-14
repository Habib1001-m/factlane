from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows server runtime qualification only")

from factlane.adapter import trusted_write_context_for_profile
from factlane.contract import AdapterError
from factlane.runtime_control import (
    PROTOCOL_VERSION,
    RuntimeControlRequest,
    WindowsRuntimeControlClient,
    windows_runtime_pipe_name,
)
from factlane.server import (
    _AdapterLaunchConfig,
    _build_runtime_session,
    _default_barrier_root,
    _default_runtime_root,
)
from factlane.storage import _acquire_runtime_maintenance_lease, _release_runtime_maintenance_lease
from factlane.webui_windows import windows_boot_id
from factlane.windows_maintenance import (
    WindowsInPlaceMaintenanceLease,
    WindowsMaintenanceError,
    WindowsRuntimeDBLease,
)
from factlane.windows_security import current_user_sid, process_identity, validate_owner_private_path


class _LeaseAdapter:
    def __init__(self, db_path: str) -> None:
        lease = _acquire_runtime_maintenance_lease(db_path)
        self.engine = SimpleNamespace(
            _closed=False,
            _maintenance_lease_handle=lease,
            conn=object(),
            storage=object(),
        )

    async def dispatch(self, _operation: str, _request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "OK", "results": [], "degradation": None, "audit": {}}

    async def close(self) -> None:
        if self.engine._closed:
            return
        _release_runtime_maintenance_lease(self.engine._maintenance_lease_handle)
        self.engine._maintenance_lease_handle = None
        self.engine.conn = None
        self.engine.storage = None
        self.engine._closed = True


def _initialize_runtime_db(db: Path) -> None:
    connection = sqlite3.connect(db)
    try:
        journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        assert journal is not None and str(journal[0]).casefold() == "wal"
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
    finally:
        connection.close()


def _config(db: Path) -> _AdapterLaunchConfig:
    return _AdapterLaunchConfig(
        db_path=str(db),
        profile_name="nomic-768",
        ollama_url="http://127.0.0.1:11434",
        tokenizer_path=None,
        trusted_write_context=trusted_write_context_for_profile(
            "read-only",
            contributor_ref="windows-runtime-test",
            grant_ref="launcher:read-only:windows-runtime-test",
        ),
    )


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


def _session(db: Path):
    runtime_root = _default_runtime_root(str(db))
    assert runtime_root is not None
    barrier_root = _default_barrier_root(str(db))

    async def factory(db_path: str, *_args: Any, **_kwargs: Any) -> _LeaseAdapter:
        return _LeaseAdapter(db_path)

    return _build_runtime_session(
        _config(db),
        runtime_root=runtime_root,
        barrier_root=barrier_root,
        adapter_factory=factory,
    )


def test_native_windows_session_registers_exact_pipe_and_round_trips_close_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    db = tmp_path / "memory.db"
    _initialize_runtime_db(db)

    async def run() -> None:
        session = _session(db)
        await session.start()
        try:
            sid = current_user_sid()
            process = process_identity(os.getpid())
            registered = session.registry.load(session.identity.instance_id)
            assert registered == session.identity
            assert registered.control_socket_path == windows_runtime_pipe_name(registered.instance_id)
            assert registered.process_start_ticks == process.creation_time_us
            assert registered.boot_id == windows_boot_id()
            assert registered.owner_uid == 0
            assert registered.lifecycle_state == "OPEN"

            local_appdata = (tmp_path / "LocalAppData").resolve()
            runtime_namespace = local_appdata / "FactLane" / "runtime"
            session.registry.runtime_root.relative_to(runtime_namespace)
            assert "wsl" not in str(session.registry.runtime_root).casefold()
            assert "/mnt/" not in str(session.registry.runtime_root).replace("\\", "/").casefold()
            validate_owner_private_path(session.registry.runtime_root, owner_sid=sid, directory=True)
            validate_owner_private_path(session.registry.registry_root, owner_sid=sid, directory=True)
            validate_owner_private_path(
                session.registry.registry_root / f"{registered.instance_id}.json",
                owner_sid=sid,
                directory=False,
            )

            client = WindowsRuntimeControlClient(session.registry, owner_sid=sid)
            operation_id = str(uuid.uuid4())
            session.barrier.begin_prepare(operation_id, expected_generation=1)
            closed = await client.call(
                session.identity,
                _request(
                    session.identity,
                    command="PREPARE_CLOSE",
                    operation_id=operation_id,
                    generation=1,
                    drain_deadline=time.time() + 30,
                ),
            )
            assert closed["status_code"] == "OK"
            assert closed["state"] == "CLOSED"
            assert closed["lease_open"] is False

            exclusive = WindowsInPlaceMaintenanceLease.acquire(str(db))
            try:
                with pytest.raises(AdapterError) as blocked:
                    _acquire_runtime_maintenance_lease(str(db))
                assert blocked.value.code == "MAINTENANCE_IN_PROGRESS"
            finally:
                exclusive.release_unmutated()

            session.barrier.begin_activation(operation_id, generation=2)
            resumed = await client.call(
                session.identity,
                _request(
                    session.identity,
                    command="RESUME",
                    operation_id=operation_id,
                    generation=2,
                ),
            )
            assert resumed["status_code"] == "OK"
            assert resumed["state"] == "READY"
            assert resumed["lease_open"] is True
            assert resumed["actual_generation"] == 2
            session.barrier.publish_running(operation_id, generation=2)
        finally:
            await session.close()

        exclusive = WindowsInPlaceMaintenanceLease.acquire(str(db))
        exclusive.release_unmutated()

    asyncio.run(run())


def test_multiple_native_windows_runtime_sessions_share_lease_and_exclusive_waits_for_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    db = tmp_path / "memory.db"
    _initialize_runtime_db(db)

    async def run() -> None:
        first = _session(db)
        second = _session(db)
        await first.start()
        await second.start()
        try:
            assert first.holder.lease_is_open() is True
            assert second.holder.lease_is_open() is True
            assert first.identity.instance_id != second.identity.instance_id
            assert first.identity.control_socket_path != second.identity.control_socket_path
            with pytest.raises(Exception) as blocked:
                WindowsInPlaceMaintenanceLease.acquire(str(db))
            assert getattr(blocked.value, "code", None) == "MAINTENANCE_IN_PROGRESS"
        finally:
            await second.close()
            await first.close()

        exclusive = WindowsInPlaceMaintenanceLease.acquire(str(db))
        exclusive.release_unmutated()

    asyncio.run(run())


def test_windows_storage_lease_maps_unavailable_kernel_to_backend_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "memory.db"
    db.touch()

    def unavailable(_path: str):
        raise WindowsMaintenanceError(
            "WINDOWS_MAINTENANCE_UNAVAILABLE",
            "synthetic unavailable Windows maintenance kernel",
        )

    monkeypatch.setattr(WindowsRuntimeDBLease, "open", staticmethod(unavailable))
    with pytest.raises(AdapterError) as caught:
        _acquire_runtime_maintenance_lease(str(db))
    assert caught.value.code == "BACKEND_UNAVAILABLE"
    assert "maintenance coordination" in caught.value.safe_message


def test_native_windows_session_rejects_wsl_database_paths_before_runtime_state_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    runtime_root = _default_runtime_root(str(tmp_path / "placeholder.db"))
    assert runtime_root is not None
    barrier_root = _default_barrier_root(str(tmp_path / "placeholder.db"))
    before = set(runtime_root.parent.iterdir()) if runtime_root.parent.exists() else set()
    config = _config(Path(r"\\wsl.localhost\Ubuntu-22.04\home\habib\memory.db"))

    with pytest.raises(AdapterError) as caught:
        _build_runtime_session(
            config,
            runtime_root=runtime_root,
            barrier_root=barrier_root,
            adapter_factory=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        )
    assert caught.value.code == "BACKEND_UNAVAILABLE"
    after = set(runtime_root.parent.iterdir()) if runtime_root.parent.exists() else set()
    assert after == before
