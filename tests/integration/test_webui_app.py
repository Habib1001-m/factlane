from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
    pytest.skip(
        "managed Owner WebUI backend qualification requires Linux/POSIX service primitives",
        allow_module_level=True,
    )

import fcntl

from factlane.adapter import MemoryAdapter
from factlane.contract import AdapterError, ScopeContext
from factlane.owner_database import build_logical_export
from factlane.owner_database_service import OwnerDatabaseService
from factlane.owner_destructive import OwnerDestructiveOrchestrator, PurgeTargetIdentity
from factlane.maintenance_coordinator import MaintenanceCoordinatorError, PreparedMaintenance, ResumeReport
from factlane.owner_operations import OwnerOperationsService
from factlane.owner_overview import OwnerOverviewService
from factlane.owner_control import OwnerControlService
from factlane.owner_restore import FullRestoreService
from factlane.recovery import MaintenanceLease, RecoveryScope, RecoveryTarget
from factlane.runtime_control import PROTOCOL_VERSION, RuntimeControlRequest, RuntimeIdentity
from factlane.storage import SQLiteVecEngine, STORAGE_CONTRACT_VERSION
from factlane.webui_app import (
    _OwnerAdapterMaintenanceCoordinator,
    _configured_embedding_profile,
    _database_state_roots,
    _make_post_activation_verifier,
    create_caddy_process,
    create_owner_runtime_backend,
)
from factlane.webui_runtime import BackendLaunchSpec, BackendMode, WebUIRuntimeConfig, caddy_command
from factlane.webui_service import WebUIServiceState


ORIGIN = "https://factlane-compose.test:8765"


class _Confirmation:
    def __init__(self, approved: bool = False) -> None:
        self.approved = approved

    def confirm(self, _request) -> bool:
        return self.approved


class _Engine:
    def __init__(self, events: list[tuple[str, int, int]]) -> None:
        self.events = events

    async def close(self) -> None:
        self.events.append(("close", id(asyncio.get_running_loop()), threading.get_ident()))


class _LeaseEngine:
    def __init__(self, db_path: str, events: list[tuple[str, int, int]]) -> None:
        self.db_path = db_path
        self.events = events
        self.closed = False
        self.status_entered: asyncio.Event | None = None
        self.status_release: asyncio.Event | None = None
        Path(db_path).touch(exist_ok=True)
        self._lease = open(db_path, "rb")
        fcntl.flock(self._lease.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)

    async def status(self, _scope) -> dict[str, object]:
        if self.closed:
            raise AdapterError("BACKEND_UNAVAILABLE", "test lease engine is closed")
        if self.status_entered is not None:
            self.status_entered.set()
        if self.status_release is not None:
            await self.status_release.wait()
        return {"state": "OPEN"}

    async def close(self) -> None:
        if self.closed:
            return
        fcntl.flock(self._lease.fileno(), fcntl.LOCK_UN)
        self._lease.close()
        self.closed = True
        self.events.append(("close", id(asyncio.get_running_loop()), threading.get_ident()))


class _EmptyRegistry:
    def registrations(self, **_kwargs):
        return ()


class _ExclusiveProbeCoordinator:
    def __init__(self, db_path: str) -> None:
        self.registry = _EmptyRegistry()
        self.barrier = SimpleNamespace(database_path=os.path.realpath(db_path), read=self._read)
        self.state = "RUNNING"
        self.generation = 1
        self.exclusion_proven = 0

    def _read(self):
        stat = os.stat(self.barrier.database_path)
        return SimpleNamespace(
            state=self.state,
            generation=self.generation,
            operation_id=None,
            device=stat.st_dev,
            inode=stat.st_ino,
        )

    async def prepare_close(self, operation_id: str, *, drain_deadline: float):
        assert drain_deadline > time.time()
        with open(self.barrier.database_path, "rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.exclusion_proven += 1
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.state = "PREPARING"
        return PreparedMaintenance(
            operation_id=operation_id,
            original_generation=self.generation,
            intent_revision=1,
            initially_open_instance_ids=(),
            registrations=(),
        )

    async def abort_pre_mutation(self, _prepared) -> None:
        self.state = "RUNNING"

    def begin_activation(self, prepared, *, new_generation: int):
        assert self.state == "PREPARING"
        assert new_generation == prepared.original_generation + 1
        self.state = "ACTIVATING"
        self.generation = new_generation
        stat = os.stat(self.barrier.database_path)
        return SimpleNamespace(
            state=self.state,
            generation=self.generation,
            device=stat.st_dev,
            inode=stat.st_ino,
        )

    def publish_running(self, _prepared, *, new_generation: int):
        assert self.state == "ACTIVATING"
        assert new_generation == self.generation
        self.state = "RUNNING"
        return self._read()

    async def resume_running(self, _prepared, *, new_generation: int):
        assert self.state == "RUNNING"
        assert new_generation == self.generation
        return ResumeReport(("runtime-a",), {})

    async def resume_activating(self, _prepared, *, new_generation: int):
        assert self.state == "ACTIVATING"
        assert new_generation == self.generation
        return ResumeReport(("runtime-a",), {})

    def rearm_recovery(self, _prepared):
        self.state = "PREPARING"
        return self._read()

    def mark_recovery_required(self, _operation_id: str):
        self.state = "RECOVERY_REQUIRED"
        return self._read()

    def activation_state(self, _operation_id: str, *, new_generation: int, device=None, inode=None):
        assert new_generation == self.generation
        snapshot = self._read()
        if device is not None:
            assert snapshot.device == device
        if inode is not None:
            assert snapshot.inode == inode
        return snapshot


class _ResumeDelegate:
    def __init__(self, report: ResumeReport) -> None:
        self.report = report
        self.barrier = object()
        self.registry = object()
        self.activation_calls: list[tuple[str, int]] = []

    async def resume_running(self, _prepared, *, new_generation: int) -> ResumeReport:
        self.activation_calls.append(("resume_running", new_generation))
        return self.report

    def begin_activation(self, _prepared, *, new_generation: int):
        self.activation_calls.append(("begin_activation", new_generation))
        return object()

    def publish_running(self, _prepared, *, new_generation: int):
        self.activation_calls.append(("publish_running", new_generation))
        return object()


class _FailingOwnerParticipant:
    def __init__(self) -> None:
        self.reopen_calls = 0
        self.recovery_required_calls = 0

    async def reopen(self):
        self.reopen_calls += 1
        raise RuntimeError("owner adapter reopen failed")

    def mark_recovery_required(self) -> None:
        self.recovery_required_calls += 1


class _HangingOwnerParticipant:
    def __init__(self) -> None:
        self.reopen_calls = 0
        self.recovery_required_calls = 0
        self.reopen_cancelled = False

    async def reopen(self):
        self.reopen_calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.reopen_cancelled = True
            raise

    def mark_recovery_required(self) -> None:
        self.recovery_required_calls += 1


class _AbortDelegate:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.barrier = _PrepareBarrier("PREPARING")
        self.registry = object()
        self.abort_calls = 0
        self.recover_calls = 0

    async def abort_pre_mutation(self, _prepared) -> None:
        self.abort_calls += 1
        if self.error is not None:
            raise self.error
        self.barrier.state = "RUNNING"

    async def recover_aborted_pre_mutation(self, _prepared):
        self.recover_calls += 1
        if self.error is not None:
            raise self.error
        assert self.barrier.state == "RUNNING"
        return SimpleNamespace(state="RUNNING")


class _PrepareBarrier:
    def __init__(self, state: str) -> None:
        self.state = state

    def read(self):
        return SimpleNamespace(state=self.state)


class _PrepareDelegate:
    def __init__(self, error: MaintenanceCoordinatorError, *, barrier_state: str = "RUNNING") -> None:
        self.error = error
        self.barrier = _PrepareBarrier(barrier_state)
        self.registry = object()
        self.prepare_calls = 0

    async def prepare_close(self, _operation_id: str, *, drain_deadline: float):
        assert drain_deadline > time.time()
        self.prepare_calls += 1
        raise self.error


class _PrepareParticipant:
    def __init__(
        self,
        *,
        state: str = "RUNNING",
        prepare_error: BaseException | None = None,
        reopen_error: BaseException | None = None,
    ) -> None:
        self.state = state
        self.prepare_error = prepare_error
        self.reopen_error = reopen_error
        self.prepare_calls = 0
        self.reopen_calls = 0
        self.recovery_required_calls = 0

    async def prepare_close(self, *, drain_deadline: float) -> None:
        assert drain_deadline > time.time()
        self.prepare_calls += 1
        if self.prepare_error is not None:
            self.state = "RECOVERY_REQUIRED"
            raise self.prepare_error
        self.state = "CLOSED"

    async def reopen(self):
        self.reopen_calls += 1
        if self.reopen_error is not None:
            self.state = "RECOVERY_REQUIRED"
            raise self.reopen_error
        self.state = "RUNNING"
        return object()

    def mark_recovery_required(self) -> None:
        self.recovery_required_calls += 1
        self.state = "RECOVERY_REQUIRED"


@pytest.mark.parametrize(
    "original",
    [
        MaintenanceCoordinatorError("CLIENT_DRAIN_TIMEOUT", "owner adapter close timed out"),
        RuntimeError("owner adapter close failed"),
    ],
)
def test_owner_prepare_close_failure_in_recovery_state_raises_typed_recovery_error(
    original: BaseException,
) -> None:
    async def run() -> None:
        delegate = _PrepareDelegate(MaintenanceCoordinatorError("UNUSED", "delegate must not run"))
        participant = _PrepareParticipant(prepare_error=original)
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]

        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.prepare_close("operation-a", drain_deadline=time.time() + 1.0)

        assert exc_info.value.code == "MAINTENANCE_RECOVERY_REQUIRED"
        assert exc_info.value.__cause__ is original
        assert participant.state == "RECOVERY_REQUIRED"
        assert delegate.prepare_calls == 0

    asyncio.run(run())


def test_delegate_prepare_failure_plus_owner_reopen_failure_raises_typed_recovery_error() -> None:
    async def run() -> None:
        original = MaintenanceCoordinatorError("CLIENT_CLOSE_FAILED", "stdio close failed")
        delegate = _PrepareDelegate(original, barrier_state="RUNNING")
        participant = _PrepareParticipant(reopen_error=RuntimeError("owner reopen failed"))
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]

        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.prepare_close("operation-b", drain_deadline=time.time() + 1.0)

        assert exc_info.value.code == "MAINTENANCE_RECOVERY_REQUIRED"
        assert exc_info.value.__cause__ is original
        assert delegate.prepare_calls == 1
        assert participant.reopen_calls == 1
        assert participant.state == "RECOVERY_REQUIRED"

    asyncio.run(run())


def test_delegate_prepare_failure_clean_reconciliation_preserves_original_error() -> None:
    async def run() -> None:
        original = MaintenanceCoordinatorError("CLIENT_INSPECTION_FAILED", "stdio inspection failed")
        delegate = _PrepareDelegate(original, barrier_state="RUNNING")
        participant = _PrepareParticipant()
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]

        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.prepare_close("operation-c", drain_deadline=time.time() + 1.0)

        assert exc_info.value is original
        assert exc_info.value.code == "CLIENT_INSPECTION_FAILED"
        assert participant.reopen_calls == 1
        assert participant.state == "RUNNING"
        assert delegate.barrier.read().state == "RUNNING"

    asyncio.run(run())


def test_abort_pre_mutation_owner_reopen_failure_requires_typed_recovery() -> None:
    async def run() -> None:
        delegate = _AbortDelegate()
        participant = _PrepareParticipant(reopen_error=RuntimeError("owner reopen failed"))
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]

        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.abort_pre_mutation(object())

        assert delegate.abort_calls == 1
        assert delegate.barrier.read().state == "RUNNING"
        assert participant.reopen_calls == 1
        assert participant.recovery_required_calls == 1
        assert participant.state == "RECOVERY_REQUIRED"
        assert exc_info.value.code == "MAINTENANCE_RECOVERY_REQUIRED"
        assert str(exc_info.value) == coordinator._RECOVERY_REQUIRED_MESSAGE
        assert isinstance(exc_info.value.__cause__, RuntimeError)

    asyncio.run(run())


def test_abort_pre_mutation_owner_reopen_timeout_requires_typed_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        delegate = _AbortDelegate()
        participant = _HangingOwnerParticipant()
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]
        monkeypatch.setattr(coordinator, "_OWNER_ADAPTER_REOPEN_TIMEOUT_SECONDS", 0.01)

        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.abort_pre_mutation(object())

        assert delegate.abort_calls == 1
        assert delegate.barrier.read().state == "RUNNING"
        assert participant.reopen_calls == 1
        assert participant.reopen_cancelled is True
        assert participant.recovery_required_calls == 1
        assert exc_info.value.code == "MAINTENANCE_RECOVERY_REQUIRED"
        assert str(exc_info.value) == coordinator._RECOVERY_REQUIRED_MESSAGE
        assert isinstance(exc_info.value.__cause__, TimeoutError)

    asyncio.run(run())


def test_abort_pre_mutation_delegate_error_is_preserved_without_owner_reopen() -> None:
    async def run() -> None:
        original = MaintenanceCoordinatorError("ABORT_FAILED", "delegate abort failed")
        delegate = _AbortDelegate(original)
        participant = _PrepareParticipant()
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]

        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.abort_pre_mutation(object())

        assert exc_info.value is original
        assert delegate.abort_calls == 1
        assert participant.reopen_calls == 0

    asyncio.run(run())


def test_recover_aborted_pre_mutation_reopens_owner_participant_after_base_running() -> None:
    async def run() -> None:
        delegate = _AbortDelegate()
        delegate.barrier.state = "RUNNING"
        participant = _PrepareParticipant(state="RECOVERY_REQUIRED")
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]

        snapshot = await coordinator.recover_aborted_pre_mutation(object())

        assert snapshot.state == "RUNNING"
        assert delegate.recover_calls == 1
        assert participant.reopen_calls == 1
        assert participant.state == "RUNNING"

    asyncio.run(run())


def test_recover_aborted_pre_mutation_owner_reopen_failure_stays_typed_recovery() -> None:
    async def run() -> None:
        delegate = _AbortDelegate()
        delegate.barrier.state = "RUNNING"
        participant = _PrepareParticipant(
            state="RECOVERY_REQUIRED",
            reopen_error=RuntimeError("owner reopen still failed"),
        )
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]

        with pytest.raises(MaintenanceCoordinatorError) as exc_info:
            await coordinator.recover_aborted_pre_mutation(object())

        assert exc_info.value.code == "MAINTENANCE_RECOVERY_REQUIRED"
        assert delegate.recover_calls == 1
        assert participant.reopen_calls == 1
        assert participant.state == "RECOVERY_REQUIRED"

    asyncio.run(run())


def test_owner_resume_preserves_stdio_ready_when_owner_adapter_reopen_fails() -> None:
    async def run() -> None:
        delegate_report = ResumeReport(("runtime-a",), {})
        delegate = _ResumeDelegate(delegate_report)
        participant = _FailingOwnerParticipant()
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]

        report = await coordinator.resume_running(object(), new_generation=2)

        assert report.ready_instance_ids == ("runtime-a",)
        assert report.failures == {}
        assert report.participant_failures == {"owner_adapter": "OWNER_ADAPTER_REOPEN_FAILED"}
        assert delegate_report.participant_failures == {}
        assert participant.reopen_calls == 1
        assert participant.recovery_required_calls == 1

    asyncio.run(run())


def test_owner_resume_timeout_preserves_exact_delegate_stdio_report(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        delegate_report = ResumeReport(("runtime-a",), {"runtime-b": "UNREACHABLE"})
        delegate = _ResumeDelegate(delegate_report)
        participant = _HangingOwnerParticipant()
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]
        monkeypatch.setattr(coordinator, "_OWNER_ADAPTER_REOPEN_TIMEOUT_SECONDS", 0.01)

        report = await coordinator.resume_running(object(), new_generation=2)

        assert report.ready_instance_ids == delegate_report.ready_instance_ids
        assert report.failures == delegate_report.failures
        assert report.participant_failures == {"owner_adapter": "OWNER_ADAPTER_REOPEN_TIMEOUT"}
        assert participant.reopen_calls == 1
        assert participant.reopen_cancelled is True
        assert participant.recovery_required_calls == 1

    asyncio.run(run())


def test_owner_resume_external_cancellation_is_not_swallowed() -> None:
    async def run() -> None:
        delegate_report = ResumeReport(("runtime-a",), {"runtime-b": "UNREACHABLE"})
        delegate = _ResumeDelegate(delegate_report)
        participant = _HangingOwnerParticipant()
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]
        task = asyncio.create_task(coordinator.resume_running(object(), new_generation=2))
        while participant.reopen_calls == 0:
            await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert participant.reopen_cancelled is True
        assert participant.recovery_required_calls == 1

    asyncio.run(run())


def test_publish_activation_resume_preserves_mixed_stdio_and_owner_failures() -> None:
    async def run() -> None:
        delegate_report = ResumeReport(("runtime-a",), {"runtime-b": "UNREACHABLE"})
        delegate = _ResumeDelegate(delegate_report)
        participant = _FailingOwnerParticipant()
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]

        report = await coordinator.publish_activation_and_resume(object(), new_generation=3)

        assert report.ready_instance_ids == ("runtime-a",)
        assert report.failures == {"runtime-b": "UNREACHABLE"}
        assert report.participant_failures == {"owner_adapter": "OWNER_ADAPTER_REOPEN_FAILED"}
        assert "owner_adapter" not in report.ready_instance_ids
        assert "owner_adapter" not in report.failures
        assert delegate.activation_calls == [
            ("begin_activation", 3),
            ("publish_running", 3),
            ("resume_running", 3),
        ]
        assert participant.reopen_calls == 1
        assert participant.recovery_required_calls == 1

    asyncio.run(run())


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, host: str) -> None:
        super().__init__(host, timeout=2)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


class _FakePopen:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.wait_calls: list[float] = []
        self.wait_timeout = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1

    def wait(self, *, timeout: float) -> int:
        self.wait_calls.append(timeout)
        if self.wait_timeout:
            raise subprocess.TimeoutExpired(cmd="caddy", timeout=timeout)
        self.returncode = 0
        return 0


class _FakeTCPConnection:
    def __init__(self, closes: list[bool]) -> None:
        self._closes = closes

    def close(self) -> None:
        self._closes.append(True)


def _spec(tmp_path: Path, *, normal: bool = True) -> BackendLaunchSpec:
    runtime_dir = tmp_path / "run" / "factlane"
    state_dir = tmp_path / "state" / "factlane"
    runtime_dir.mkdir(parents=True, mode=0o700)
    state_dir.mkdir(parents=True, mode=0o700)
    os.chmod(runtime_dir, 0o700)
    os.chmod(state_dir, 0o700)
    db = tmp_path / "memory.db"
    return BackendLaunchSpec(
        db=str(db) if normal else None,
        profile="nomic-768" if normal else None,
        host_id="owner-host-compose" if normal else None,
        expected_origin=ORIGIN,
        socket_path=str(runtime_dir / "owner-http.sock"),
        runtime_dir=str(runtime_dir),
        state_dir=str(state_dir),
        mode=BackendMode.NORMAL if normal else BackendMode.SETUP_SHELL,
        normal_adapter_required=normal,
    )


def _recovery_spec(tmp_path: Path) -> BackendLaunchSpec:
    runtime_dir = tmp_path / "run" / "factlane"
    state_dir = tmp_path / "state" / "factlane"
    runtime_dir.mkdir(parents=True, mode=0o700)
    state_dir.mkdir(parents=True, mode=0o700)
    db = tmp_path / "memory.db"
    db.touch()
    return BackendLaunchSpec(
        db=str(db),
        profile="nomic-768",
        host_id="owner-host-compose",
        expected_origin=ORIGIN,
        socket_path=str(runtime_dir / "owner-http.sock"),
        runtime_dir=str(runtime_dir),
        state_dir=str(state_dir),
        mode=BackendMode.RECOVERY_SHELL,
        normal_adapter_required=False,
    )


def _logical_import_payload() -> bytes:
    record = {
        "record_id": "00000000-0000-4000-8000-000000000101",
        "memory_id": "00000000-0000-4000-8000-000000000201",
        "revision": 1,
        "parent_record_id": None,
        "scope": "PROJECT",
        "project_id": "factlane",
        "memory_type": "PROJECT_LEARNED_FACT",
        "fact": "private import content must never enter the job ledger",
        "tags": ["subject:database-compose"],
        "source_provenance": {
            "source_class": "OWNER_PROVIDED",
            "source_ref": "fixture:webui-app",
            "source_hash": "a" * 64,
            "review_ref": "fixture:webui-app",
            "extraction_method": "MANUAL",
        },
        "freshness_policy": {"kind": "manual"},
        "source_timestamp": "2026-11-05T20:00:00Z",
        "lifecycle_state": "VALIDATED_CURRENT",
        "verified_by": "OWNER",
        "supersedes": [],
        "active": True,
    }
    return build_logical_export(
        [record],
        factlane_version="0.1.7",
        storage_contract_version=STORAGE_CONTRACT_VERSION,
        embedding_profile_id="nomic-768",
        exported_at="2026-11-05T20:30:00Z",
    )


def _service_state(tmp_path: Path) -> WebUIServiceState:
    return WebUIServiceState(
        tmp_path / "state" / "factlane" / "webui-service.json",
        boot_id=lambda: "boot-compose",
    )


def _runtime_config(tmp_path: Path) -> WebUIRuntimeConfig:
    runtime_dir = tmp_path / "run" / "factlane"
    state_dir = tmp_path / "state" / "factlane"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    return WebUIRuntimeConfig(
        db=str(tmp_path / "memory.db"),
        profile="nomic-768",
        host_id="owner-host-compose",
        origin=ORIGIN,
        backend_socket=str(runtime_dir / "owner-http.sock"),
        caddy_path="/usr/bin/caddy",
        caddyfile=str(tmp_path / "config" / "Caddyfile"),
        runtime_dir=str(runtime_dir),
        state_dir=str(state_dir),
    )


def test_caddy_process_requires_exact_argv_live_child_and_exact_origin_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _runtime_config(tmp_path)
    argv = caddy_command(config)
    process = _FakePopen()
    popen_calls: list[tuple[tuple[str, ...], bool]] = []
    connects: list[tuple[tuple[str, int], float]] = []
    closes: list[bool] = []
    listener_available = False

    def fake_popen(received_argv, *, shell):
        popen_calls.append((tuple(received_argv), shell))
        return process

    def fake_connect(address, timeout):
        connects.append((address, timeout))
        if not listener_available:
            raise ConnectionRefusedError("listener not ready")
        return _FakeTCPConnection(closes)

    monkeypatch.setattr("factlane.webui_app.subprocess.Popen", fake_popen)
    monkeypatch.setattr("factlane.webui_app.socket.create_connection", fake_connect)

    caddy = create_caddy_process(config, argv)

    assert popen_calls == [(argv, False)]
    assert caddy.poll() is None
    assert caddy.is_ready() is False
    assert connects[-1][0] == ("factlane-compose.test", 8765)

    listener_available = True
    assert caddy.is_ready() is True
    assert connects[-1][0] == ("factlane-compose.test", 8765)
    assert closes == [True]

    process.returncode = 7
    before = len(connects)
    assert caddy.is_ready() is False
    assert len(connects) == before


def test_caddy_process_graceful_stop_is_bounded_and_never_kills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _runtime_config(tmp_path)
    process = _FakePopen()
    monkeypatch.setattr("factlane.webui_app.subprocess.Popen", lambda _argv, *, shell: process)

    caddy = create_caddy_process(config, caddy_command(config))
    caddy.terminate()
    assert process.terminate_calls == 1

    process.wait_timeout = True
    assert caddy.wait_stopped(0.25) is False
    assert process.wait_calls == [0.25]
    assert not hasattr(process, "kill_calls")

    process.wait_timeout = False
    assert caddy.wait_stopped(0.5) is True
    assert process.wait_calls == [0.25, 0.5]


def test_caddy_process_rejects_mismatched_argv_or_corrupted_origin_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _runtime_config(tmp_path)
    launches: list[object] = []
    monkeypatch.setattr(
        "factlane.webui_app.subprocess.Popen",
        lambda *_args, **_kwargs: launches.append(object()) or pytest.fail("must not spawn"),
    )

    with pytest.raises(ValueError, match="argv"):
        create_caddy_process(config, (config.caddy_path, "version"))

    object.__setattr__(config, "origin", "http://factlane-compose.test:8765")
    with pytest.raises(ValueError, match="origin"):
        create_caddy_process(config, caddy_command(config))
    assert launches == []


def _post(server, path: str, body: dict[str, object], *, cookies: dict[str, str] | None = None):
    parsed = urlsplit(ORIGIN)
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {
        "Host": parsed.netloc,
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
    }
    if cookies:
        headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
    conn = _UnixHTTPConnection(str(server.socket_path), parsed.netloc)
    conn.request("POST", path, body=raw, headers=headers)
    response = conn.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    conn.close()
    return response.status, payload


def test_normal_backend_binds_exact_runtime_authority_and_adapter_to_one_service_loop(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    service_state = _service_state(tmp_path)
    events: list[tuple[str, int, int]] = []
    calls: list[tuple[str, str, str, str, str]] = []

    async def adapter_factory(db_path: str, profile: str, **kwargs) -> MemoryAdapter:
        events.append(("create", id(asyncio.get_running_loop()), threading.get_ident()))
        Path(db_path).touch()
        context = kwargs["trusted_write_context"]
        calls.append(
            (
                db_path,
                profile,
                kwargs["runtime_agent_id"],
                context.contributor_ref,
                context.grant_ref,
            )
        )
        return MemoryAdapter(_Engine(events), object(), trusted_write_context=context)  # type: ignore[arg-type]

    backend = create_owner_runtime_backend(
        spec,
        service_state,
        adapter_factory=adapter_factory,
        native_confirmation=_Confirmation(),
        owner_uid=os.getuid(),
    )

    assert backend.service_state is service_state
    assert backend.http_server.socket.family == socket.AF_UNIX
    assert backend.http_server.socket_path == Path(spec.socket_path)
    assert backend.http_server.origin == spec.expected_origin
    assert backend.native_server.socket.family == socket.AF_UNIX
    assert backend.native_server.socket_path == Path(spec.runtime_dir) / "owner-control.sock"
    assert backend.native_server.auth_state is backend.auth_state
    assert backend.native_server.service_control.service_state is service_state
    assert isinstance(backend.http_server.control_service, OwnerControlService)
    assert isinstance(backend.http_server.overview_service, OwnerOverviewService)
    assert isinstance(backend.http_server.operations_service, OwnerOperationsService)
    assert backend.http_server.control_service._adapter is backend.adapter
    assert backend.http_server.overview_service._adapter is backend.adapter
    assert backend.http_server.operations_service._owner is backend.http_server.control_service
    assert isinstance(backend.http_server.destructive_service, OwnerDestructiveOrchestrator)
    assert isinstance(backend.http_server.database_service, OwnerDatabaseService)
    assert backend.runtime_registry is backend.maintenance_coordinator.registry
    assert backend.maintenance_coordinator.barrier.database_path == os.path.realpath(spec.db)
    assert calls == [
        (
            spec.db,
            spec.profile,
            spec.host_id,
            spec.host_id,
            f"launcher:owner-current:{spec.host_id}",
        )
    ]

    backend.start()
    try:
        assert backend.is_ready() is True
        status, payload = _post(backend.http_server, "/auth/preauth", {"db": "/tmp/browser-chosen.db"})
        assert status == 400
        assert payload["status"] == "ERROR"
        assert payload["code"] == "INVALID_ENVELOPE"
        assert len(calls) == 1
    finally:
        backend.begin_drain()
        assert backend.wait_stopped(2.0) is True
    assert [item[0] for item in events] == ["create", "close"]
    assert events[0][1:] == events[1][1:]


def test_owner_adapter_participant_drains_releases_exclusion_and_reopens_exact_launch_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path)
    service_state = _service_state(tmp_path)
    coordinator = _ExclusiveProbeCoordinator(spec.db)
    events: list[tuple[str, int, int]] = []
    factory_calls: list[tuple[str, str, str, object, int, int]] = []
    engines: list[_LeaseEngine] = []

    monkeypatch.setattr(
        "factlane.webui_app._build_maintenance",
        lambda _spec, _db, *, owner_uid: (coordinator.registry, coordinator),
    )

    async def adapter_factory(db_path: str, profile: str, **kwargs) -> MemoryAdapter:
        context = kwargs["trusted_write_context"]
        factory_calls.append(
            (
                db_path,
                profile,
                kwargs["runtime_agent_id"],
                context,
                id(asyncio.get_running_loop()),
                threading.get_ident(),
            )
        )
        engine = _LeaseEngine(db_path, events)
        engines.append(engine)
        return MemoryAdapter(engine, object(), trusted_write_context=context)  # type: ignore[arg-type]

    backend = create_owner_runtime_backend(
        spec,
        service_state,
        adapter_factory=adapter_factory,
        native_confirmation=_Confirmation(),
        owner_uid=os.getuid(),
    )
    original = backend.adapter
    assert original is not None
    original_context = original._trusted_write_context
    assert original_context.authorization_class == "DIRECT_OWNER_OPERATOR"
    assert original_context.contributor_class == "OWNER"
    assert original_context.contributor_ref == spec.host_id
    assert original_context.grant_ref == f"launcher:owner-current:{spec.host_id}"
    control = backend.http_server.control_service
    overview = backend.http_server.overview_service
    operations = backend.http_server.operations_service
    assert isinstance(control, OwnerControlService)
    assert isinstance(overview, OwnerOverviewService)
    assert isinstance(operations, OwnerOperationsService)
    participant = backend.owner_adapter_participant
    assert participant is not None

    async def exercise() -> None:
        first_engine = engines[0]
        first_engine.status_entered = asyncio.Event()
        first_engine.status_release = asyncio.Event()
        health = asyncio.create_task(control.health(scope="GLOBAL_USER"))
        await first_engine.status_entered.wait()

        operation_id = "11111111-1111-4111-8111-111111111111"
        preparing = asyncio.create_task(
            backend.maintenance_coordinator.prepare_close(
                operation_id,
                drain_deadline=time.time() + 30.0,
            )
        )
        while participant.state == "RUNNING":
            await asyncio.sleep(0)
        assert participant.state == "DRAINING"
        assert first_engine.closed is False
        assert preparing.done() is False

        with pytest.raises(AdapterError) as control_blocked:
            await control.health(scope="GLOBAL_USER")
        assert control_blocked.value.code == "BACKEND_UNAVAILABLE"
        with pytest.raises(AdapterError) as overview_blocked:
            await overview.overview(limit=1)
        assert overview_blocked.value.code == "BACKEND_UNAVAILABLE"
        with pytest.raises(AdapterError) as operations_blocked:
            await operations.agent_inventory()
        assert operations_blocked.value.code == "BACKEND_UNAVAILABLE"

        first_engine.status_release.set()
        await health
        prepared = await preparing
        assert participant.state == "CLOSED"
        assert first_engine.closed is True
        assert coordinator.exclusion_proven == 1
        assert backend.adapter is None

        await backend.maintenance_coordinator.abort_pre_mutation(prepared)
        reopened_after_abort = backend.adapter
        assert reopened_after_abort is not None
        assert reopened_after_abort is not original
        assert participant.state == "RUNNING"
        assert control._adapter is reopened_after_abort
        assert control._engine is reopened_after_abort.engine
        assert overview._adapter is reopened_after_abort
        assert overview._engine is reopened_after_abort.engine
        assert operations._engine is reopened_after_abort.engine
        assert (await control.health(scope="GLOBAL_USER"))["backend_status"] == {"state": "OPEN"}

        prepared_again = await backend.maintenance_coordinator.prepare_close(
            operation_id,
            drain_deadline=time.time() + 30.0,
        )
        assert participant.state == "CLOSED"
        assert coordinator.exclusion_proven == 2
        backend.maintenance_coordinator.begin_activation(prepared_again, new_generation=2)
        backend.maintenance_coordinator.publish_running(prepared_again, new_generation=2)
        # RUNNING publication alone must not reopen the normal Owner adapter. This
        # leaves the PREPARED/post-close revalidation window exclusively governed.
        assert participant.state == "CLOSED"
        assert backend.adapter is None
        report = await backend.maintenance_coordinator.resume_running(prepared_again, new_generation=2)
        assert report.ready_instance_ids == ("runtime-a",)
        assert report.failures == {}
        assert report.participant_failures == {}
        assert participant.state == "RUNNING"
        assert backend.adapter is not None
        assert backend.adapter is not reopened_after_abort

        recovery_prepared = await backend.maintenance_coordinator.prepare_close(
            operation_id,
            drain_deadline=time.time() + 30.0,
        )
        assert participant.state == "CLOSED"
        assert coordinator.exclusion_proven == 3
        backend.maintenance_coordinator.mark_recovery_required(operation_id)
        assert participant.state == "RECOVERY_REQUIRED"
        backend.maintenance_coordinator.rearm_recovery(recovery_prepared)
        backend.maintenance_coordinator.begin_activation(recovery_prepared, new_generation=3)
        backend.maintenance_coordinator.publish_running(recovery_prepared, new_generation=3)
        assert participant.state == "RECOVERY_REQUIRED"
        recovery_report = await backend.maintenance_coordinator.resume_running(
            recovery_prepared,
            new_generation=3,
        )
        assert recovery_report.ready_instance_ids == ("runtime-a",)
        assert recovery_report.failures == {}
        assert recovery_report.participant_failures == {}
        assert participant.state == "RUNNING"
        assert backend.adapter is not None

    try:
        backend.http_server.run_service_coroutine(exercise)
        assert len(factory_calls) == 4
        assert all(call[0] == spec.db for call in factory_calls)
        assert all(call[1] == spec.profile for call in factory_calls)
        assert all(call[2] == spec.host_id for call in factory_calls)
        assert all(call[3] is original_context for call in factory_calls)
        assert len({call[4] for call in factory_calls}) == 1
        assert len({call[5] for call in factory_calls}) == 1
        assert [event[0] for event in events] == ["close", "close", "close"]
        assert all(event[1:] == factory_calls[0][4:] for event in events)
    finally:
        assert backend.wait_stopped(2.0) is True
    assert [event[0] for event in events] == ["close", "close", "close", "close"]


def test_setup_shell_never_opens_database_and_keeps_owner_services_unconfigured(tmp_path: Path) -> None:
    spec = _spec(tmp_path, normal=False)
    service_state = _service_state(tmp_path)

    async def forbidden_adapter_factory(*_args, **_kwargs):
        pytest.fail("setup shell must not initialize the normal memory adapter")

    backend = create_owner_runtime_backend(
        spec,
        service_state,
        adapter_factory=forbidden_adapter_factory,
        native_confirmation=_Confirmation(approved=True),
        owner_uid=os.getuid(),
    )

    assert backend.adapter is None
    assert backend.runtime_registry is None
    assert backend.maintenance_coordinator is None
    assert backend.http_server.overview_service is None
    assert backend.http_server.control_service is None
    assert backend.http_server.operations_service is None
    assert backend.http_server.destructive_service is None
    assert backend.http_server.database_service is None

    auth = backend.auth_state
    preauth = auth.issue_preauth(origin=ORIGIN)
    pending = auth.begin_enrollment(caller_uid=os.getuid(), source="test", device_label="Primary browser")
    auth.claim_pending(locator=pending.locator, origin=ORIGIN, preauth_secret=preauth)
    auth.approve_pending(locator=pending.locator, caller_uid=os.getuid())
    credentials = auth.poll_pending(locator=pending.locator, origin=ORIGIN, preauth_secret=preauth)
    assert credentials is not None

    backend.start()
    try:
        status, payload = _post(
            backend.http_server,
            "/api/read/overview",
            {"csrf": credentials.csrf_secret},
            cookies={"__Host-factlane-session": credentials.session_secret},
        )
        assert status == 503
        assert payload == {"status": "ERROR", "code": "SERVICE_UNAVAILABLE"}
    finally:
        backend.begin_drain()
        assert backend.wait_stopped(2.0) is True


def test_normal_backend_composes_database_restore_and_destructive_with_one_shared_registry(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    service_state = _service_state(tmp_path)

    async def adapter_factory(db_path: str, _profile: str, **kwargs) -> MemoryAdapter:
        Path(db_path).touch(exist_ok=True)
        return MemoryAdapter(_Engine([]), object(), trusted_write_context=kwargs["trusted_write_context"])  # type: ignore[arg-type]

    backend = create_owner_runtime_backend(
        spec,
        service_state,
        adapter_factory=adapter_factory,
        native_confirmation=_Confirmation(),
        owner_uid=os.getuid(),
    )
    try:
        assert isinstance(backend.database_service, OwnerDatabaseService)
        assert isinstance(backend.full_restore_service, FullRestoreService)
        assert isinstance(backend.http_server.destructive_service, OwnerDestructiveOrchestrator)
        assert backend.http_server.database_service is backend.database_service
        assert backend.full_restore_service.registry is backend.database_service._registry
        artifacts_root, restore_root, purge_root = _database_state_roots(spec, spec.db)
        assert backend.database_service._registry.root == artifacts_root
        assert backend.full_restore_service.state_dir == restore_root
        assert backend.http_server.destructive_service._state_root == purge_root
        assert backend.database_service._active_db_path == Path(os.path.realpath(spec.db))
        assert backend.database_service._logical_export_producer["storage_contract_version"] == STORAGE_CONTRACT_VERSION
        assert backend.database_service._logical_export_producer["embedding_profile_id"] == spec.profile
    finally:
        assert backend.wait_stopped(2.0) is True


def test_database_export_hook_holds_owner_gate_until_operation_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec(tmp_path)
    coordinator = _ExclusiveProbeCoordinator(spec.db)
    monkeypatch.setattr(
        "factlane.webui_app._build_maintenance",
        lambda _spec, _db, *, owner_uid: (coordinator.registry, coordinator),
    )
    entered: asyncio.Event | None = None
    release: asyncio.Event | None = None

    async def adapter_factory(db_path: str, _profile: str, **kwargs) -> MemoryAdapter:
        Path(db_path).touch(exist_ok=True)
        adapter = MemoryAdapter(_LeaseEngine(db_path, []), object(), trusted_write_context=kwargs["trusted_write_context"])  # type: ignore[arg-type]

        async def logical_export_records(_scope, *, lifecycle_states, max_records):
            assert lifecycle_states == ["VALIDATED_CURRENT"]
            assert max_records == 5
            assert entered is not None and release is not None
            entered.set()
            await release.wait()
            return []

        adapter.logical_export_records = logical_export_records  # type: ignore[method-assign]
        return adapter

    backend = create_owner_runtime_backend(
        spec,
        _service_state(tmp_path),
        adapter_factory=adapter_factory,
        native_confirmation=_Confirmation(),
        owner_uid=os.getuid(),
    )

    async def exercise() -> None:
        nonlocal entered, release
        entered = asyncio.Event()
        release = asyncio.Event()
        hook = backend.database_service._logical_export_selector
        assert hook is not None
        exporting = asyncio.create_task(
            hook(
                {
                    "scope": "PROJECT",
                    "project_id": "factlane",
                    "worktree_id": None,
                    "workflow_id": None,
                    "agent_id": None,
                    "lifecycle_states": ["VALIDATED_CURRENT"],
                    "max_records": 5,
                }
            )
        )
        await entered.wait()
        participant = backend.owner_adapter_participant
        assert participant is not None
        preparing = asyncio.create_task(
            backend.maintenance_coordinator.prepare_close(
                "22222222-2222-4222-8222-222222222222",
                drain_deadline=time.time() + 30,
            )
        )
        while participant.state == "RUNNING":
            await asyncio.sleep(0)
        assert participant.state == "DRAINING"
        assert preparing.done() is False
        release.set()
        assert await exporting == []
        prepared = await preparing
        assert participant.state == "CLOSED"
        await backend.maintenance_coordinator.abort_pre_mutation(prepared)

    try:
        backend.http_server.run_service_coroutine(exercise)
    finally:
        assert backend.wait_stopped(2.0) is True


def test_logical_import_preview_is_exact_scope_read_only_and_safe_for_job_ledger(tmp_path: Path) -> None:
    spec = _spec(tmp_path)

    class _ProbeEngine(_Engine):
        async def find_exact(self, _native_hash):
            return None

        async def find_contradictions(self, _key, _scope):
            return []

    async def adapter_factory(db_path: str, _profile: str, **kwargs) -> MemoryAdapter:
        Path(db_path).touch(exist_ok=True)
        return MemoryAdapter(_ProbeEngine([]), object(), trusted_write_context=kwargs["trusted_write_context"])  # type: ignore[arg-type]

    backend = create_owner_runtime_backend(
        spec,
        _service_state(tmp_path),
        adapter_factory=adapter_factory,
        native_confirmation=_Confirmation(),
        owner_uid=os.getuid(),
    )
    payload = _logical_import_payload()
    binding = {
        "job_id": str(uuid.uuid4()),
        "kind": "LOGICAL_IMPORT_PREVIEW",
        "operation_id": str(uuid.uuid4()),
        "artifact_id": str(uuid.uuid4()),
        "artifact_kind": "LOGICAL_IMPORT",
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "artifact_size_bytes": len(payload),
        "binding_digest": "b" * 64,
    }
    try:
        hook = backend.database_service._logical_import_preview
        assert hook is not None
        preview = backend.http_server.run_service_coroutine(lambda: hook(payload, binding))
        encoded = json.dumps(preview, sort_keys=True)
        assert preview["destination_analysis"] == "COMPLETE"
        assert preview["record_count"] == 1
        assert "private import content" not in encoded
        assert "source_provenance" not in encoded
        assert "candidate_admissions" not in preview
        assert "external_history" not in preview
    finally:
        assert backend.wait_stopped(2.0) is True


def test_recovery_shell_never_opens_owner_adapter_but_composes_restore_and_destructive_services(tmp_path: Path) -> None:
    spec = _recovery_spec(tmp_path)

    async def forbidden_adapter_factory(*_args, **_kwargs):
        pytest.fail("RECOVERY_SHELL must not open MemoryAdapter")

    backend = create_owner_runtime_backend(
        spec,
        _service_state(tmp_path),
        adapter_factory=forbidden_adapter_factory,
        native_confirmation=_Confirmation(),
        owner_uid=os.getuid(),
    )
    try:
        assert backend.adapter is None
        assert backend.owner_adapter_participant is None
        assert backend.runtime_registry is not None
        assert backend.maintenance_coordinator is not None
        assert isinstance(backend.database_service, OwnerDatabaseService)
        assert isinstance(backend.full_restore_service, FullRestoreService)
        assert backend.database_service._logical_export_selector is None
        assert backend.database_service._logical_import_apply is None
        assert backend.http_server.database_service is backend.database_service
        assert isinstance(backend.http_server.destructive_service, OwnerDestructiveOrchestrator)
        with pytest.raises(AdapterError) as unavailable:
            backend.http_server.run_service_coroutine(
                lambda: backend.http_server.destructive_service._target_resolver(tuple())
            )
        assert unavailable.value.code == "BACKEND_UNAVAILABLE"
    finally:
        assert backend.wait_stopped(2.0) is True


def test_restore_postflight_uses_maintenance_engine_without_provider_calls(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    profile = _configured_embedding_profile(spec.profile)

    async def provision() -> None:
        engine = SQLiteVecEngine(spec.db, profile)
        await engine.open()
        await engine.close()

    asyncio.run(provision())

    async def adapter_factory(db_path: str, _profile: str, **kwargs) -> MemoryAdapter:
        return MemoryAdapter(_Engine([]), object(), trusted_write_context=kwargs["trusted_write_context"])  # type: ignore[arg-type]

    backend = create_owner_runtime_backend(
        spec,
        _service_state(tmp_path),
        adapter_factory=adapter_factory,
        native_confirmation=_Confirmation(),
        owner_uid=os.getuid(),
    )
    callback = backend.full_restore_service._postflight
    assert callback is not None

    async def exercise() -> None:
        with MaintenanceLease(spec.db, "postflight-webui-app") as lease:
            assert lease.capability is not None
            await callback(spec.db, lease.capability)

    try:
        asyncio.run(exercise())
    finally:
        assert backend.wait_stopped(2.0) is True


def test_full_restore_is_bound_to_owner_auth_epoch_invalidation(tmp_path: Path) -> None:
    spec = _spec(tmp_path)

    async def adapter_factory(db_path: str, _profile: str, **kwargs) -> MemoryAdapter:
        Path(db_path).touch(exist_ok=True)
        return MemoryAdapter(_Engine([]), object(), trusted_write_context=kwargs["trusted_write_context"])  # type: ignore[arg-type]

    backend = create_owner_runtime_backend(
        spec,
        _service_state(tmp_path),
        adapter_factory=adapter_factory,
        native_confirmation=_Confirmation(),
        owner_uid=os.getuid(),
    )
    try:
        callback = backend.full_restore_service._invalidate_sessions
        assert callback is not None
        before = backend.auth_state._auth_epoch
        assert callback() == before + 1
        assert backend.auth_state._auth_epoch == before + 1
    finally:
        assert backend.wait_stopped(2.0) is True


def test_post_activation_verifier_rejects_foreign_runtime_and_returns_exact_valid_receipt(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    expected_profile = {"profile_id": "nomic-768", "marker": "bounded"}
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO adapter_meta VALUES ('contract_version', ?)", (str(STORAGE_CONTRACT_VERSION),))
        conn.execute("INSERT INTO adapter_meta VALUES ('embedding_profile', ?)", (json.dumps(expected_profile),))
        conn.commit()
    finally:
        conn.close()
    info = db.stat()
    instance_id = str(uuid.uuid4())
    original = RuntimeIdentity(
        protocol_version=PROTOCOL_VERSION,
        instance_id=instance_id,
        owner_uid=os.getuid(),
        pid=os.getpid(),
        process_start_ticks=10,
        boot_id="boot-verifier",
        database_path=os.path.realpath(db),
        device=info.st_dev,
        inode=max(0, info.st_ino - 1),
        generation=1,
        control_socket_path=str(tmp_path / "runtime.sock"),
        lifecycle_state="OPEN",
    )
    current = RuntimeIdentity(
        **{
            **original.to_mapping(),
            "device": info.st_dev,
            "inode": info.st_ino,
            "generation": 2,
            "lifecycle_state": "OPEN",
        }
    )

    class _Registry:
        identity = current

        def registrations(self, **_kwargs):
            return (self.identity,)

    class _Client:
        async def call(self, identity, request: RuntimeControlRequest):
            assert identity is _Registry.identity
            return {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request.request_id,
                "operation_id": request.operation_id,
                "instance_id": identity.instance_id,
                "state": "OPEN",
                "actual_generation": 2,
                "status_code": "OK",
                "lease_open": True,
            }

    registry = _Registry()
    verifier = _make_post_activation_verifier(os.path.realpath(db), registry, _Client())
    operation_id = str(uuid.uuid4())
    kwargs = {
        "operation_id": operation_id,
        "generation": 2,
        "database_path": os.path.realpath(db),
        "database_device": info.st_dev,
        "database_inode": info.st_ino,
        "expected_storage_contract_version": STORAGE_CONTRACT_VERSION,
        "expected_embedding_profile": expected_profile,
        "initially_open_runtime_identities": [original.to_mapping()],
        "ready_instance_ids": [instance_id],
        "reconnect_failures": {},
    }
    receipt = asyncio.run(verifier(**kwargs))
    assert receipt["status"] == "VERIFIED"
    assert receipt["bounded_read_verified"] is True
    assert receipt["resumed_runtime_evidence"][instance_id]["lease_open"] is True

    registry.identity = RuntimeIdentity(
        **{
            **current.to_mapping(),
            "process_start_ticks": 11,
        }
    )
    with pytest.raises(AdapterError, match="runtime"):
        asyncio.run(verifier(**kwargs))


def test_normal_destructive_resolver_converts_governed_rows_in_requested_order(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    resolved_calls: list[list[dict[str, object]]] = []

    async def adapter_factory(db_path: str, _profile: str, **kwargs) -> MemoryAdapter:
        Path(db_path).touch(exist_ok=True)
        adapter = MemoryAdapter(_Engine([]), object(), trusted_write_context=kwargs["trusted_write_context"])  # type: ignore[arg-type]

        async def resolve_permanent_purge_targets(identities):
            resolved_calls.append(list(identities))
            return [
                {
                    "scope": item["scope"].scope,
                    "project_id": item["scope"].project_id,
                    "worktree_id": item["scope"].worktree_id,
                    "workflow_id": item["scope"].workflow_id,
                    "agent_id": item["scope"].agent_id,
                    "memory_id": item["memory_id"],
                    "record_id": item["record_id"],
                    "revision": item["revision"],
                    "lifecycle_state": "VALIDATED_CURRENT",
                    "materialization_class": "PM1_FULL_NATIVE_MATERIALIZATION",
                    "native_materialized": True,
                    "native_content_hash": "c" * 64,
                }
                for item in identities
            ]

        adapter.resolve_permanent_purge_targets = resolve_permanent_purge_targets  # type: ignore[method-assign]
        return adapter

    backend = create_owner_runtime_backend(
        spec,
        _service_state(tmp_path),
        adapter_factory=adapter_factory,
        native_confirmation=_Confirmation(),
        owner_uid=os.getuid(),
    )
    first = PurgeTargetIdentity(
        RecoveryScope("PROJECT", project_id="factlane"),
        "memory-a",
        "record-a",
        1,
    )
    second = PurgeTargetIdentity(
        RecoveryScope("PROJECT", project_id="factlane"),
        "memory-b",
        "record-b",
        2,
    )
    try:
        service = backend.http_server.destructive_service
        assert isinstance(service, OwnerDestructiveOrchestrator)
        targets = backend.http_server.run_service_coroutine(
            lambda: service._target_resolver((first, second))
        )
        assert [target.record_id for target in targets] == ["record-a", "record-b"]
        assert all(isinstance(target, RecoveryTarget) for target in targets)
        assert len(resolved_calls) == 1
        assert [item["record_id"] for item in resolved_calls[0]] == ["record-a", "record-b"]
        assert all(isinstance(item["scope"], ScopeContext) for item in resolved_calls[0])
    finally:
        assert backend.wait_stopped(2.0) is True
