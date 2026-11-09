from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

if os.name == "nt":
    pytest.skip(
        "full Owner restore qualification requires POSIX maintenance and recovery locking",
        allow_module_level=True,
    )

import factlane.owner_restore as owner_restore_module
from factlane.contract import AdapterError
from factlane.maintenance_coordinator import MaintenanceCoordinatorError, PreparedMaintenance
from factlane.owner_database import ArtifactRegistry, create_consistent_backup
from factlane.owner_restore import FullRestoreService, RestoreRecoveryRequired
from factlane.runtime_control import PROTOCOL_VERSION, RuntimeIdentity
from factlane.webui_service import ServiceStatus, WebUIServiceState
from factlane.windows_owner_restore import WindowsRestoreMutationError


class _Prepared:
    operation_id = "11111111-1111-4111-8111-111111111111"
    original_generation = 7
    intent_revision = 1
    initially_open_instance_ids: tuple[str, ...] = ()
    registrations: tuple[object, ...] = ()

    def initially_open(self):
        return ()


class _ServiceState:
    def __init__(self, *, initial: str = "RUNNING", events: list[str] | None = None) -> None:
        self.status = initial
        self.initial = initial
        self.maintenance_operation_id: str | None = None
        self.newer_stop = False
        self.intent_revision = 0
        self.stopped_boot_id = "test-boot" if initial == "STOPPED" else None
        self.events = events if events is not None else []

    def snapshot(self):
        return SimpleNamespace(
            status=self.status,
            stopped_boot_id=self.stopped_boot_id,
            intent_revision=self.intent_revision,
            maintenance_operation_id=self.maintenance_operation_id,
        )

    def begin_maintenance(self, operation_id: str) -> None:
        assert self.maintenance_operation_id in {None, operation_id}
        self.maintenance_operation_id = operation_id
        self.status = "MAINTENANCE"
        self.events.append("service_begin")

    def request_stop(self) -> None:
        self.newer_stop = True
        self.intent_revision += 1
        self.stopped_boot_id = "test-boot"
        self.events.append("service_stop")

    def finish_maintenance(self, operation_id: str) -> None:
        assert self.maintenance_operation_id == operation_id
        self.maintenance_operation_id = None
        self.status = "STOPPED" if self.initial == "STOPPED" or self.newer_stop else "RUNNING"
        self.events.append("service_finish")

    def mark_recovery_required(self) -> None:
        self.maintenance_operation_id = None
        self.status = "RECOVERY_REQUIRED"
        self.events.append("service_recovery")

    def clear_recovery_required(self) -> None:
        assert self.status == "RECOVERY_REQUIRED"
        self.status = "STOPPED" if self.initial == "STOPPED" or self.newer_stop else "RUNNING"
        self.events.append("service_recovery_clear")


def _verified_product_proof(**kwargs):
    prepared = {
        item["instance_id"]: item
        for item in kwargs["initially_open_runtime_identities"]
    }
    resumed_runtime_evidence = {}
    for instance_id in kwargs["ready_instance_ids"]:
        original = prepared[instance_id]
        resumed_runtime_evidence[instance_id] = {
            "protocol_version": original["protocol_version"],
            "instance_id": instance_id,
            "owner_uid": original["owner_uid"],
            "pid": original["pid"],
            "process_start_ticks": original["process_start_ticks"],
            "boot_id": original["boot_id"],
            "database_path": kwargs["database_path"],
            "device": kwargs["database_device"],
            "inode": kwargs["database_inode"],
            "generation": kwargs["generation"],
            "control_socket_path": original["control_socket_path"],
            "lifecycle_state": "OPEN",
            "lease_open": True,
        }
    return {
        "status": "VERIFIED",
        "operation_id": kwargs["operation_id"],
        "generation": kwargs["generation"],
        "database_path": kwargs["database_path"],
        "database_device": kwargs["database_device"],
        "database_inode": kwargs["database_inode"],
        "storage_contract_version": kwargs["expected_storage_contract_version"],
        "embedding_profile": kwargs["expected_embedding_profile"],
        "bounded_read_verified": True,
        "ready_instance_ids": list(kwargs["ready_instance_ids"]),
        "reconnect_failures": dict(kwargs["reconnect_failures"]),
        "resumed_runtime_evidence": resumed_runtime_evidence,
    }


def _service(
    *,
    active_db: Path,
    registry: ArtifactRegistry,
    state_dir: Path,
    service_state: _ServiceState | None = None,
    **kwargs,
) -> FullRestoreService:
    kwargs.setdefault("invalidate_sessions", lambda: None)
    kwargs.setdefault("post_activation_verify", _verified_product_proof)
    return FullRestoreService(
        active_db=active_db,
        registry=registry,
        state_dir=state_dir,
        service_state=service_state or _ServiceState(),
        **kwargs,
    )


class _Coordinator:
    def __init__(
        self,
        *,
        active_db: Path | None = None,
        events: list[str] | None = None,
        prepared_result: PreparedMaintenance | None = None,
        ready_instance_ids: tuple[str, ...] = (),
        reconnect_failures: dict[str, str] | None = None,
        participant_failures: dict[str, str] | None = None,
        resume_delay: float = 0.0,
    ) -> None:
        self.prepared = False
        self.prepare_calls = 0
        self.aborted = False
        self.recovery = False
        self.resumed: list[int] = []
        self.activations: list[int] = []
        self.published: list[int] = []
        self.state = "RUNNING"
        self.generation = 7
        self.device: int | None = None
        self.inode: int | None = None
        self.active_db = active_db
        self.events = events if events is not None else []
        self.fail_resume = False
        self.crash_before_publish = False
        self.crash_after_publish = False
        self.fail_publish = False
        self.rearm_calls = 0
        self.prepared_result = prepared_result or _Prepared()
        self.ready_instance_ids = ready_instance_ids
        self.reconnect_failures = dict(reconnect_failures or {})
        self.participant_failures = dict(participant_failures or {})
        self.resume_delay = resume_delay
        self.resume_error: BaseException | None = None

    async def prepare_close(self, operation_id: str, *, drain_deadline: float):
        assert operation_id == _Prepared.operation_id
        self.prepared = True
        self.prepare_calls += 1
        self.state = "PREPARING"
        self.events.append("prepare")
        return self.prepared_result

    async def abort_pre_mutation(self, prepared) -> None:
        self.aborted = True
        self.state = "RUNNING"

    def mark_recovery_required(self, operation_id: str):
        if self.state not in {"PREPARING", "ACTIVATING", "RECOVERY_REQUIRED"}:
            raise AdapterError("INVALID_BARRIER_TRANSITION", "cannot mark recovery from current state")
        self.recovery = True
        self.state = "RECOVERY_REQUIRED"

    def rearm_recovery(self, prepared):
        assert prepared.operation_id == _Prepared.operation_id
        assert self.state in {"RECOVERY_REQUIRED", "PREPARING"}
        if self.state == "PREPARING":
            return type(
                "Preparing",
                (),
                {
                    "state": "PREPARING",
                    "generation": self.generation,
                    "original_generation": prepared.original_generation,
                    "intent_revision": prepared.intent_revision,
                },
            )()
        self.rearm_calls += 1
        self.state = "PREPARING"
        self.events.append("rearm_recovery")
        return type(
            "Preparing",
            (),
            {
                "state": "PREPARING",
                "generation": self.generation,
                "original_generation": prepared.original_generation,
                "intent_revision": prepared.intent_revision,
            },
        )()

    def begin_activation(self, prepared, *, new_generation: int):
        assert self.state == "PREPARING"
        self.state = "ACTIVATING"
        self.generation = new_generation
        if self.active_db is not None:
            info = self.active_db.stat()
            self.device = info.st_dev
            self.inode = info.st_ino
        self.activations.append(new_generation)
        self.events.append("begin_activation")
        return type(
            "Activation",
            (),
            {"state": "ACTIVATING", "generation": new_generation, "device": self.device, "inode": self.inode},
        )()

    async def resume_activating(self, prepared, *, new_generation: int):
        assert self.state == "ACTIVATING"
        if self.active_db is not None:
            conn = sqlite3.connect(self.active_db)
            try:
                row = conn.execute(
                    "SELECT value FROM adapter_meta WHERE key='sensitive_recovery_interlock'"
                ).fetchone()
            finally:
                conn.close()
            assert row is None, "runtime resume must happen only after DB interlock clear"
        self.events.append("resume")
        if self.fail_resume:
            raise RuntimeError("injected resume failure")
        self.resumed.append(new_generation)
        return type(
            "Report",
            (),
            {"ready_instance_ids": self.ready_instance_ids, "failures": dict(self.reconnect_failures)},
        )()

    async def resume_running(self, prepared, *, new_generation: int):
        assert self.state == "RUNNING"
        self.events.append("resume")
        if self.resume_error is not None:
            raise self.resume_error
        if self.resume_delay:
            await asyncio.sleep(self.resume_delay)
        if self.fail_resume:
            raise RuntimeError("injected resume failure")
        self.resumed.append(new_generation)
        return type(
            "Report",
            (),
            {
                "ready_instance_ids": self.ready_instance_ids,
                "failures": dict(self.reconnect_failures),
                "participant_failures": dict(self.participant_failures),
            },
        )()

    def publish_running(self, prepared, *, new_generation: int):
        assert self.state == "ACTIVATING"
        self.events.append("publish_running")
        if self.fail_publish:
            raise RuntimeError("injected final publish failure")
        if self.crash_before_publish:
            raise SystemExit("injected crash before final publish")
        self.state = "RUNNING"
        self.generation = new_generation
        self.published.append(new_generation)
        if self.crash_after_publish:
            raise SystemExit("injected lost response after final publish")
        return type("Running", (), {"state": "RUNNING", "generation": new_generation})()

    def activation_state(self, operation_id: str, *, new_generation: int, device=None, inode=None):
        if new_generation != self.generation:
            raise AdapterError("ACTIVATION_IDENTITY_MISMATCH", "generation mismatch")
        if device is not None and device != self.device:
            raise AdapterError("ACTIVATION_IDENTITY_MISMATCH", "device mismatch")
        if inode is not None and inode != self.inode:
            raise AdapterError("ACTIVATION_IDENTITY_MISMATCH", "inode mismatch")
        if self.state not in {"ACTIVATING", "RUNNING"}:
            raise AdapterError("INVALID_BARRIER_TRANSITION", "not activation-finalizable")
        return type(
            "ActivationState",
            (),
            {"state": self.state, "generation": self.generation, "device": self.device, "inode": self.inode},
        )()

    def reconcile_activation_ready(self, operation_id: str, *, new_generation: int, device: int, inode: int):
        state = self.activation_state(
            operation_id,
            new_generation=new_generation,
            device=device,
            inode=inode,
        )
        if state.state == "ACTIVATING":
            self.state = "RUNNING"
            self.published.append(new_generation)
        return type("Running", (), {"state": "RUNNING", "generation": new_generation})()


def _db(path: Path, value: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO adapter_meta(key,value) VALUES ('contract_version','3')")
    conn.execute("CREATE TABLE facts(value TEXT NOT NULL)")
    conn.execute("INSERT INTO facts(value) VALUES (?)", (value,))
    conn.commit()
    conn.close()


def _set_embedding_profile(path: Path, profile: dict[str, object]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO adapter_meta(key,value) VALUES ('embedding_profile',?)",
            (json.dumps(profile, sort_keys=True, separators=(",", ":")),),
        )
        conn.commit()
    finally:
        conn.close()


def _runtime_identity(path: Path, instance_id: str) -> RuntimeIdentity:
    info = path.stat()
    return RuntimeIdentity(
        protocol_version=PROTOCOL_VERSION,
        instance_id=instance_id,
        owner_uid=1000,
        pid=12345,
        process_start_ticks=67890,
        boot_id="restore-test-boot",
        database_path=str(path.resolve()),
        device=info.st_dev,
        inode=info.st_ino,
        generation=7,
        control_socket_path=str((path.parent / f"{instance_id}.sock").resolve()),
        lifecycle_state="OPEN",
    )


def _read(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT value FROM facts").fetchone()[0]
    finally:
        conn.close()


def _interlock(path: Path) -> str | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT value FROM adapter_meta WHERE key='sensitive_recovery_interlock'"
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


class _FakeWindowsRestoreOperator:
    def __init__(
        self,
        *,
        active_db: Path,
        state_dir: Path,
        registry: ArtifactRegistry,
        operation_id: str,
        events: list[str],
        fail_promote: bool = False,
        fail_enter: bool = False,
        fail_before_backup: bool = False,
        fail_classify: bool = False,
    ) -> None:
        self.active_db = Path(active_db)
        self.state_dir = Path(state_dir)
        self.registry = registry
        self.operation_id = operation_id
        self.events = events
        self.fail_promote = fail_promote
        self.fail_enter = fail_enter
        self.fail_before_backup = fail_before_backup
        self.fail_classify = fail_classify
        self.stage: Path | None = None
        self.retained = False

    def __enter__(self):
        self.events.append("windows_exclusive_acquire")
        if self.fail_enter:
            raise AdapterError("WINDOWS_RESTORE_EXCLUSION_UNAVAILABLE", "injected Windows exclusion failure")
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()

    def close(self) -> None:
        if self.retained:
            return
        self.events.append("windows_exclusive_release")

    def capture_pre_restore_backup(self, *, artifact_id: str):
        self.events.append("windows_snapshot")
        receipt = create_consistent_backup(self.active_db, self.registry, artifact_id=artifact_id)
        info = self.active_db.stat()
        return receipt, {
            "main": {
                "device": info.st_dev,
                "inode": info.st_ino,
                "sha256": owner_restore_module._sha256_file(self.active_db),
            }
        }

    def prepare_activation(self, stage: Path):
        self.events.append("windows_prepare_activation")
        self.stage = Path(stage)
        info = self.stage.stat()
        active_info = self.active_db.stat()
        return {
            "stage_name": self.stage.name,
            "stage_device": info.st_dev,
            "stage_inode": info.st_ino,
            "stage_sha256": owner_restore_module._sha256_file(self.stage),
            "active_device": active_info.st_dev,
            "active_inode": active_info.st_ino,
        }

    def activate_and_verify(self, intent):
        self.events.append("windows_activate")
        assert self.stage is not None
        if self.fail_before_backup:
            raise WindowsRestoreMutationError(
                "WINDOWS_STAGE_REJECTED",
                "injected pre-backup Windows stage failure",
                mutation_may_have_started=False,
            )
        before = self.active_db.stat()
        source = sqlite3.connect(self.stage)
        target = sqlite3.connect(self.active_db)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        after = self.active_db.stat()
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        if self.fail_promote:
            error = RuntimeError("injected Windows activation ambiguity after active-image mutation")
            error.code = "WINDOWS_ACTIVATION_AMBIGUOUS"  # type: ignore[attr-defined]
            error.safe_message = "Windows activation outcome is ambiguous"  # type: ignore[attr-defined]
            raise error
        self.stage.unlink()
        self.stage = None
        self.events.append("windows_postflight")
        return {
            "active_device": after.st_dev,
            "active_inode": after.st_ino,
            "active_interlock": _interlock(self.active_db),
            "intent_stage_sha256": intent["stage_sha256"],
        }

    def recover_activation(self, intent):
        self.events.append("windows_recover_activation")
        stage_name = intent.get("stage_name")
        stage = self.active_db.parent / stage_name if isinstance(stage_name, str) else None
        if _interlock(self.active_db) != self.operation_id and stage is not None and stage.exists():
            source = sqlite3.connect(stage)
            target = sqlite3.connect(self.active_db)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
        if stage is not None and stage.exists():
            stage.unlink()
        info = self.active_db.stat()
        return {
            "active_device": info.st_dev,
            "active_inode": info.st_ino,
            "active_interlock": _interlock(self.active_db),
            "intent_stage_sha256": intent["stage_sha256"],
        }

    def classify_activation(self, intent):
        self.events.append("windows_classify_activation")
        if self.fail_classify:
            raise WindowsRestoreMutationError(
                "WINDOWS_RESTORE_OUTCOME_UNPROVEN",
                "injected unprovable Windows activation outcome",
                mutation_may_have_started=True,
            )
        if _interlock(self.active_db) == self.operation_id:
            stage_name = intent.get("stage_name")
            stage = self.active_db.parent / stage_name if isinstance(stage_name, str) else None
            if stage is not None and stage.exists():
                stage.unlink()
            return {"state": "COMMITTED"}
        return {"state": "NOT_COMMITTED"}

    def verify_active(self, intent, proof, *, interlock_required: bool):
        info = self.active_db.stat()
        assert (info.st_dev, info.st_ino) == (intent["active_device"], intent["active_inode"])
        expected = self.operation_id if interlock_required else None
        assert _interlock(self.active_db) == expected
        return dict(proof)

    def prepare_runtime_handoff(self):
        self.events.append("windows_runtime_handoff")
        return {
            "journal_mode": "wal",
            "synchronous": 2,
            "busy_timeout_ms": 5000,
            "locking_mode": "normal",
        }

    def clear_interlock(self) -> None:
        self.events.append("windows_clear_interlock")
        owner_restore_module._clear_interlock(self.active_db, self.operation_id)

    def restore_interlock(self) -> None:
        self.events.append("windows_restore_interlock")
        owner_restore_module._write_interlock(self.active_db, self.operation_id)

    def retain_fail_closed(self) -> None:
        self.retained = True
        self.events.append("windows_retain_fail_closed")


def _disk_usage_with_free(free: int):
    return type("DiskUsage", (), {"total": free, "used": 0, "free": free})()


def test_full_restore_requires_registered_backup_and_trusted_origin(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    _db(active, "current")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    service = _service(active_db=active, registry=registry, state_dir=tmp_path / "state")

    with pytest.raises(AdapterError, match="registered"):
        service.preview(artifact_id="missing", trusted_origin_attested=True)

    backup = create_consistent_backup(active, registry, artifact_id="backup-a")
    with pytest.raises(AdapterError, match="trusted origin"):
        service.preview(artifact_id=backup["artifact_id"], trusted_origin_attested=False)


def test_full_restore_same_device_capacity_requires_snapshot_and_stage_sum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    old = create_consistent_backup(active, registry, artifact_id="old-retained")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service_state = _ServiceState()
    authorizations: list[str] = []
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda preview: authorizations.append(preview),
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    active_requirement = active.stat().st_size
    stage_requirement = replacement["size_bytes"]
    available = max(active_requirement, stage_requirement)
    assert available < active_requirement + stage_requirement
    monkeypatch.setattr(
        owner_restore_module.shutil,
        "disk_usage",
        lambda _path: _disk_usage_with_free(available),
    )
    artifact_ids_before = set(registry._load())

    with pytest.raises(AdapterError) as caught:
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert caught.value.code == "RESTORE_INSUFFICIENT_SPACE"
    assert coordinator.prepare_calls == 0
    assert authorizations == []
    assert service_state.status == "RUNNING"
    assert _read(active) == "current"
    assert set(registry._load()) == artifact_ids_before
    assert registry.get(old["artifact_id"])["sha256"] == old["sha256"]
    assert not list(active.parent.glob(f".{active.name}.restore-*.stage"))


def test_full_restore_capacity_includes_existing_wal_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    wal = Path(str(active) + "-wal")
    wal.write_bytes(b"w" * 8192)
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: pytest.fail("authorization must not run on insufficient space"),
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    available_without_wal = active.stat().st_size + replacement["size_bytes"]
    monkeypatch.setattr(
        owner_restore_module.shutil,
        "disk_usage",
        lambda _path: _disk_usage_with_free(available_without_wal),
    )

    with pytest.raises(AdapterError) as caught:
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert caught.value.code == "RESTORE_INSUFFICIENT_SPACE"
    assert coordinator.prepare_calls == 0
    assert wal.exists()
    assert _read(active) == "current"


def test_full_restore_capacity_splits_requirements_across_distinct_devices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    wal = Path(str(active) + "-wal")
    wal.write_bytes(b"w" * 4096)
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    snapshot_requirement = active.stat().st_size + wal.stat().st_size
    stage_requirement = replacement["size_bytes"]

    def capacity_device(path: Path) -> int:
        resolved = Path(path).resolve(strict=False)
        if resolved == registry.root:
            return 1001
        if resolved == active.parent.resolve(strict=False):
            return 1002
        raise AssertionError(f"unexpected capacity path: {resolved}")

    monkeypatch.setattr(service, "_capacity_device", capacity_device, raising=False)

    def disk_usage(path):
        resolved = Path(path).resolve(strict=False)
        if resolved == registry.root:
            return _disk_usage_with_free(snapshot_requirement)
        if resolved == active.parent.resolve(strict=False):
            return _disk_usage_with_free(stage_requirement)
        raise AssertionError(f"unexpected disk usage path: {resolved}")

    monkeypatch.setattr(owner_restore_module.shutil, "disk_usage", disk_usage)

    result = asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert result["status"] == "OK"
    assert coordinator.prepare_calls == 1
    assert _read(active) == "replacement"


def test_full_restore_rechecks_capacity_after_preview_before_maintenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service_state = _ServiceState()
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: pytest.fail("authorization must not run after capacity shrinks"),
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    monkeypatch.setattr(owner_restore_module.shutil, "disk_usage", lambda _path: _disk_usage_with_free(0))

    with pytest.raises(AdapterError) as caught:
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert caught.value.code == "RESTORE_INSUFFICIENT_SPACE"
    assert coordinator.prepare_calls == 0
    assert service_state.status == "RUNNING"
    assert _read(active) == "current"
    assert f"pre-restore-{_Prepared.operation_id}" not in registry._load()


def test_full_restore_rechecks_capacity_after_prepare_before_backup_and_same_operation_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    authorizations: list[str] = []
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda exact_preview: authorizations.append(exact_preview),
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    calls = 0

    def disk_usage(_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            return _disk_usage_with_free(0)
        return _disk_usage_with_free(1 << 40)

    monkeypatch.setattr(owner_restore_module.shutil, "disk_usage", disk_usage)

    with pytest.raises(AdapterError) as caught:
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert caught.value.code == "RESTORE_INSUFFICIENT_SPACE"
    assert coordinator.prepare_calls == 1
    assert coordinator.aborted is True
    assert authorizations == []
    assert _read(active) == "current"
    assert f"pre-restore-{_Prepared.operation_id}" not in registry._load()
    assert not list(active.parent.glob(f".{active.name}.restore-*.stage"))

    retry = asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=101.0))
    assert retry["status"] == "OK"
    assert coordinator.prepare_calls == 2
    assert authorizations == [preview["authorization_preview"]]
    assert _read(active) == "replacement"


@pytest.mark.parametrize("failure", ["device_stat", "disk_usage"])
def test_full_restore_capacity_observation_failure_is_fail_closed_before_maintenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: pytest.fail("authorization must not run when capacity cannot be observed"),
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    if failure == "device_stat":
        original_stat = Path.stat

        def fail_registry_root_stat(path: Path, *args, **kwargs):
            if path == registry.root:
                raise OSError("injected capacity stat failure")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fail_registry_root_stat)
    else:
        monkeypatch.setattr(
            owner_restore_module.shutil,
            "disk_usage",
            lambda _path: (_ for _ in ()).throw(OSError("injected disk usage failure")),
        )

    with pytest.raises(AdapterError) as caught:
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert caught.value.code == "RESTORE_CAPACITY_UNAVAILABLE"
    assert coordinator.prepare_calls == 0
    assert _read(active) == "current"


def test_full_restore_success_creates_pre_restore_snapshot_promotes_verified_image_and_resumes(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    events: list[str] = []
    coordinator = _Coordinator(active_db=active, events=events)
    service_state = _ServiceState(events=events)
    authorized: list[str] = []
    postflight: list[str] = []
    proof_calls: list[dict[str, object]] = []

    def product_proof(**kwargs):
        proof_calls.append(dict(kwargs))
        events.append("product_proof")
        return _verified_product_proof(**kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda preview: authorized.append(preview),
        postflight=lambda db, capability: (events.append("postflight"), postflight.append(_read(Path(db)))),
        invalidate_sessions=lambda: events.append("invalidate"),
        post_activation_verify=product_proof,
    )
    Path(str(active) + "-wal").write_bytes(b"stale-wal")
    Path(str(active) + "-shm").write_bytes(b"stale-shm")
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    result = asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert result["status"] == "OK"
    assert not Path(str(active) + "-wal").exists()
    assert not Path(str(active) + "-shm").exists()
    assert result["generation"] == 8
    assert _read(active) == "replacement"
    assert coordinator.prepared is True
    assert coordinator.aborted is False
    assert coordinator.recovery is False
    assert coordinator.resumed == [8]
    assert coordinator.activations == [8]
    assert coordinator.published == [8]
    assert events == [
        "service_begin",
        "prepare",
        "postflight",
        "begin_activation",
        "invalidate",
        "publish_running",
        "resume",
        "product_proof",
        "service_finish",
    ]
    assert service_state.status == "RUNNING"
    assert postflight == ["replacement"]
    assert authorized == [preview["authorization_preview"]]
    assert len(proof_calls) == 1
    assert proof_calls[0]["operation_id"] == _Prepared.operation_id
    assert proof_calls[0]["generation"] == 8
    assert proof_calls[0]["database_path"] == str(active.resolve())
    assert proof_calls[0]["database_device"] == active.stat().st_dev
    assert proof_calls[0]["database_inode"] == active.stat().st_ino
    assert proof_calls[0]["expected_storage_contract_version"] == preview["storage_contract_version"]
    assert proof_calls[0]["expected_embedding_profile"] == preview["embedding_profile"]
    assert proof_calls[0]["initially_open_runtime_identities"] == []
    assert proof_calls[0]["ready_instance_ids"] == []
    assert proof_calls[0]["reconnect_failures"] == {}
    assert result["post_activation_verification"] == {
        "status": "VERIFIED",
        "operation_id": _Prepared.operation_id,
        "generation": 8,
        "database_path": str(active.resolve()),
        "database_device": active.stat().st_dev,
        "database_inode": active.stat().st_ino,
        "storage_contract_version": preview["storage_contract_version"],
        "embedding_profile": preview["embedding_profile"],
        "bounded_read_verified": True,
        "ready_instance_ids": [],
        "reconnect_failures": {},
        "resumed_runtime_evidence": {},
    }
    assert result["pre_restore_artifact_id"].startswith("pre-restore-")
    assert registry.get(result["pre_restore_artifact_id"])["kind"] == "SQLITE_BACKUP"

    conn = sqlite3.connect(active)
    try:
        assert conn.execute("SELECT value FROM adapter_meta WHERE key='sensitive_recovery_interlock'").fetchone() is None
    finally:
        conn.close()


def test_windows_restore_operator_orders_exclusion_snapshot_promotion_release_and_resume(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    events: list[str] = []
    coordinator = _Coordinator(active_db=active, events=events)
    service_state = _ServiceState(events=events)

    def operator_factory(**kwargs):
        return _FakeWindowsRestoreOperator(events=events, **kwargs)

    def product_proof(**kwargs):
        events.append("product_proof")
        return _verified_product_proof(**kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: events.append("authorize"),
        postflight=lambda _db, _capability: pytest.fail("Windows handle-bound postflight must not call POSIX callback"),
        invalidate_sessions=lambda: events.append("invalidate"),
        post_activation_verify=product_proof,
        windows_restore_operator_factory=operator_factory,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    result = asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert result["status"] == "OK"
    assert _read(active) == "replacement"
    assert _interlock(active) is None
    assert events == [
        "service_begin",
        "prepare",
        "windows_exclusive_acquire",
        "windows_snapshot",
        "authorize",
        "windows_prepare_activation",
        "windows_activate",
        "windows_postflight",
        "begin_activation",
        "invalidate",
        "windows_runtime_handoff",
        "windows_clear_interlock",
        "publish_running",
        "windows_exclusive_release",
        "resume",
        "product_proof",
        "service_finish",
    ]
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "VERIFIED"
    assert journal["windows_snapshot_receipt"]["main"]["sha256"]
    assert journal["windows_mutation_intent"]["stage_sha256"]
    assert journal["windows_mutation_proof"]["active_interlock"] == _Prepared.operation_id
    assert journal["windows_runtime_handoff"]["journal_mode"] == "wal"


def test_windows_restore_exclusion_failure_aborts_before_snapshot_authorization_or_database_mutation(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    events: list[str] = []
    coordinator = _Coordinator(active_db=active, events=events)
    service_state = _ServiceState(events=events)

    def operator_factory(**kwargs):
        return _FakeWindowsRestoreOperator(events=events, fail_enter=True, **kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: pytest.fail("authorization must not run without Windows exclusion"),
        postflight=lambda _db, _capability: None,
        windows_restore_operator_factory=operator_factory,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(AdapterError) as caught:
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert caught.value.code == "WINDOWS_RESTORE_EXCLUSION_UNAVAILABLE"
    assert _read(active) == "current"
    assert coordinator.aborted is True
    assert service_state.status == "RUNNING"
    assert events == ["service_begin", "prepare", "windows_exclusive_acquire", "service_finish"]
    assert f"pre-restore-{_Prepared.operation_id}" not in registry._load()
    assert service.read_journal(_Prepared.operation_id)["phase"] == "ABORTED_BEFORE_ACTIVATION"


def test_windows_restore_postcommit_ambiguity_is_classified_released_and_recovered_from_intent(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    events: list[str] = []
    coordinator = _Coordinator(active_db=active, events=events)
    service_state = _ServiceState(events=events)
    attempts = 0

    def operator_factory(**kwargs):
        nonlocal attempts
        attempts += 1
        return _FakeWindowsRestoreOperator(events=events, fail_promote=attempts == 1, **kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: events.append("authorize_initial"),
        postflight=lambda _db, _capability: None,
        invalidate_sessions=lambda: events.append("invalidate"),
        post_activation_verify=lambda **kwargs: _verified_product_proof(**kwargs),
        windows_restore_operator_factory=operator_factory,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert _read(active) == "replacement"
    assert service_state.status == "RECOVERY_REQUIRED"
    assert coordinator.state == "RECOVERY_REQUIRED"
    assert "windows_retain_fail_closed" not in events
    assert "windows_classify_activation" in events
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "WINDOWS_MUTATION_INTENT"
    assert journal["windows_mutation_recovery_required"] is True
    assert journal["windows_mutation_classification"]["state"] == "COMMITTED"
    assert journal["windows_exclusion_retained"] is False
    assert journal["windows_runtime_handoff"]["journal_mode"] == "wal"
    stage = active.parent / journal["windows_mutation_intent"]["stage_name"]
    assert not stage.exists()

    recovered = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=101.0,
            authorize=lambda _preview: events.append("authorize_recovery"),
        )
    )

    assert recovered["status"] == "OK"
    assert recovered["idempotent_replay"] is True
    assert _read(active) == "replacement"
    assert not stage.exists()
    assert coordinator.rearm_calls == 1
    assert service_state.status == "RUNNING"
    assert events.index("windows_recover_activation") < events.index("rearm_recovery")
    assert events.index("windows_exclusive_release", events.index("windows_recover_activation")) < events.index("resume")
    assert service.read_journal(_Prepared.operation_id)["phase"] == "VERIFIED"


def test_windows_restore_prebackup_failure_aborts_without_recovery_or_retained_exclusion(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    events: list[str] = []
    coordinator = _Coordinator(active_db=active, events=events)
    service_state = _ServiceState(events=events)

    def operator_factory(**kwargs):
        return _FakeWindowsRestoreOperator(events=events, fail_before_backup=True, **kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        windows_restore_operator_factory=operator_factory,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(WindowsRestoreMutationError) as caught:
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert caught.value.mutation_may_have_started is False
    assert _read(active) == "current"
    assert coordinator.aborted is True
    assert coordinator.recovery is False
    assert service_state.status == "RUNNING"
    assert "windows_retain_fail_closed" not in events
    assert "windows_classify_activation" not in events
    assert not list(active.parent.glob(f".{active.name}.restore-*.stage"))
    assert service.read_journal(_Prepared.operation_id)["phase"] == "ABORTED_BEFORE_ACTIVATION"


def test_windows_restore_unprovable_postbackup_outcome_retains_exclusion_fail_closed(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    events: list[str] = []
    coordinator = _Coordinator(active_db=active, events=events)
    service_state = _ServiceState(events=events)

    def operator_factory(**kwargs):
        return _FakeWindowsRestoreOperator(events=events, fail_promote=True, fail_classify=True, **kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        windows_restore_operator_factory=operator_factory,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert _read(active) == "replacement"
    assert service_state.status == "RECOVERY_REQUIRED"
    assert coordinator.state == "RECOVERY_REQUIRED"
    assert "windows_retain_fail_closed" in events
    assert "windows_exclusive_release" not in events
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "WINDOWS_MUTATION_INTENT"
    assert journal["windows_exclusion_retained"] is True
    assert journal["windows_mutation_classification"] is None


def test_windows_restore_publish_failure_restores_interlock_and_retains_exclusion(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    events: list[str] = []
    coordinator = _Coordinator(active_db=active, events=events)
    coordinator.fail_publish = True
    service_state = _ServiceState(events=events)

    def operator_factory(**kwargs):
        return _FakeWindowsRestoreOperator(events=events, **kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        windows_restore_operator_factory=operator_factory,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert _read(active) == "replacement"
    assert _interlock(active) == _Prepared.operation_id
    assert service_state.status == "RECOVERY_REQUIRED"
    assert coordinator.state == "RECOVERY_REQUIRED"
    assert "windows_runtime_handoff" in events
    assert events.index("windows_clear_interlock") < events.index("publish_running")
    assert events.index("publish_running") < events.index("windows_restore_interlock")
    assert events.index("windows_restore_interlock") < events.index("windows_retain_fail_closed")
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "RECOVERY_REQUIRED"
    assert journal["windows_runtime_handoff"]["journal_mode"] == "wal"


def test_post_activation_proof_binds_fresh_restored_metadata_and_resumed_runtime_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    expected_profile = {"profile_id": "restore-profile", "model_digest": "a" * 64, "output_dimension": 768}
    _set_embedding_profile(replacement_source, expected_profile)
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")

    ready_id = "22222222-2222-4222-8222-222222222222"
    failed_id = "33333333-3333-4333-8333-333333333333"
    ready_identity = _runtime_identity(active, ready_id)
    failed_identity = _runtime_identity(active, failed_id)
    prepared = PreparedMaintenance(
        operation_id=_Prepared.operation_id,
        original_generation=7,
        intent_revision=1,
        initially_open_instance_ids=(ready_id, failed_id),
        registrations=(ready_identity, failed_identity),
    )
    coordinator = _Coordinator(
        active_db=active,
        prepared_result=prepared,
        ready_instance_ids=(ready_id,),
        reconnect_failures={failed_id: "REOPEN_FAILED"},
    )
    proof_calls: list[dict[str, object]] = []
    integrity_checks_while_running: list[Path] = []
    original_validate = owner_restore_module._validate_sqlite_image

    def observe_validate(path: Path):
        if path.resolve() == active.resolve() and coordinator.state == "RUNNING":
            integrity_checks_while_running.append(path)
            raise AssertionError("post-RUNNING metadata proof must not run full SQLite integrity_check")
        return original_validate(path)

    def proof(**kwargs):
        proof_calls.append(dict(kwargs))
        return _verified_product_proof(**kwargs)

    monkeypatch.setattr(owner_restore_module, "_validate_sqlite_image", observe_validate)
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=proof,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    result = asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert integrity_checks_while_running == []
    assert len(proof_calls) == 1
    call = proof_calls[0]
    assert call["expected_storage_contract_version"] == preview["storage_contract_version"]
    assert call["expected_embedding_profile"] == expected_profile
    assert call["ready_instance_ids"] == [ready_id]
    assert call["reconnect_failures"] == {failed_id: "REOPEN_FAILED"}
    assert [item["instance_id"] for item in call["initially_open_runtime_identities"]] == [ready_id, failed_id]

    receipt = result["post_activation_verification"]
    assert set(receipt) == {
        "status",
        "operation_id",
        "generation",
        "database_path",
        "database_device",
        "database_inode",
        "storage_contract_version",
        "embedding_profile",
        "bounded_read_verified",
        "ready_instance_ids",
        "reconnect_failures",
        "resumed_runtime_evidence",
    }
    assert receipt["ready_instance_ids"] == [ready_id]
    assert receipt["reconnect_failures"] == {failed_id: "REOPEN_FAILED"}
    assert result["ready_instance_ids"] == [ready_id]
    assert result["reconnect_failures"] == {failed_id: "REOPEN_FAILED"}
    runtime_evidence = receipt["resumed_runtime_evidence"][ready_id]
    assert runtime_evidence["instance_id"] == ready_id
    assert runtime_evidence["pid"] == ready_identity.pid
    assert runtime_evidence["process_start_ticks"] == ready_identity.process_start_ticks
    assert runtime_evidence["boot_id"] == ready_identity.boot_id
    assert runtime_evidence["database_path"] == str(active.resolve())
    assert runtime_evidence["device"] == active.stat().st_dev
    assert runtime_evidence["inode"] == active.stat().st_ino
    assert runtime_evidence["generation"] == 8
    assert runtime_evidence["lifecycle_state"] == "OPEN"
    assert runtime_evidence["lease_open"] is True


def test_post_activation_proof_rejects_ready_runtime_outside_initially_open_set(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    initial_id = "22222222-2222-4222-8222-222222222222"
    outsider_id = "33333333-3333-4333-8333-333333333333"
    prepared = PreparedMaintenance(
        operation_id=_Prepared.operation_id,
        original_generation=7,
        intent_revision=1,
        initially_open_instance_ids=(initial_id,),
        registrations=(_runtime_identity(active, initial_id),),
    )
    coordinator = _Coordinator(
        active_db=active,
        prepared_result=prepared,
        ready_instance_ids=(outsider_id,),
    )
    proof_calls = 0

    def proof(**kwargs):
        nonlocal proof_calls
        proof_calls += 1
        return _verified_product_proof(**kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=proof,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired, match="product verification"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert proof_calls == 0
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "ACTIVATION_VERIFICATION_FAILED"
    assert journal["verification_error"]["code"] == "POST_ACTIVATION_VERIFICATION_FAILED"


@pytest.mark.parametrize("field", ["storage_contract_version", "embedding_profile"])
def test_post_activation_proof_rejects_restored_metadata_mismatch(tmp_path: Path, field: str) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    profile = {"profile_id": "restore-profile"}
    _set_embedding_profile(replacement_source, profile)
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")

    def mismatched_proof(**kwargs):
        receipt = _verified_product_proof(**kwargs)
        receipt[field] = 999 if field == "storage_contract_version" else {"profile_id": "wrong-profile"}
        return receipt

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=_Coordinator(active_db=active),
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=mismatched_proof,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired, match="product verification"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "ACTIVATION_VERIFICATION_FAILED"
    assert journal["verification_error"]["code"] == "POST_ACTIVATION_VERIFICATION_FAILED"


def test_full_restore_prefers_per_call_authorize_and_passes_exact_authorization_preview(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    seen: list[str] = []

    def constructor_authorize(_preview: str) -> None:
        raise AssertionError("constructor authorize must not run when execute supplies a request-bound callback")

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=_Coordinator(active_db=active),
        authorize=constructor_authorize,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    result = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=100.0,
            authorize=lambda authorization_preview: seen.append(authorization_preview),
        )
    )
    assert result["status"] == "OK"
    assert seen == [preview["authorization_preview"]]


def test_full_restore_per_call_authorize_rejects_wrong_preview_and_constructor_can_be_absent(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=None,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    expected = preview["authorization_preview"] + "-stale"

    def reject_wrong(authorization_preview: str) -> None:
        if authorization_preview != expected:
            raise AdapterError("SENSITIVE_PREVIEW_MISMATCH", "request-bound restore authorization preview mismatch")

    with pytest.raises(AdapterError) as caught:
        asyncio.run(
            service.execute(
                preview=preview,
                operation_id=_Prepared.operation_id,
                now=100.0,
                authorize=reject_wrong,
            )
        )
    assert caught.value.code == "SENSITIVE_PREVIEW_MISMATCH"
    assert coordinator.aborted is True
    assert _read(active) == "current"


def test_full_restore_without_constructor_or_per_call_authorize_fails_closed(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    service_state = _ServiceState()
    coordinator = _Coordinator(active_db=active)
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=None,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    with pytest.raises(AdapterError) as caught:
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))
    assert caught.value.code == "RESTORE_NOT_CONFIGURED"
    assert coordinator.prepare_calls == 0
    assert service_state.status == "RUNNING"


@pytest.mark.parametrize("initial", ["RUNNING", "STOPPED"])
def test_restore_finishes_service_maintenance_respecting_initial_state(tmp_path: Path, initial: str) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    service_state = _ServiceState(initial=initial)
    coordinator = _Coordinator(active_db=active)
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    result = asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert result["status"] == "OK"
    assert service_state.status == initial
    assert service_state.maintenance_operation_id is None


def test_newer_stop_intent_during_restore_wins_on_finish(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    service_state = _ServiceState(initial="RUNNING")
    coordinator = _Coordinator(active_db=active)

    def invalidate() -> None:
        service_state.request_stop()

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        invalidate_sessions=invalidate,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert service_state.status == "STOPPED"
    assert service_state.newer_stop is True


@pytest.mark.parametrize(
    "missing",
    ["coordinator", "authorize", "postflight", "invalidate_sessions", "post_activation_verify"],
)
def test_executable_restore_fails_closed_when_required_dependency_is_missing(tmp_path: Path, missing: str) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    service_state = _ServiceState()
    kwargs = {
        "coordinator": _Coordinator(active_db=active),
        "authorize": lambda _preview: None,
        "postflight": lambda _db, _capability: None,
        "invalidate_sessions": lambda: None,
        "post_activation_verify": _verified_product_proof,
    }
    kwargs[missing] = None
    service = FullRestoreService(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        **kwargs,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))
    assert exc_info.value.code == "RESTORE_NOT_CONFIGURED"
    assert service_state.status == "RUNNING"
    assert service_state.maintenance_operation_id is None


def test_constructor_requires_service_state_dependency(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    _db(active, "current")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    with pytest.raises(TypeError):
        FullRestoreService(active_db=active, registry=registry, state_dir=tmp_path / "state")  # type: ignore[call-arg]


def test_pre_activation_failure_aborts_and_preserves_active_database(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service_state = _ServiceState(initial="RUNNING")

    def deny(_preview: str) -> None:
        raise AdapterError("SENSITIVE_GRANT_INVALID", "grant denied")

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=deny,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    with pytest.raises(AdapterError, match="grant denied"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert _read(active) == "current"
    assert coordinator.aborted is True
    assert coordinator.recovery is False
    assert service_state.status == "RUNNING"
    assert service_state.maintenance_operation_id is None


def test_prepare_recovery_required_marks_service_recovery_without_finishing_or_mutating(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    service_state = _ServiceState()
    coordinator = _Coordinator(active_db=active)

    async def recovery_required_prepare(_operation_id: str, *, drain_deadline: float):
        assert drain_deadline > 0
        raise MaintenanceCoordinatorError(
            "MAINTENANCE_RECOVERY_REQUIRED",
            "runtime close reconciliation requires recovery",
        )

    coordinator.prepare_close = recovery_required_prepare  # type: ignore[method-assign]
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired, match="maintenance preparation"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert _read(active) == "current"
    assert service_state.status == "RECOVERY_REQUIRED"
    assert service_state.events == ["service_begin", "service_recovery"]
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "MAINTENANCE_PREPARE_RECOVERY_REQUIRED"
    assert journal["service_initial"] == {
        "status": "RUNNING",
        "intent_revision": 0,
        "stopped_boot_id": None,
    }


def test_ordinary_clean_prepare_failure_finishes_service_and_preserves_original_error(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    service_state = _ServiceState()
    coordinator = _Coordinator(active_db=active)

    async def clean_prepare_failure(_operation_id: str, *, drain_deadline: float):
        assert drain_deadline > 0
        raise RuntimeError("clean prepare failure")

    coordinator.prepare_close = clean_prepare_failure  # type: ignore[method-assign]
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RuntimeError, match="clean prepare failure"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert _read(active) == "current"
    assert service_state.status == "RUNNING"
    assert service_state.events == ["service_begin", "service_finish"]


def test_post_activation_verification_failure_keeps_durable_interlock_and_marks_recovery(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service_state = _ServiceState()
    postflight_calls = 0
    recovery_authorized: list[str] = []

    def fail_postflight(_db: str, _capability) -> None:
        nonlocal postflight_calls
        postflight_calls += 1
        if postflight_calls == 1:
            raise RuntimeError("injected postflight failure")
        assert coordinator.state == "PREPARING"
        assert _interlock(active) == _Prepared.operation_id

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=fail_postflight,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    with pytest.raises(RestoreRecoveryRequired):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert _read(active) == "replacement"
    activated_inode = active.stat().st_ino
    snapshot_ids = [key for key in registry._load() if key.startswith("pre-restore-")]
    assert coordinator.recovery is True
    assert coordinator.aborted is False
    assert coordinator.resumed == []
    assert coordinator.prepare_calls == 1
    assert service_state.status == "RECOVERY_REQUIRED"
    assert _interlock(active) == _Prepared.operation_id
    failed_journal = service.read_journal(_Prepared.operation_id)
    assert failed_journal["recovery_from_phase"] == "ACTIVATED_UNVERIFIED"
    assert failed_journal["data_postflight_verified"] is False

    with pytest.raises(AdapterError) as missing_auth:
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.5))
    assert missing_auth.value.code == "RESTORE_RECOVERY_AUTH_REQUIRED"
    assert coordinator.rearm_calls == 0
    assert postflight_calls == 1
    assert _interlock(active) == _Prepared.operation_id

    recovered = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=101.0,
            authorize=lambda authorization_preview: recovery_authorized.append(authorization_preview),
        )
    )

    assert recovered["status"] == "OK"
    assert recovered["idempotent_replay"] is True
    assert recovery_authorized == [preview["authorization_preview"]]
    assert postflight_calls == 2
    assert coordinator.rearm_calls == 1
    assert coordinator.prepare_calls == 1
    assert active.stat().st_ino == activated_inode
    assert [key for key in registry._load() if key.startswith("pre-restore-")] == snapshot_ids
    assert coordinator.state == "RUNNING"
    assert service_state.status == "RUNNING"
    assert _interlock(active) is None


def test_full_restore_rejects_backup_with_incomplete_recovery_interlock(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    source = tmp_path / "blocked-source.db"
    _db(active, "current")
    _db(source, "blocked")
    conn = sqlite3.connect(source)
    conn.execute("INSERT INTO adapter_meta(key,value) VALUES ('sensitive_recovery_interlock','other-operation')")
    conn.commit()
    conn.close()
    registry = ArtifactRegistry(tmp_path / "artifacts")
    blocked = create_consistent_backup(source, registry, artifact_id="blocked")
    service = _service(active_db=active, registry=registry, state_dir=tmp_path / "state")

    with pytest.raises(AdapterError, match="recovery"):
        service.preview(artifact_id=blocked["artifact_id"], trusted_origin_attested=True)


def test_full_restore_exact_success_retry_returns_same_receipt_without_second_activation(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    first = asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))
    artifacts_after_first = set(registry._load())
    prepared_after_first = coordinator.prepared
    resumed_after_first = list(coordinator.resumed)

    replay = asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=101.0))

    assert replay["idempotent_replay"] is True
    assert {k: v for k, v in replay.items() if k != "idempotent_replay"} == first
    assert set(registry._load()) == artifacts_after_first
    assert coordinator.prepared is prepared_after_first
    assert coordinator.resumed == resumed_after_first
    assert _read(active) == "replacement"


def test_pre_activation_retry_reuses_existing_snapshot_for_same_operation(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    calls = {"count": 0}

    def authorize(_preview: str) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise AdapterError("SENSITIVE_GRANT_INVALID", "injected first authorization failure")

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=authorize,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    with pytest.raises(AdapterError, match="first authorization failure"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))
    snapshot_ids = [key for key in registry._load() if key.startswith("pre-restore-")]
    assert snapshot_ids == [f"pre-restore-{_Prepared.operation_id}"]

    result = asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=101.0))
    assert result["status"] == "OK"
    assert [key for key in registry._load() if key.startswith("pre-restore-")] == snapshot_ids
    assert _read(active) == "replacement"


def test_recovery_required_journal_preserves_bound_restore_identity(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: (_ for _ in ()).throw(RuntimeError("postflight")),
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    with pytest.raises(RestoreRecoveryRequired):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "RECOVERY_REQUIRED"
    assert journal["artifact_id"] == replacement["artifact_id"]
    assert journal["artifact_sha256"] == replacement["sha256"]
    assert journal["pre_restore_artifact_id"] == f"pre-restore-{_Prepared.operation_id}"
    assert journal["generation"] == 7


def test_recovery_after_postflight_begin_activation_failure_does_not_repeat_postflight_or_data_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    postflight_calls = 0

    def postflight(_db: str, _capability) -> None:
        nonlocal postflight_calls
        postflight_calls += 1

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=postflight,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    original_begin = coordinator.begin_activation
    failed = False

    def fail_after_begin(prepared, *, new_generation: int):
        nonlocal failed
        activation = original_begin(prepared, new_generation=new_generation)
        if not failed:
            failed = True
            raise RuntimeError("lost response after begin_activation")
        return activation

    monkeypatch.setattr(coordinator, "begin_activation", fail_after_begin)
    with pytest.raises(RestoreRecoveryRequired):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    activated_inode = active.stat().st_ino
    snapshot_ids = [key for key in registry._load() if key.startswith("pre-restore-")]
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "RECOVERY_REQUIRED"
    assert journal["recovery_from_phase"] == "POSTFLIGHT_VERIFIED"
    assert journal["data_postflight_verified"] is True

    recovered = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=101.0,
            authorize=lambda exact_preview: exact_preview == preview["authorization_preview"] or pytest.fail(),
        )
    )
    assert recovered["status"] == "OK"
    assert postflight_calls == 1
    assert coordinator.prepare_calls == 1
    assert coordinator.rearm_calls == 1
    assert active.stat().st_ino == activated_inode
    assert [key for key in registry._load() if key.startswith("pre-restore-")] == snapshot_ids


def test_recovery_after_session_invalidation_crash_may_reinvalidate_but_never_repromotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    postflight_calls = 0
    invalidations = 0

    def postflight(_db: str, _capability) -> None:
        nonlocal postflight_calls
        postflight_calls += 1

    def invalidate() -> None:
        nonlocal invalidations
        invalidations += 1

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=postflight,
        invalidate_sessions=invalidate,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    original_journal = service._journal
    crashed = False

    def crash_before_activation_ready(operation_id: str, **payload):
        nonlocal crashed
        if payload.get("phase") == "ACTIVATION_READY" and not crashed:
            crashed = True
            raise RuntimeError("lost response after session invalidation")
        return original_journal(operation_id, **payload)

    monkeypatch.setattr(service, "_journal", crash_before_activation_ready)
    with pytest.raises(RestoreRecoveryRequired):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))
    activated_inode = active.stat().st_ino
    assert invalidations == 1
    assert postflight_calls == 1
    assert service.read_journal(_Prepared.operation_id)["recovery_from_phase"] == "POSTFLIGHT_VERIFIED"

    recovered = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=101.0,
            authorize=lambda _preview: None,
        )
    )
    assert recovered["status"] == "OK"
    assert invalidations == 2
    assert postflight_calls == 1
    assert coordinator.prepare_calls == 1
    assert coordinator.rearm_calls == 1
    assert active.stat().st_ino == activated_inode


def test_failure_during_resume_after_running_is_reconnect_failure_not_db_recovery(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    coordinator.fail_resume = True
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    result = asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert coordinator.state == "RUNNING"
    assert coordinator.recovery is False
    assert coordinator.activations == [8]
    assert coordinator.resumed == []
    assert result["reconnect_failures"] == {"coordinator": "RUNTIME_RESUME_FAILED"}
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "VERIFIED"
    conn = sqlite3.connect(active)
    try:
        assert conn.execute(
            "SELECT value FROM adapter_meta WHERE key='sensitive_recovery_interlock'"
        ).fetchone() is None
    finally:
        conn.close()


def test_final_publish_failure_reestablishes_interlock_and_marks_recovery(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    coordinator.fail_publish = True
    service_state = _ServiceState()
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    activated_inode = active.stat().st_ino
    assert coordinator.state == "RECOVERY_REQUIRED"
    assert coordinator.recovery is True
    assert coordinator.published == []
    assert service_state.status == "RECOVERY_REQUIRED"
    conn = sqlite3.connect(active)
    try:
        assert conn.execute(
            "SELECT value FROM adapter_meta WHERE key='sensitive_recovery_interlock'"
        ).fetchone() == (_Prepared.operation_id,)
    finally:
        conn.close()
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "RECOVERY_REQUIRED"
    assert journal["recovery_from_phase"] == "ACTIVATION_READY"
    assert journal["data_postflight_verified"] is True

    coordinator.fail_publish = False
    authorized: list[str] = []
    recovered = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=101.0,
            authorize=lambda exact_preview: authorized.append(exact_preview),
        )
    )
    assert recovered["status"] == "OK"
    assert authorized == [preview["authorization_preview"]]
    assert coordinator.prepare_calls == 1
    assert coordinator.rearm_calls == 1
    assert active.stat().st_ino == activated_inode
    assert coordinator.state == "RUNNING"
    assert service_state.status == "RUNNING"
    assert _interlock(active) is None


@pytest.mark.parametrize("crash_after_publish", [False, True])
def test_activation_ready_crash_reconciles_without_repeating_restore(
    tmp_path: Path,
    crash_after_publish: bool,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    coordinator.crash_before_publish = not crash_after_publish
    coordinator.crash_after_publish = crash_after_publish
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(SystemExit):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "ACTIVATION_READY"
    assert coordinator.state == ("RUNNING" if crash_after_publish else "ACTIVATING")
    assert coordinator.prepare_calls == 1
    coordinator.crash_before_publish = False
    coordinator.crash_after_publish = False

    with pytest.raises(AdapterError) as missing_auth:
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=101.0))
    assert missing_auth.value.code == "RESTORE_RECOVERY_AUTH_REQUIRED"

    authorized: list[str] = []
    replay = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=101.0,
            authorize=lambda exact_preview: authorized.append(exact_preview),
        )
    )
    assert replay["status"] == "OK"
    assert replay["idempotent_replay"] is True
    assert authorized == [preview["authorization_preview"]]
    assert coordinator.state == "RUNNING"
    assert coordinator.prepare_calls == 1
    assert _read(active) == "replacement"
    assert service.read_journal(_Prepared.operation_id)["phase"] == "VERIFIED"


def test_activation_ready_reconciliation_fails_closed_on_generation_identity_mismatch(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    coordinator.crash_before_publish = True
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    with pytest.raises(SystemExit):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    coordinator.crash_before_publish = False
    coordinator.generation = 99
    with pytest.raises(AdapterError, match="generation mismatch"):
        asyncio.run(
            service.execute(
                preview=preview,
                operation_id=_Prepared.operation_id,
                now=101.0,
                authorize=lambda _preview: None,
            )
        )
    assert service.read_journal(_Prepared.operation_id)["phase"] == "ACTIVATION_READY"


def test_post_activation_product_proof_failure_keeps_running_barrier_but_service_requires_recovery(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service_state = _ServiceState()
    proof_calls = 0

    def fail_product_proof(**kwargs):
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 1:
            raise AdapterError("D12_PRODUCT_PROOF_FAILED", "fresh active-database verification was not observed")
        return _verified_product_proof(**kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=fail_product_proof,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired, match="product verification"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert coordinator.state == "RUNNING"
    assert coordinator.recovery is False
    assert service_state.status == "RECOVERY_REQUIRED"
    assert coordinator.prepare_calls == 1
    assert proof_calls == 1
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "ACTIVATION_VERIFICATION_FAILED"
    assert journal["data_postflight_verified"] is True
    assert journal["verification_error"]["code"] == "D12_PRODUCT_PROOF_FAILED"

    activated_inode = active.stat().st_ino
    with pytest.raises(AdapterError) as missing_auth:
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=101.0))
    assert missing_auth.value.code == "RESTORE_RECOVERY_AUTH_REQUIRED"
    assert coordinator.prepare_calls == 1
    assert proof_calls == 1

    authorized: list[str] = []
    recovered = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=102.0,
            authorize=lambda exact_preview: authorized.append(exact_preview),
        )
    )
    assert recovered["status"] == "OK"
    assert recovered["idempotent_replay"] is True
    assert authorized == [preview["authorization_preview"]]
    assert coordinator.prepare_calls == 1
    assert proof_calls == 2
    assert active.stat().st_ino == activated_inode
    assert coordinator.state == "RUNNING"
    assert service_state.status == "RUNNING"
    assert service.read_journal(_Prepared.operation_id)["phase"] == "VERIFIED"


def test_recovery_restores_initial_failed_service_state_from_durable_entry_evidence(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    actions: list[str] = []
    service_state = WebUIServiceState(
        tmp_path / "webui-service.json",
        controller=actions.append,
        boot_id=lambda: "boot-a",
    )
    service_state.mark_failed()
    coordinator = _Coordinator(active_db=active)
    proof_calls = 0

    def proof(**kwargs):
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 1:
            raise AdapterError("D12_PRODUCT_PROOF_FAILED", "injected product proof failure")
        return _verified_product_proof(**kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=proof,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired, match="product verification"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert service_state.snapshot().status is ServiceStatus.RECOVERY_REQUIRED
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["service_initial"] == {
        "status": "FAILED",
        "intent_revision": 0,
        "stopped_boot_id": None,
    }

    recovered = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=101.0,
            authorize=lambda _preview: None,
        )
    )

    assert recovered["status"] == "OK"
    assert recovered["idempotent_replay"] is True
    assert service_state.snapshot().status is ServiceStatus.FAILED
    assert actions == []


def test_recovery_preserves_initial_stopped_service_state_from_durable_entry_evidence(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    actions: list[str] = []
    service_state = WebUIServiceState(
        tmp_path / "webui-service.json",
        controller=actions.append,
        boot_id=lambda: "boot-a",
    )
    service_state.request_stop("stop-before-restore")
    coordinator = _Coordinator(active_db=active)
    proof_calls = 0

    def proof(**kwargs):
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 1:
            raise AdapterError("D12_PRODUCT_PROOF_FAILED", "injected product proof failure")
        return _verified_product_proof(**kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=proof,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    journal = service.read_journal(_Prepared.operation_id)
    assert journal["service_initial"] == {
        "status": "STOPPED_UNTIL_REBOOT",
        "intent_revision": 1,
        "stopped_boot_id": "boot-a",
    }

    recovered = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=101.0,
            authorize=lambda _preview: None,
        )
    )

    assert recovered["status"] == "OK"
    assert service_state.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT
    assert actions == ["stop"]


def test_recovery_preserves_newer_stop_intent_recorded_after_maintenance_entry(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    actions: list[str] = []
    service_state = WebUIServiceState(
        tmp_path / "webui-service.json",
        controller=actions.append,
        boot_id=lambda: "boot-a",
    )
    service_state.mark_ready()
    coordinator = _Coordinator(active_db=active)
    proof_calls = 0
    invalidations = 0

    def invalidate() -> None:
        nonlocal invalidations
        invalidations += 1
        service_state.request_stop("stop-during-restore")

    def proof(**kwargs):
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 1:
            raise AdapterError("D12_PRODUCT_PROOF_FAILED", "injected product proof failure")
        return _verified_product_proof(**kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        invalidate_sessions=invalidate,
        post_activation_verify=proof,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    journal = service.read_journal(_Prepared.operation_id)
    assert journal["service_initial"] == {
        "status": "READY",
        "intent_revision": 0,
        "stopped_boot_id": None,
    }
    assert service_state.snapshot().intent_revision == 1
    assert service_state.snapshot().stopped_boot_id == "boot-a"

    recovered = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=101.0,
            authorize=lambda _preview: None,
        )
    )

    assert recovered["status"] == "OK"
    assert invalidations == 1
    assert service_state.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT
    assert service_state.snapshot().intent_revision == 1
    assert actions == ["stop"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intent_revision", True),
        ("status", "X" * 65),
    ],
)
def test_recovery_rejects_malformed_durable_service_entry_evidence(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=lambda **_kwargs: (_ for _ in ()).throw(
            AdapterError("D12_PRODUCT_PROOF_FAILED", "injected product proof failure")
        ),
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    journal_path = service._journal_path(_Prepared.operation_id)
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    payload["service_initial"][field] = value
    journal_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AdapterError) as caught:
        asyncio.run(
            service.execute(
                preview=preview,
                operation_id=_Prepared.operation_id,
                now=101.0,
                authorize=lambda _preview: None,
            )
        )
    assert caught.value.code == "RESTORE_STATE_UNAVAILABLE"


def test_malformed_post_activation_receipt_is_not_accepted_as_verified(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service_state = _ServiceState()
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=lambda **_kwargs: {"status": "VERIFIED"},
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired, match="product verification"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert coordinator.state == "RUNNING"
    assert coordinator.recovery is False
    assert service_state.status == "RECOVERY_REQUIRED"
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "ACTIVATION_VERIFICATION_FAILED"
    assert journal["verification_error"]["code"] == "POST_ACTIVATION_VERIFICATION_INVALID"


def test_persisted_post_activation_proof_replays_without_repeating_restore_or_product_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    service_state = _ServiceState()
    proof_calls = 0

    def proof(**kwargs):
        nonlocal proof_calls
        proof_calls += 1
        return _verified_product_proof(**kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=proof,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)
    original_journal = service._journal
    crashed = False

    def crash_after_proof_receipt(operation_id: str, **payload):
        nonlocal crashed
        value = original_journal(operation_id, **payload)
        if payload.get("phase") == "POST_ACTIVATION_VERIFIED" and not crashed:
            crashed = True
            raise SystemExit("lost response after durable product proof receipt")
        return value

    monkeypatch.setattr(service, "_journal", crash_after_proof_receipt)
    with pytest.raises(SystemExit):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert service.read_journal(_Prepared.operation_id)["phase"] == "POST_ACTIVATION_VERIFIED"
    assert coordinator.prepare_calls == 1
    assert proof_calls == 1

    monkeypatch.setattr(service, "_journal", original_journal)
    replay = asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=101.0))
    assert replay["idempotent_replay"] is True
    assert replay["post_activation_verification"]["bounded_read_verified"] is True
    assert coordinator.prepare_calls == 1
    assert proof_calls == 1
    assert service_state.status == "RUNNING"
    assert service.read_journal(_Prepared.operation_id)["phase"] == "VERIFIED"


def test_participant_resume_failure_persists_stdio_truth_skips_product_proof_and_retries(tmp_path: Path) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    ready_id = "22222222-2222-4222-8222-222222222222"
    failed_id = "33333333-3333-4333-8333-333333333333"
    prepared = PreparedMaintenance(
        operation_id=_Prepared.operation_id,
        original_generation=7,
        intent_revision=1,
        initially_open_instance_ids=(ready_id, failed_id),
        registrations=(_runtime_identity(active, ready_id), _runtime_identity(active, failed_id)),
    )
    coordinator = _Coordinator(
        active_db=active,
        prepared_result=prepared,
        ready_instance_ids=(ready_id,),
        reconnect_failures={failed_id: "REOPEN_FAILED"},
        participant_failures={"owner_adapter": "OWNER_ADAPTER_REOPEN_FAILED"},
    )
    service_state = _ServiceState()
    proof_calls = 0

    def proof(**kwargs):
        nonlocal proof_calls
        proof_calls += 1
        return _verified_product_proof(**kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        service_state=service_state,
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=proof,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired, match="maintenance participant"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert proof_calls == 0
    assert service_state.status == "RECOVERY_REQUIRED"
    assert coordinator.state == "RUNNING"
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "ACTIVATION_VERIFICATION_FAILED"
    assert journal["ready_instance_ids"] == [ready_id]
    assert journal["reconnect_failures"] == {failed_id: "REOPEN_FAILED"}
    assert journal["maintenance_participant_failures"] == {"owner_adapter": "OWNER_ADAPTER_REOPEN_FAILED"}
    assert journal["result"]["ready_instance_ids"] == [ready_id]
    assert journal["result"]["reconnect_failures"] == {failed_id: "REOPEN_FAILED"}

    coordinator.participant_failures = {}
    recovered = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=101.0,
            authorize=lambda _preview: None,
        )
    )

    assert recovered["status"] == "OK"
    assert recovered["idempotent_replay"] is True
    assert proof_calls == 1
    assert coordinator.resumed == [8, 8]
    final_journal = service.read_journal(_Prepared.operation_id)
    assert final_journal["phase"] == "VERIFIED"
    assert final_journal["maintenance_participant_failures"] == {}
    assert final_journal["ready_instance_ids"] == [ready_id]
    assert final_journal["reconnect_failures"] == {failed_id: "REOPEN_FAILED"}


@pytest.mark.parametrize(
    "field",
    ["protocol_version", "owner_uid", "pid", "process_start_ticks", "device", "inode", "generation"],
)
def test_post_activation_proof_rejects_bool_in_nested_runtime_integer_evidence(tmp_path: Path, field: str) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    ready_id = "22222222-2222-4222-8222-222222222222"
    prepared = PreparedMaintenance(
        operation_id=_Prepared.operation_id,
        original_generation=7,
        intent_revision=1,
        initially_open_instance_ids=(ready_id,),
        registrations=(_runtime_identity(active, ready_id),),
    )
    coordinator = _Coordinator(active_db=active, prepared_result=prepared, ready_instance_ids=(ready_id,))

    def invalid_proof(**kwargs):
        receipt = _verified_product_proof(**kwargs)
        receipt["resumed_runtime_evidence"][ready_id][field] = True
        return receipt

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=invalid_proof,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired, match="product verification"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    journal = service.read_journal(_Prepared.operation_id)
    assert journal["verification_error"]["code"] == "POST_ACTIVATION_VERIFICATION_FAILED"


def test_post_running_resume_timeout_persists_failure_and_requires_authenticated_retry(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    coordinator.resume_error = MaintenanceCoordinatorError(
        "RUNTIME_RESUME_TIMEOUT",
        "runtime resume exceeded its absolute deadline",
    )
    proof_calls = 0

    def proof(**kwargs):
        nonlocal proof_calls
        proof_calls += 1
        return _verified_product_proof(**kwargs)

    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=proof,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired, match="runtime resume"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "ACTIVATION_VERIFICATION_FAILED"
    assert journal["ready_instance_ids"] == []
    assert journal["reconnect_failures"] == {"coordinator": "RUNTIME_RESUME_TIMEOUT"}
    assert journal["maintenance_participant_failures"] == {}
    assert proof_calls == 0

    coordinator.resume_error = None
    result = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=101.0,
            authorize=lambda _preview: None,
        )
    )
    assert result["status"] == "OK"
    assert result["idempotent_replay"] is True
    assert proof_calls == 1


def test_post_activation_verify_timeout_requires_recovery_and_does_not_auto_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.db"
    replacement_source = tmp_path / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    registry = ArtifactRegistry(tmp_path / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    coordinator = _Coordinator(active_db=active)
    proof_calls = 0

    async def proof(**kwargs):
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 1:
            await asyncio.sleep(0.05)
        return _verified_product_proof(**kwargs)

    monkeypatch.setattr(owner_restore_module, "_POST_ACTIVATION_VERIFY_TIMEOUT_SECONDS", 0.005, raising=False)
    service = _service(
        active_db=active,
        registry=registry,
        state_dir=tmp_path / "state",
        coordinator=coordinator,
        authorize=lambda _preview: None,
        postflight=lambda _db, _capability: None,
        post_activation_verify=proof,
    )
    preview = service.preview(artifact_id=replacement["artifact_id"], trusted_origin_attested=True)

    with pytest.raises(RestoreRecoveryRequired, match="product verification"):
        asyncio.run(service.execute(preview=preview, operation_id=_Prepared.operation_id, now=100.0))

    assert proof_calls == 1
    journal = service.read_journal(_Prepared.operation_id)
    assert journal["phase"] == "ACTIVATION_VERIFICATION_FAILED"
    assert journal["verification_error"]["code"] == "POST_ACTIVATION_VERIFICATION_TIMEOUT"
    assert journal["result"]["ready_instance_ids"] == []
    assert journal["result"]["reconnect_failures"] == {}

    recovered = asyncio.run(
        service.execute(
            preview=preview,
            operation_id=_Prepared.operation_id,
            now=101.0,
            authorize=lambda _preview: None,
        )
    )
    assert recovered["status"] == "OK"
    assert recovered["idempotent_replay"] is True
    assert proof_calls == 2
