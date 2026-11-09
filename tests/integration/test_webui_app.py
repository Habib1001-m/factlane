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
from factlane.owner_auth import NativeConfirmationRequest
from factlane.owner_database import build_logical_export
from factlane.owner_database_service import OwnerDatabaseService
from factlane.owner_destructive import OwnerDestructiveOrchestrator, PurgeTargetIdentity
from factlane.maintenance_coordinator import MaintenanceCoordinatorError, PreparedMaintenance, ResumeReport
from factlane.owner_operations import OwnerOperationsService
from factlane.owner_overview import OwnerOverviewService
from factlane.owner_control import OwnerControlService
from factlane.owner_http import OwnerSetupError
from factlane.owner_restore import FullRestoreService
from factlane.recovery import MaintenanceLease, RecoveryScope, RecoveryTarget
from factlane.runtime_control import PROTOCOL_VERSION, RuntimeControlRequest, RuntimeIdentity
from factlane.storage import SQLiteVecEngine, STORAGE_CONTRACT_VERSION
from factlane.windows_owner_purge import WindowsOwnerPurgeOperator
from factlane.windows_owner_restore import WindowsOwnerRestoreOperator
from factlane.webui_app import (
    _LinuxSetupBindingService,
    _OwnerAdapterMaintenanceCoordinator,
    _WindowsNativeConfirmation,
    _build_maintenance,
    _compose_database_authority,
    _configured_embedding_profile,
    _database_state_roots,
    _make_post_activation_verifier,
    _owner_capabilities_for_spec,
    _prepare_linux_managed_restart,
    _validate_linux_setup_database_path,
    _windows_process_start_provider,
    create_caddy_process,
    create_owner_runtime_backend,
)
from factlane.webui_runtime import (
    BackendLaunchSpec,
    BackendMode,
    BackendTransportKind,
    PlatformKind,
    WebUIRuntimeConfig,
    caddy_command,
    load_runtime_config,
)
from factlane.webui_service import WebUIServiceState
from factlane.webui_windows import WindowsManagedConfig


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

    async def resume_activating(self, _prepared, *, new_generation: int) -> ResumeReport:
        self.activation_calls.append(("resume_activating", new_generation))
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


def test_owner_resume_activating_preserves_stdio_truth_and_participant_failure() -> None:
    async def run() -> None:
        delegate_report = ResumeReport(
            ("runtime-a",),
            {"runtime-b": "UNREACHABLE"},
            {"existing_participant": "DEGRADED"},
        )
        delegate = _ResumeDelegate(delegate_report)
        participant = _FailingOwnerParticipant()
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]

        report = await coordinator.resume_activating(object(), new_generation=4)

        assert report.ready_instance_ids == ("runtime-a",)
        assert report.failures == {"runtime-b": "UNREACHABLE"}
        assert report.participant_failures == {
            "existing_participant": "DEGRADED",
            "owner_adapter": "OWNER_ADAPTER_REOPEN_FAILED",
        }
        assert delegate.activation_calls == [("resume_activating", 4)]
        assert participant.reopen_calls == 1
        assert participant.recovery_required_calls == 1

    asyncio.run(run())


def test_owner_resume_activating_success_preserves_delegate_report_without_fake_participant() -> None:
    async def run() -> None:
        delegate_report = ResumeReport(("runtime-a", "runtime-b"), {})
        delegate = _ResumeDelegate(delegate_report)
        participant = _PrepareParticipant(state="CLOSED")
        coordinator = _OwnerAdapterMaintenanceCoordinator(delegate, participant)  # type: ignore[arg-type]

        report = await coordinator.resume_activating(object(), new_generation=5)

        assert report.ready_instance_ids == delegate_report.ready_instance_ids
        assert report.failures == {}
        assert report.participant_failures == {}
        assert delegate.activation_calls == [("resume_activating", 5)]
        assert participant.reopen_calls == 1
        assert participant.state == "RUNNING"

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


class _FakeRestartPlan:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.handoffs = 0

    def handoff(self) -> None:
        self.handoffs += 1
        if self.fail:
            raise RuntimeError("restart handoff failed")


def _install_linux_setup_runtime_config(
    tmp_path: Path,
    spec: BackendLaunchSpec,
    monkeypatch: pytest.MonkeyPatch,
    *,
    configured: bool = False,
) -> Path:
    config_path = tmp_path / "config" / "factlane" / "webui.json"
    config_path.parent.mkdir(parents=True, mode=0o700)
    os.chmod(config_path.parent, 0o700)
    payload = {
        "db": str(Path(spec.state_dir) / "memory.db") if configured else None,
        "profile": "nomic-768" if configured else None,
        "host_id": "owner-compose" if configured else None,
        "origin": spec.expected_origin,
        "backend_socket": spec.socket_path,
        "caddy_path": str(tmp_path / "bin" / "caddy"),
        "caddyfile": str(config_path.parent / "Caddyfile"),
        "runtime_dir": spec.runtime_dir,
        "state_dir": spec.state_dir,
    }
    config_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(config_path, 0o600)
    monkeypatch.setattr("factlane.webui_app.default_runtime_config_path", lambda: config_path)
    return config_path


def _windows_spec(*, normal: bool = False) -> BackendLaunchSpec:
    return BackendLaunchSpec(
        db=r"C:\Users\Owner\AppData\Local\FactLane\memory.db" if normal else None,
        profile="nomic-768" if normal else None,
        host_id="owner-host-compose" if normal else None,
        expected_origin=ORIGIN,
        socket_path=None,
        runtime_dir=r"C:\Users\Owner\AppData\Local\FactLane\runtime",
        state_dir=r"C:\Users\Owner\AppData\Local\FactLane\state",
        mode=BackendMode.NORMAL if normal else BackendMode.SETUP_SHELL,
        normal_adapter_required=normal,
        platform_kind=PlatformKind.WINDOWS,
        backend_transport=BackendTransportKind.WINDOWS_HTTPS,
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


class _FakeWindowsHTTPServer:
    def __init__(self, *, origin: str, tls_context: object, auth_state, **_kwargs) -> None:
        self.origin = origin
        self.tls_context = tls_context
        self.auth_state = auth_state
        self.setup_service = _kwargs.get("setup_service")
        self.service_control = _kwargs.get("service_control")
        self.service_loop_thread_alive = True
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.probe_result = True
        self.probe_calls: list[tuple[object, float]] = []

    def serve_forever(self, poll_interval: float = 0.05) -> None:
        assert poll_interval == 0.05
        self.started.set()
        self.stopped.wait(2.0)

    def probe_ready(self, *, trust_context: object, timeout_seconds: float) -> bool:
        self.probe_calls.append((trust_context, timeout_seconds))
        return self.probe_result

    def shutdown(self) -> None:
        self.stopped.set()

    def server_close(self) -> None:
        self.stopped.set()


class _FakeWindowsNativeControlServer:
    def __init__(
        self,
        *,
        pipe_name: str,
        owner_uid: int,
        auth_state,
        service_control,
        owner_sid: str,
        **_kwargs,
    ) -> None:
        self.pipe_name = pipe_name
        self.owner_uid = owner_uid
        self.auth_state = auth_state
        self.service_control = service_control
        self.owner_sid = owner_sid
        self.started = threading.Event()
        self.stopped = threading.Event()

    def serve_forever(self, poll_interval: float = 0.05) -> None:
        assert poll_interval == 0.05
        self.started.set()
        self.stopped.wait(2.0)

    def shutdown(self) -> None:
        self.stopped.set()

    def server_close(self) -> None:
        self.stopped.set()


def test_windows_native_confirmation_uses_win32_confirmer_without_zenity(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []
    monkeypatch.setattr(
        "factlane.webui_app.windows_native_confirm",
        lambda message: messages.append(message) or True,
    )
    confirmation = _WindowsNativeConfirmation()
    request = NativeConfirmationRequest(
        kind="SENSITIVE_ACTION",
        verification_phrase="VERIFY-123",
        auth_epoch=1,
        caller_uid=0,
        origin=ORIGIN,
        preview="Delete one exact memory",
    )

    assert confirmation.confirm(request) is True
    assert len(messages) == 1
    assert "Action: SENSITIVE_ACTION" in messages[0]
    assert "Verification phrase: VERIFY-123" in messages[0]
    assert "Preview: Delete one exact memory" in messages[0]


def test_windows_setup_composition_uses_sid_pipe_loaded_tls_and_exact_readiness_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "S-1-5-21-100-200-300-1001"
    pipe = r"\\.\pipe\factlane-owner-deadbeefdeadbeef"
    material = object()
    server_context = object()
    client_context = object()
    tls_loads: list[tuple[Path, str, str | None]] = []
    spec = _windows_spec(normal=False)
    config_path = r"C:\Users\Owner\AppData\Local\FactLane\config\webui.json"
    browser_profile_dir = r"C:\Users\Owner\AppData\Local\FactLane\browser"
    managed = WindowsManagedConfig(
        installation_id="compose123",
        origin=spec.expected_origin,
        native_control_pipe=pipe,
        runtime_dir=spec.runtime_dir,
        state_dir=spec.state_dir,
        browser_profile_dir=browser_profile_dir,
    )

    monkeypatch.setattr("factlane.webui_app.current_user_sid", lambda: sid)
    monkeypatch.setattr(
        "factlane.webui_app.windows_owner_paths",
        lambda *, owner_sid: SimpleNamespace(
            native_control_socket=pipe,
            service_config_path=config_path,
            browser_profile_dir=browser_profile_dir,
        ),
    )
    monkeypatch.setattr(
        "factlane.webui_app.load_windows_managed_config",
        lambda path, *, owner_sid=None: managed,
    )
    monkeypatch.setattr(
        "factlane.webui_app.load_windows_tls_material",
        lambda root, hostname, *, owner_sid=None: tls_loads.append((Path(root), hostname, owner_sid)) or material,
    )
    monkeypatch.setattr("factlane.webui_app.build_windows_server_ssl_context", lambda exact: server_context if exact is material else None)
    monkeypatch.setattr("factlane.webui_app.build_windows_client_ssl_context", lambda exact: client_context if exact is material else None)
    monkeypatch.setattr("factlane.webui_app.WindowsOwnerHTTPServer", _FakeWindowsHTTPServer)
    monkeypatch.setattr("factlane.webui_app.WindowsOwnerNativeControlServer", _FakeWindowsNativeControlServer)

    backend = create_owner_runtime_backend(
        spec,
        _service_state(tmp_path),
        owner_uid=777,
    )
    try:
        assert backend.auth_state._owner_uid == 0
        assert isinstance(backend.auth_state._native_confirmation, _WindowsNativeConfirmation)
        assert isinstance(backend.http_server, _FakeWindowsHTTPServer)
        assert backend.http_server.tls_context is server_context
        assert backend.http_server.setup_service is not None
        assert tls_loads == [(Path(spec.state_dir) / "tls", "factlane-compose.test", sid)]
        assert isinstance(backend.native_server, _FakeWindowsNativeControlServer)
        assert backend.native_server.pipe_name == pipe
        assert backend.native_server.owner_uid == 0
        assert backend.native_server.owner_sid == sid
        assert backend.native_server.auth_state is backend.auth_state
        assert backend.http_server.service_control is backend.native_server.service_control
        assert backend.native_server.service_control.service_state is backend.service_state

        backend.start()
        assert backend.http_server.started.wait(1.0)
        assert backend.native_server.started.wait(1.0)
        backend.http_server.probe_result = False
        assert backend.is_ready() is False
        backend.http_server.probe_result = True
        assert backend.is_ready() is True
        assert backend.http_server.probe_calls[-1] == (client_context, 1.0)
    finally:
        backend.begin_drain()
        assert backend.wait_stopped(2.0) is True


def test_windows_capability_projection_matches_only_the_proven_normal_panel_contract(tmp_path: Path) -> None:
    assert _owner_capabilities_for_spec(_windows_spec(normal=True)) == {
        "database_management": True,
        "full_restore": True,
        "permanent_purge": True,
    }
    assert _owner_capabilities_for_spec(_windows_spec(normal=False)) == {
        "database_management": False,
        "full_restore": False,
        "permanent_purge": False,
    }
    assert _owner_capabilities_for_spec(_spec(tmp_path)) is None


def test_windows_maintenance_composition_uses_windows_registry_client_and_in_place_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "S-1-5-21-100-200-300-1001"
    created: dict[str, object] = {}

    class FakeRegistry:
        def __init__(self, runtime_root, **kwargs) -> None:
            created["runtime_root"] = runtime_root
            created["registry_kwargs"] = kwargs

    class FakeBarrier:
        def __init__(self, root, database_path) -> None:
            created["barrier_root"] = root
            self.database_path = database_path

        def initialize(self, *, generation: int):
            created["barrier_generation"] = generation
            return object()

    class FakeWindowsClient:
        def __init__(self, registry, *, owner_sid: str) -> None:
            created["client_registry"] = registry
            created["client_sid"] = owner_sid

        async def call(self, *_args, **_kwargs):
            return {}

    class FakeCoordinator:
        def __init__(self, *, barrier, registry, call_runtime, exclusion_check) -> None:
            self.barrier = barrier
            self.registry = registry
            self.call_runtime = call_runtime
            self.exclusion_check = exclusion_check

    class FakeLease:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeInPlace:
        @classmethod
        def acquire(cls, database_path):
            created["in_place_path"] = database_path
            lease = FakeLease()
            lease.revalidate = lambda: created.setdefault("revalidated", True)
            lease.release_unmutated = lambda: created.setdefault("released_unmutated", True)
            lease.retain_fail_closed = lambda: created.setdefault("retained_fail_closed", True)
            return lease

    boot_provider = lambda: "windows-boot"
    monkeypatch.setattr("factlane.webui_app.current_user_sid", lambda: sid)
    monkeypatch.setattr("factlane.webui_app.windows_boot_id", boot_provider)
    monkeypatch.setattr("factlane.webui_app.RuntimeRegistry", FakeRegistry)
    monkeypatch.setattr("factlane.webui_app.MaintenanceBarrierStore", FakeBarrier)
    monkeypatch.setattr("factlane.webui_app.WindowsRuntimeControlClient", FakeWindowsClient)
    monkeypatch.setattr("factlane.webui_app.MaintenanceCoordinator", FakeCoordinator)
    monkeypatch.setattr("factlane.webui_app.WindowsInPlaceMaintenanceLease", FakeInPlace)

    spec = _windows_spec(normal=True)
    registry, coordinator = _build_maintenance(spec, spec.db, owner_uid=999)

    assert registry is coordinator.registry
    assert created["registry_kwargs"] == {
        "owner_uid": 0,
        "boot_id_provider": boot_provider,
        "process_start_provider": _windows_process_start_provider,
    }
    assert created["client_registry"] is registry
    assert created["client_sid"] == sid
    assert coordinator.exclusion_check(spec.db) is True
    assert created["in_place_path"] == spec.db
    assert created["revalidated"] is True
    assert created["released_unmutated"] is True
    assert "retained_fail_closed" not in created


def test_windows_database_authority_selects_in_place_restore_and_panel_purge_operators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("factlane.webui_app._runtime_control_client_for_spec", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("factlane.webui_app._make_post_activation_verifier", lambda *_args, **_kwargs: (lambda **_kw: {}))
    spec = _windows_spec(normal=True)
    db = tmp_path / "memory.db"
    spec = BackendLaunchSpec(
        db=str(db),
        profile=spec.profile,
        host_id=spec.host_id,
        expected_origin=spec.expected_origin,
        socket_path=spec.socket_path,
        runtime_dir=str(tmp_path / "runtime"),
        state_dir=str(tmp_path / "state"),
        mode=spec.mode,
        normal_adapter_required=spec.normal_adapter_required,
        platform_kind=spec.platform_kind,
        backend_transport=spec.backend_transport,
    )
    Path(spec.runtime_dir).mkdir(parents=True)
    Path(spec.state_dir).mkdir(parents=True)

    class FakeRegistry:
        pass

    class FakeCoordinator:
        pass

    class FakeAuth:
        def invalidate_all_sessions_after_maintenance(self) -> None:
            return None

    database, restore, destructive = _compose_database_authority(
        spec=spec,
        database_path=str(db),
        profile_name="nomic-768",
        auth_state=FakeAuth(),  # type: ignore[arg-type]
        service_state=_service_state(tmp_path),
        runtime_registry=FakeRegistry(),  # type: ignore[arg-type]
        coordinator=FakeCoordinator(),
        adapter_executor=None,
    )

    assert isinstance(database, OwnerDatabaseService)
    assert restore._windows_restore_operator_factory is WindowsOwnerRestoreOperator
    assert isinstance(destructive._recovery_operator, WindowsOwnerPurgeOperator)
    assert destructive._post_close_target_resolver is not None


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


def test_caddy_process_requires_exact_argv_live_child_and_loopback_listener(
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
    assert connects[-1][0] == ("127.0.0.1", 8765)

    listener_available = True
    assert caddy.is_ready() is True
    assert connects[-1][0] == ("127.0.0.1", 8765)
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


def _establish_owner_session(backend):
    auth = backend.auth_state
    preauth = auth.issue_preauth(origin=ORIGIN)
    pending = auth.begin_enrollment(caller_uid=os.getuid(), source="test", device_label="Primary browser")
    auth.claim_pending(locator=pending.locator, origin=ORIGIN, preauth_secret=preauth)
    auth.approve_pending(locator=pending.locator, caller_uid=os.getuid())
    credentials = auth.poll_pending(locator=pending.locator, origin=ORIGIN, preauth_secret=preauth)
    assert credentials is not None
    return credentials


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
    assert backend.http_server.setup_service is None
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


def test_setup_shell_never_opens_database_and_keeps_owner_services_unconfigured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, normal=False)
    _install_linux_setup_runtime_config(tmp_path, spec, monkeypatch)
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
    assert backend.http_server.setup_service is not None

    credentials = _establish_owner_session(backend)

    backend.start()
    try:
        status, payload = _post(
            backend.http_server,
            "/api/setup/status",
            {"csrf": credentials.csrf_secret},
            cookies={"__Host-factlane-session": credentials.session_secret},
        )
        assert status == 200
        assert payload["status"] == "SETUP_REQUIRED"
        assert payload["suggested_db"] == str(Path(spec.state_dir) / "memory.db")
        assert payload["suggested_host_id"] == "owner-compose"
        assert "nomic-768" in payload["profiles"]

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


def test_linux_setup_shell_rejects_already_configured_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, normal=False)
    _install_linux_setup_runtime_config(tmp_path, spec, monkeypatch, configured=True)

    with pytest.raises(ValueError, match="must remain unbound"):
        create_owner_runtime_backend(
            spec,
            _service_state(tmp_path),
            native_confirmation=_Confirmation(approved=True),
            owner_uid=os.getuid(),
        )


def test_linux_setup_bind_validates_existing_database_preserves_runtime_fields_and_handoffs_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, normal=False)
    config_path = _install_linux_setup_runtime_config(tmp_path, spec, monkeypatch)
    baseline = load_runtime_config(config_path, owner_uid=os.getuid())
    database = Path(spec.state_dir) / "memory.db"

    async def provision() -> None:
        profile = _configured_embedding_profile("nomic-768")
        engine = SQLiteVecEngine(str(database), profile)
        await engine.open()
        await engine.close()
        metadata = {
            "metadata_revision": 2,
            "store_key": profile.profile_id,
            "semantic_identity": profile.semantic_identity(),
            "compatibility_proof": {},
            "runtime_provenance": {},
            "runtime_fingerprint": "f" * 64,
        }
        conn = sqlite3.connect(database)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO adapter_meta(key,value) VALUES ('embedding_profile',?)",
                (json.dumps(metadata, sort_keys=True, separators=(",", ":")),),
            )
            conn.commit()
        finally:
            conn.close()

    asyncio.run(provision())
    database_before = database.read_bytes()
    restart_plan = _FakeRestartPlan()
    monkeypatch.setattr("factlane.webui_app._prepare_linux_managed_restart", lambda: restart_plan)

    backend = create_owner_runtime_backend(
        spec,
        _service_state(tmp_path),
        native_confirmation=_Confirmation(approved=True),
        owner_uid=os.getuid(),
    )
    credentials = _establish_owner_session(backend)
    backend.start()
    try:
        status, payload = _post(
            backend.http_server,
            "/api/setup/bind",
            {
                "db": str(database),
                "profile": "nomic-768",
                "host_id": "owner-compose",
                "request_id": "linux-setup-1",
                "csrf": credentials.csrf_secret,
            },
            cookies={"__Host-factlane-session": credentials.session_secret},
        )
        assert status == 202, payload
        assert payload == {
            "status": "CONFIG_PUBLISHED",
            "restart": "REQUESTED",
            "request_id": "linux-setup-1",
        }
        assert restart_plan.handoffs == 1
        assert database.read_bytes() == database_before

        observed = load_runtime_config(config_path, owner_uid=os.getuid())
        assert observed.db == str(database)
        assert observed.profile == "nomic-768"
        assert observed.host_id == "owner-compose"
        for field in (
            "origin",
            "backend_socket",
            "caddy_path",
            "caddyfile",
            "runtime_dir",
            "state_dir",
            "platform_kind",
            "backend_transport",
        ):
            assert getattr(observed, field) == getattr(baseline, field)

        status, payload = _post(
            backend.http_server,
            "/api/setup/status",
            {"csrf": credentials.csrf_secret},
            cookies={"__Host-factlane-session": credentials.session_secret},
        )
        assert status == 200
        assert payload["status"] == "CONFIG_PUBLISHED_RESTART_REQUESTED"
        assert payload["binding"] == {
            "db": str(database),
            "profile": "nomic-768",
            "host_id": "owner-compose",
        }
    finally:
        backend.begin_drain()
        assert backend.wait_stopped(2.0) is True


def test_linux_setup_database_binding_rejects_group_or_other_writable_database(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = parent / "memory.db"
    database.write_bytes(b"sqlite")
    os.chmod(database, 0o666)

    with pytest.raises(OwnerSetupError) as raised:
        _validate_linux_setup_database_path(str(database), owner_uid=os.getuid())

    assert raised.value.code == "SETUP_DATABASE_PATH_INVALID"
    assert "not writable by group or other users" in raised.value.safe_message


def test_linux_setup_database_binding_rejects_group_or_other_writable_sidecar(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = parent / "memory.db"
    database.write_bytes(b"sqlite")
    os.chmod(database, 0o600)
    sidecar = Path(str(database) + "-wal")
    sidecar.write_bytes(b"wal")
    os.chmod(sidecar, 0o662)

    with pytest.raises(OwnerSetupError) as raised:
        _validate_linux_setup_database_path(str(database), owner_uid=os.getuid())

    assert raised.value.code == "SETUP_DATABASE_PATH_INVALID"
    assert "sidecars" in raised.value.safe_message


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_linux_setup_database_binding_rejects_writable_sidecar_when_main_database_is_absent(
    tmp_path: Path,
    suffix: str,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = parent / "memory.db"
    sidecar = Path(str(database) + suffix)
    sidecar.write_bytes(b"sidecar")
    os.chmod(sidecar, 0o662)

    with pytest.raises(OwnerSetupError) as raised:
        _validate_linux_setup_database_path(str(database), owner_uid=os.getuid())

    assert raised.value.code == "SETUP_DATABASE_PATH_INVALID"
    assert "sidecars" in raised.value.safe_message


def test_linux_setup_database_binding_rejects_sidecar_symlink_when_main_database_is_absent(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    database = parent / "memory.db"
    target = parent / "untrusted-sidecar"
    target.write_bytes(b"sidecar")
    Path(str(database) + "-wal").symlink_to(target)

    with pytest.raises(OwnerSetupError) as raised:
        _validate_linux_setup_database_path(str(database), owner_uid=os.getuid())

    assert raised.value.code == "SETUP_DATABASE_PATH_INVALID"
    assert "sidecars" in raised.value.safe_message


def test_linux_setup_database_binding_rejects_replaceable_direct_parent(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o777)
    database = parent / "memory.db"

    with pytest.raises(OwnerSetupError) as raised:
        _validate_linux_setup_database_path(str(database), owner_uid=os.getuid())

    assert raised.value.code == "SETUP_DATABASE_PATH_INVALID"
    assert "database parent" in raised.value.safe_message


def test_linux_setup_existing_database_profile_mismatch_fails_before_restart_or_config_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, normal=False)
    config_path = _install_linux_setup_runtime_config(tmp_path, spec, monkeypatch)
    baseline_bytes = config_path.read_bytes()
    database = Path(spec.state_dir) / "memory.db"

    async def provision() -> None:
        profile = _configured_embedding_profile("nomic-768")
        engine = SQLiteVecEngine(str(database), profile)
        await engine.open()
        await engine.close()
        metadata = {
            "metadata_revision": 2,
            "store_key": profile.profile_id,
            "semantic_identity": profile.semantic_identity(),
            "compatibility_proof": {},
            "runtime_provenance": {},
            "runtime_fingerprint": "f" * 64,
        }
        conn = sqlite3.connect(database)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO adapter_meta(key,value) VALUES ('embedding_profile',?)",
                (json.dumps(metadata, sort_keys=True, separators=(",", ":")),),
            )
            conn.commit()
        finally:
            conn.close()

    asyncio.run(provision())
    database_before = database.read_bytes()
    monkeypatch.setattr(
        "factlane.webui_app._prepare_linux_managed_restart",
        lambda: pytest.fail("restart must not be prepared for an incompatible database/profile"),
    )
    backend = create_owner_runtime_backend(
        spec,
        _service_state(tmp_path),
        native_confirmation=_Confirmation(approved=True),
        owner_uid=os.getuid(),
    )
    credentials = _establish_owner_session(backend)
    backend.start()
    try:
        status, payload = _post(
            backend.http_server,
            "/api/setup/bind",
            {
                "db": str(database),
                "profile": "nomic-512",
                "host_id": "owner-compose",
                "request_id": "linux-setup-mismatch",
                "csrf": credentials.csrf_secret,
            },
            cookies={"__Host-factlane-session": credentials.session_secret},
        )
        assert status == 409, payload
        assert payload == {
            "status": "ERROR",
            "code": "SETUP_DATABASE_PROFILE_MISMATCH",
            "message": "selected embedding profile does not match the existing database binding",
        }
        assert config_path.read_bytes() == baseline_bytes
        assert database.read_bytes() == database_before
    finally:
        backend.begin_drain()
        assert backend.wait_stopped(2.0) is True


def test_linux_setup_v2_database_requires_explicit_migration_without_mutating_config_or_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, normal=False)
    config_path = _install_linux_setup_runtime_config(tmp_path, spec, monkeypatch)
    baseline_bytes = config_path.read_bytes()
    database = Path(spec.state_dir) / "memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO adapter_meta VALUES ('contract_version','2')")
        conn.commit()
    finally:
        conn.close()
    database_before = database.read_bytes()
    monkeypatch.setattr(
        "factlane.webui_app._prepare_linux_managed_restart",
        lambda: pytest.fail("restart must not be prepared before explicit storage migration"),
    )
    backend = create_owner_runtime_backend(
        spec,
        _service_state(tmp_path),
        native_confirmation=_Confirmation(approved=True),
        owner_uid=os.getuid(),
    )
    credentials = _establish_owner_session(backend)
    backend.start()
    try:
        status, payload = _post(
            backend.http_server,
            "/api/setup/bind",
            {
                "db": str(database),
                "profile": "nomic-768",
                "host_id": "owner-compose",
                "request_id": "linux-setup-v2",
                "csrf": credentials.csrf_secret,
            },
            cookies={"__Host-factlane-session": credentials.session_secret},
        )
        assert status == 409, payload
        assert payload == {
            "status": "ERROR",
            "code": "SETUP_DATABASE_MIGRATION_REQUIRED",
            "message": "selected database uses storage v2 and must be migrated before Linux setup can bind it",
        }
        assert config_path.read_bytes() == baseline_bytes
        assert database.read_bytes() == database_before
    finally:
        backend.begin_drain()
        assert backend.wait_stopped(2.0) is True


def test_linux_setup_restart_preflight_failure_leaves_config_unmodified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, normal=False)
    config_path = _install_linux_setup_runtime_config(tmp_path, spec, monkeypatch)
    baseline_bytes = config_path.read_bytes()

    def unavailable_restart():
        raise OwnerSetupError(
            "LINUX_RESTART_UNAVAILABLE",
            "managed Linux WebUI restart is unavailable",
            status=503,
        )

    monkeypatch.setattr("factlane.webui_app._prepare_linux_managed_restart", unavailable_restart)
    backend = create_owner_runtime_backend(
        spec,
        _service_state(tmp_path),
        native_confirmation=_Confirmation(approved=True),
        owner_uid=os.getuid(),
    )
    credentials = _establish_owner_session(backend)
    backend.start()
    try:
        status, payload = _post(
            backend.http_server,
            "/api/setup/bind",
            {
                "db": str(Path(spec.state_dir) / "memory.db"),
                "profile": "nomic-768",
                "host_id": "owner-compose",
                "request_id": "linux-setup-no-restart",
                "csrf": credentials.csrf_secret,
            },
            cookies={"__Host-factlane-session": credentials.session_secret},
        )
        assert status == 503
        assert payload == {"status": "ERROR", "code": "LINUX_RESTART_UNAVAILABLE", "message": "managed Linux WebUI restart is unavailable"}
        assert config_path.read_bytes() == baseline_bytes
    finally:
        backend.begin_drain()
        assert backend.wait_stopped(2.0) is True


def test_linux_setup_readback_mismatch_restores_baseline_and_never_handoffs_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, normal=False)
    config_path = _install_linux_setup_runtime_config(tmp_path, spec, monkeypatch)
    baseline = load_runtime_config(config_path, owner_uid=os.getuid())
    baseline_bytes = config_path.read_bytes()
    restart_plan = _FakeRestartPlan()
    writes = 0

    def writer(path: str, content: str) -> None:
        nonlocal writes
        writes += 1
        payload = json.loads(content)
        if writes == 1:
            payload["origin"] = "https://factlane-drift.test:8765"
            content = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        Path(path).write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)

    service = _LinuxSetupBindingService(
        config_path=str(config_path),
        baseline_config=baseline,
        owner_uid=os.getuid(),
        supported_profiles={"nomic-768"},
        config_writer=writer,
        restart_preparer=lambda: restart_plan,
    )

    with pytest.raises(OwnerSetupError) as raised:
        service.bind(
            db=str(Path(spec.state_dir) / "memory.db"),
            profile="nomic-768",
            host_id="owner-compose",
            request_id="linux-setup-readback",
        )
    assert raised.value.code == "LINUX_CONFIG_PUBLICATION_FAILED"
    assert writes == 2
    assert config_path.read_bytes() == baseline_bytes
    assert restart_plan.handoffs == 0


def test_linux_setup_post_response_restart_failure_keeps_published_config_truthful(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path, normal=False)
    config_path = _install_linux_setup_runtime_config(tmp_path, spec, monkeypatch)
    restart_plan = _FakeRestartPlan(fail=True)
    monkeypatch.setattr("factlane.webui_app._prepare_linux_managed_restart", lambda: restart_plan)
    backend = create_owner_runtime_backend(
        spec,
        _service_state(tmp_path),
        native_confirmation=_Confirmation(approved=True),
        owner_uid=os.getuid(),
    )
    credentials = _establish_owner_session(backend)
    backend.start()
    try:
        status, payload = _post(
            backend.http_server,
            "/api/setup/bind",
            {
                "db": str(Path(spec.state_dir) / "memory.db"),
                "profile": "nomic-768",
                "host_id": "owner-compose",
                "request_id": "linux-setup-handoff-fail",
                "csrf": credentials.csrf_secret,
            },
            cookies={"__Host-factlane-session": credentials.session_secret},
        )
        assert status == 202
        assert payload["status"] == "CONFIG_PUBLISHED"
        assert restart_plan.handoffs == 1
        assert load_runtime_config(config_path, owner_uid=os.getuid()).memory_configured is True

        status, payload = _post(
            backend.http_server,
            "/api/setup/status",
            {"csrf": credentials.csrf_secret},
            cookies={"__Host-factlane-session": credentials.session_secret},
        )
        assert status == 200
        assert payload["status"] == "CONFIG_PUBLISHED_RESTART_REQUIRED"
        assert payload["restart_error"] == "LINUX_RESTART_HANDOFF_FAILED"
    finally:
        backend.begin_drain()
        assert backend.wait_stopped(2.0) is True


def test_linux_restart_preflight_and_handoff_use_exact_fixed_user_systemd_argv(tmp_path: Path) -> None:
    systemctl = tmp_path / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(systemctl, 0o700)
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> tuple[int, str]:
        calls.append(argv)
        if "is-active" in argv:
            return 0, "active\n"
        if "is-enabled" in argv:
            return 0, "enabled\n"
        return 0, ""

    plan = _prepare_linux_managed_restart(
        systemctl_resolver=lambda name: str(systemctl) if name == "systemctl" else None,
        runner=runner,
    )
    plan.handoff()

    assert calls == [
        (str(systemctl), "--user", "is-active", "factlane-webui.service"),
        (str(systemctl), "--user", "is-enabled", "factlane-webui.service"),
        (str(systemctl), "--user", "restart", "--no-block", "factlane-webui.service"),
    ]


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
