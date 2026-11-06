from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import uuid

import pytest

if sys.platform != "linux":
    pytest.skip(
        "managed Owner WebUI database-service qualification is Linux-only",
        allow_module_level=True,
    )

from factlane.contract import AdapterError
from factlane.owner_database import LOGICAL_IMPORT_MAX_BYTES, ArtifactRegistry, create_consistent_backup
from factlane.owner_database_service import (
    FULL_RESTORE_UPLOAD_MAX_BYTES,
    DOWNLOAD_READ_MAX_BYTES,
    OwnerDatabaseService,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _active_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO adapter_meta VALUES ('contract_version', '3')")
        conn.execute(
            "INSERT INTO adapter_meta VALUES ('embedding_profile', ?)",
            (json.dumps({"profile_id": "service-test", "store_key": "service-test"}),),
        )
        conn.execute("CREATE TABLE facts (value TEXT NOT NULL)")
        conn.execute("INSERT INTO facts VALUES ('kept')")
        conn.commit()
    finally:
        conn.close()


def _restore_sqlite_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / f"restore-source-{uuid.uuid4()}.sqlite3"
    _active_database(path)
    return path.read_bytes()


def _export_selection(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "scope": "PROJECT",
        "project_id": "factlane",
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "lifecycle_states": ["CANDIDATE"],
        "max_records": 100,
    }
    value.update(overrides)
    return value


def _verified_restore_result(
    binding: dict[str, object],
    *,
    database_path: str = "/private/factlane/active.sqlite3",
    control_socket_path: str = "/private/factlane/runtime-a.sock",
) -> dict[str, object]:
    operation_id = str(binding["operation_id"])
    artifact_id = str(binding["artifact_id"])
    artifact_sha256 = str(binding["artifact_sha256"])
    ready_instance_ids = ["runtime-a"]
    reconnect_failures = {"runtime-b": "UNREACHABLE"}
    return {
        "status": "OK",
        "operation": "FULL_RESTORE",
        "operation_id": operation_id,
        "artifact_id": artifact_id,
        "artifact_sha256": artifact_sha256,
        "pre_restore_artifact_id": f"pre-restore-{operation_id}",
        "generation": 2,
        "ready_instance_ids": ready_instance_ids,
        "reconnect_failures": reconnect_failures,
        "maintenance_participant_failures": {},
        "post_activation_verification": {
            "status": "VERIFIED",
            "operation_id": operation_id,
            "generation": 2,
            "database_path": database_path,
            "database_device": 17,
            "database_inode": 23,
            "storage_contract_version": 3,
            "embedding_profile": {"profile_id": "service-test", "store_key": "service-test"},
            "bounded_read_verified": True,
            "ready_instance_ids": ready_instance_ids,
            "reconnect_failures": reconnect_failures,
            "resumed_runtime_evidence": {
                "runtime-a": {
                    "protocol_version": 1,
                    "instance_id": "runtime-a",
                    "owner_uid": os.getuid(),
                    "pid": os.getpid(),
                    "process_start_ticks": 123,
                    "boot_id": "boot-service-test",
                    "database_path": database_path,
                    "device": 17,
                    "inode": 23,
                    "generation": 2,
                    "control_socket_path": control_socket_path,
                    "lifecycle_state": "OPEN",
                    "lease_open": True,
                }
            },
        },
    }


def _only_registered_payload(root: Path) -> Path:
    candidates = [
        path
        for path in root.iterdir()
        if path.is_file() and not path.name.startswith(".")
    ]
    assert len(candidates) == 1
    return candidates[0]


def test_upload_sink_is_owner_private_server_named_and_registers_only_after_exact_completion(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    service = OwnerDatabaseService(root)
    payload = b'{"format":"fixture"}'
    sink = service.begin_logical_import_upload(declared_size=len(payload))

    uuid.UUID(sink.upload_id)
    assert sink.filename.startswith(f"upload-{sink.upload_id}-")
    assert "/" not in sink.filename
    assert _mode(root) == 0o700
    assert _mode(root / ".staging") == 0o700
    staged = root / ".staging" / sink.filename
    assert staged.is_file()
    assert _mode(staged) == 0o600
    assert service.list_artifacts() == []

    assert sink.write(payload[:5]) == 5
    assert sink.write(payload[5:]) == len(payload) - 5
    metadata = sink.seal()

    uuid.UUID(metadata["artifact_id"])
    assert metadata["kind"] == "LOGICAL_IMPORT"
    assert metadata["upload_id"] == sink.upload_id
    assert metadata["size_bytes"] == len(payload)
    assert metadata["sha256"] == hashlib.sha256(payload).hexdigest()
    assert "path" not in metadata and "relative_path" not in metadata
    assert not staged.exists()
    registered = _only_registered_payload(root)
    assert _mode(registered) == 0o600
    assert service.list_artifacts() == [metadata]


def test_upload_preflight_enforces_limits_free_space_digest_and_exact_byte_accounting(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    service = OwnerDatabaseService(root, free_space_provider=lambda _path: 8)

    with pytest.raises(AdapterError, match="10 MiB"):
        service.begin_logical_import_upload(declared_size=LOGICAL_IMPORT_MAX_BYTES + 1)
    with pytest.raises(AdapterError, match="1 GiB"):
        service.begin_restore_upload(declared_size=FULL_RESTORE_UPLOAD_MAX_BYTES + 1)
    with pytest.raises(AdapterError, match="space"):
        service.begin_restore_upload(declared_size=9)

    exact_root = tmp_path / "exact"
    exact = OwnerDatabaseService(exact_root, free_space_provider=lambda _path: 1024)
    overflow = exact.begin_logical_import_upload(declared_size=3)
    staged_overflow = exact_root / ".staging" / overflow.filename
    with pytest.raises(AdapterError, match="declared"):
        overflow.write(b"four")
    assert not staged_overflow.exists()

    incomplete = exact.begin_logical_import_upload(declared_size=4)
    staged_incomplete = exact_root / ".staging" / incomplete.filename
    incomplete.write(b"abc")
    with pytest.raises(AdapterError, match="incomplete"):
        incomplete.seal()
    assert not staged_incomplete.exists()

    wrong_digest = exact.begin_restore_upload(
        declared_size=4,
        expected_sha256="0" * 64,
    )
    staged_digest = exact_root / ".staging" / wrong_digest.filename
    wrong_digest.write(b"data")
    with pytest.raises(AdapterError, match="checksum"):
        wrong_digest.seal()
    assert not staged_digest.exists()
    assert exact.list_artifacts() == []

    restore = exact.begin_restore_upload(declared_size=4)
    restore.write(b"data")
    receipt = restore.seal()
    assert receipt["kind"] == "RESTORE_UPLOAD"


def test_private_roots_reject_symlinks_and_registered_tamper_is_never_downloaded(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(real, target_is_directory=True)
    with pytest.raises(AdapterError, match="symlink"):
        OwnerDatabaseService(root_link)

    staged_root = tmp_path / "with-stage-link"
    staged_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (staged_root / ".staging").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AdapterError, match="staging"):
        OwnerDatabaseService(staged_root)

    safe_root = tmp_path / "safe"
    service = OwnerDatabaseService(safe_root)
    sink = service.begin_logical_import_upload(declared_size=4)
    sink.write(b"data")
    receipt = sink.seal()
    artifact = _only_registered_payload(safe_root)
    artifact.write_bytes(b"evil")
    with pytest.raises(AdapterError, match="checksum"):
        service.open_download(receipt["artifact_id"])


def test_upload_seal_rejects_staging_path_replacement_without_touching_symlink_target(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    service = OwnerDatabaseService(root)
    sink = service.begin_restore_upload(declared_size=4)
    sink.write(b"data")
    stage = root / ".staging" / sink.filename
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"keep")
    outside.chmod(0o640)

    stage.unlink()
    stage.symlink_to(outside)
    with pytest.raises(AdapterError, match="staging"):
        sink.seal()

    assert outside.read_bytes() == b"keep"
    assert _mode(outside) == 0o640
    assert service.list_artifacts() == []


def test_verified_download_is_path_free_bounded_and_delete_is_digest_bound(tmp_path: Path) -> None:
    service = OwnerDatabaseService(tmp_path / "artifacts")
    payload = b"download-me"
    sink = service.begin_logical_import_upload(declared_size=len(payload))
    sink.write(payload)
    receipt = sink.seal()

    handle = service.open_download(receipt["artifact_id"])
    try:
        assert handle.kind == "LOGICAL_IMPORT"
        assert handle.size_bytes == len(payload)
        assert handle.sha256 == receipt["sha256"]
        assert handle.filename.endswith(".json")
        assert not hasattr(handle, "artifact_id")
        assert not hasattr(handle, "path")
        with pytest.raises(AdapterError, match="bounded"):
            handle.read(DOWNLOAD_READ_MAX_BYTES + 1)
        assert handle.read(4) == payload[:4]
        assert handle.read(DOWNLOAD_READ_MAX_BYTES) == payload[4:]
        assert handle.read(1) == b""
    finally:
        handle.close()

    preview = service.preview_artifact_delete(receipt["artifact_id"])
    assert preview["artifact_id"] == receipt["artifact_id"]
    assert preview["sha256"] == receipt["sha256"]
    assert len(preview["preview_digest"]) == 64
    assert "path" not in preview and "relative_path" not in preview

    with pytest.raises(AdapterError, match="preview"):
        service.commit_artifact_delete(receipt["artifact_id"], "f" * 64)
    assert service.artifact_metadata(receipt["artifact_id"])["sha256"] == receipt["sha256"]

    deleted = service.commit_artifact_delete(receipt["artifact_id"], preview["preview_digest"])
    assert deleted["artifact_id"] == receipt["artifact_id"]
    assert service.list_artifacts() == []


def test_completed_artifacts_survive_service_restart_and_jobs_are_durable_private_and_path_free(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    active = tmp_path / "active.sqlite3"
    _active_database(active)
    service = OwnerDatabaseService(root, active_db_path=active)
    job = service.begin_backup_job()

    assert job["phase"] == "QUEUED"
    uuid.UUID(job["job_id"])
    uuid.UUID(job["operation_id"])
    result = asyncio.run(service.run_job(job["job_id"]))
    assert result["phase"] == "SUCCEEDED"
    assert result["kind"] == "BACKUP_CREATE"
    assert result["result"]["kind"] == "SQLITE_BACKUP"
    assert "relative_path" not in repr(result)

    ledger = root / ".factlane-database-jobs.json"
    assert ledger.is_file()
    assert _mode(ledger) == 0o600
    ledger_text = ledger.read_text(encoding="utf-8")
    assert "relative_path" not in ledger_text
    assert "grant_secret" not in ledger_text
    assert "csrf" not in ledger_text

    restarted = OwnerDatabaseService(root, active_db_path=active)
    assert restarted.job_status(job["job_id"]) == result
    assert len(restarted.list_artifacts()) == 1
    assert restarted.list_artifacts()[0]["artifact_id"] == result["result"]["artifact_id"]


def test_backup_create_job_awaits_injected_hook_with_exact_trusted_inputs(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    active = tmp_path / "active.sqlite3"
    _active_database(active)
    registry = ArtifactRegistry(root)
    calls: list[tuple[Path, ArtifactRegistry, str]] = []

    async def backup_create(active_db_path: Path, received_registry: ArtifactRegistry, artifact_id: str) -> None:
        await asyncio.sleep(0)
        calls.append((active_db_path, received_registry, artifact_id))
        create_consistent_backup(active_db_path, received_registry, artifact_id=artifact_id)

    service = OwnerDatabaseService(
        root,
        artifact_registry=registry,
        active_db_path=active,
        backup_create=backup_create,
    )
    job = service.begin_backup_job()

    result = asyncio.run(service.run_job(job["job_id"]))

    assert result["phase"] == "SUCCEEDED"
    assert calls == [(active.resolve(), registry, job["artifact_id"])]
    assert result["result"] == service.artifact_metadata(job["artifact_id"])
    assert result["result"]["kind"] == "SQLITE_BACKUP"


def test_backup_create_hook_failure_uses_existing_failed_job_path(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    active = tmp_path / "active.sqlite3"
    _active_database(active)
    registry = ArtifactRegistry(root)
    calls: list[tuple[Path, ArtifactRegistry, str]] = []

    async def backup_create(active_db_path: Path, received_registry: ArtifactRegistry, artifact_id: str) -> None:
        await asyncio.sleep(0)
        calls.append((active_db_path, received_registry, artifact_id))
        raise AdapterError("BACKUP_GATE_FAILED", "simulated governed backup failure")

    service = OwnerDatabaseService(
        root,
        artifact_registry=registry,
        active_db_path=active,
        backup_create=backup_create,
    )
    job = service.begin_backup_job()

    result = asyncio.run(service.run_job(job["job_id"]))

    assert calls == [(active.resolve(), registry, job["artifact_id"])]
    assert result["phase"] == "FAILED"
    assert result["error"] == {"code": "BACKUP_GATE_FAILED", "message": "database job failed"}
    assert service.list_artifacts() == []


def test_service_can_reuse_exact_injected_registry_and_rejects_root_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    registry = ArtifactRegistry(root)

    service = OwnerDatabaseService(root, artifact_registry=registry)

    assert service._registry is registry
    sink = service.begin_logical_import_upload(declared_size=2)
    sink.write(b"{}")
    receipt = sink.seal()
    assert registry.metadata(receipt["artifact_id"])["artifact_id"] == receipt["artifact_id"]

    with pytest.raises(AdapterError, match="registry"):
        OwnerDatabaseService(tmp_path / "other-artifacts", artifact_registry=registry)


def test_service_startup_reconciles_stale_running_jobs_as_interrupted_without_reexecution(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    active = tmp_path / "active.sqlite3"
    _active_database(active)
    service = OwnerDatabaseService(root, active_db_path=active)
    job = service.begin_backup_job()
    ledger = root / ".factlane-database-jobs.json"
    value = json.loads(ledger.read_text(encoding="utf-8"))
    value[job["job_id"]]["phase"] = "RUNNING"
    ledger.write_text(json.dumps(value), encoding="utf-8")
    ledger.chmod(0o600)

    restarted = OwnerDatabaseService(root, active_db_path=active)

    status = restarted.job_status(job["job_id"])
    assert status["phase"] == "FAILED"
    assert status["error"] == {"code": "JOB_INTERRUPTED", "message": "database job failed"}
    assert restarted.list_artifacts() == []


def test_job_ledger_rejects_symlink_replacement_instead_of_following_it(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    active = tmp_path / "active.sqlite3"
    _active_database(active)
    service = OwnerDatabaseService(root, active_db_path=active)
    job = service.begin_backup_job()
    ledger = root / ".factlane-database-jobs.json"
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    ledger.unlink()
    ledger.symlink_to(outside)
    with pytest.raises(AdapterError, match="ledger"):
        service.job_status(job["job_id"])
    assert outside.read_text(encoding="utf-8") == "{}"


def test_job_ledger_rejects_unknown_or_sensitive_fields_on_restart(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    active = tmp_path / "active.sqlite3"
    _active_database(active)
    service = OwnerDatabaseService(root, active_db_path=active)
    job = service.begin_backup_job()
    ledger = root / ".factlane-database-jobs.json"
    value = json.loads(ledger.read_text(encoding="utf-8"))
    value[job["job_id"]]["grant_secret"] = "must-never-be-readable"
    ledger.write_text(json.dumps(value), encoding="utf-8")
    ledger.chmod(0o600)

    with pytest.raises(AdapterError, match="ledger"):
        OwnerDatabaseService(root, active_db_path=active)


def test_job_ledger_binding_digest_detects_operation_identity_tamper(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    active = tmp_path / "active.sqlite3"
    _active_database(active)
    service = OwnerDatabaseService(root, active_db_path=active)
    job = service.begin_backup_job()
    ledger = root / ".factlane-database-jobs.json"
    value = json.loads(ledger.read_text(encoding="utf-8"))
    value[job["job_id"]]["operation_id"] = str(uuid.uuid4())
    ledger.write_text(json.dumps(value), encoding="utf-8")
    ledger.chmod(0o600)

    with pytest.raises(AdapterError, match="binding"):
        OwnerDatabaseService(root, active_db_path=active)


def test_import_and_restore_hooks_receive_exact_artifact_operation_binding_without_paths(tmp_path: Path) -> None:
    seen_import: list[tuple[bytes, dict[str, object]]] = []
    seen_restore_preview: list[dict[str, object]] = []
    seen_restore_apply: list[tuple[dict[str, object], dict[str, object]]] = []
    authorizations: list[str] = []

    async def import_hook(payload: bytes, binding: dict[str, object]) -> dict[str, object]:
        seen_import.append((payload, dict(binding)))
        return {"status": "READY", "record_count": 1}

    async def restore_preview_hook(binding: dict[str, object]) -> dict[str, object]:
        seen_restore_preview.append(dict(binding))
        return {
            "status": "READY",
            "operation": "FULL_RESTORE",
            "artifact_id": binding["artifact_id"],
            "artifact_sha256": binding["artifact_sha256"],
            "artifact_size_bytes": binding["artifact_size_bytes"],
            "trusted_origin_attested": binding["trusted_origin_attested"],
            "authorization_preview": f"restore:{binding['operation_id']}",
        }

    async def restore_apply_hook(binding, preview, authorize):
        seen_restore_apply.append((dict(binding), dict(preview)))
        authorize(preview["authorization_preview"])
        return {"status": "OK", "operation": "FULL_RESTORE"}

    service = OwnerDatabaseService(
        tmp_path / "artifacts",
        logical_import_preview=import_hook,
        logical_import_apply=import_hook,
        restore_preview=restore_preview_hook,
        restore_apply=restore_apply_hook,
    )
    logical_bytes = b'{"some":"logical-input"}'
    logical_sink = service.begin_logical_import_upload(declared_size=len(logical_bytes))
    logical_sink.write(logical_bytes)
    logical = logical_sink.seal()
    operation_id = str(uuid.uuid4())

    preview_job = service.begin_logical_import_preview_job(logical["artifact_id"], operation_id=operation_id)
    preview_result = asyncio.run(service.run_job(preview_job["job_id"]))
    assert preview_result["phase"] == "SUCCEEDED"
    assert seen_import[0][0] == logical_bytes
    binding = seen_import[0][1]
    assert binding["operation_id"] == operation_id
    assert binding["artifact_id"] == logical["artifact_id"]
    assert binding["artifact_sha256"] == logical["sha256"]
    assert "path" not in binding and "relative_path" not in binding

    commit_job = service.begin_logical_import_commit_job(logical["artifact_id"], operation_id=operation_id)
    asyncio.run(service.run_job(commit_job["job_id"]))
    assert seen_import[1][1]["operation_id"] == operation_id
    assert seen_import[1][1]["artifact_id"] == logical["artifact_id"]

    restore_bytes = _restore_sqlite_bytes(tmp_path)
    restore_sink = service.begin_restore_upload(declared_size=len(restore_bytes))
    restore_sink.write(restore_bytes)
    restore = restore_sink.seal()
    restore_operation = str(uuid.uuid4())
    restore_preview_job = service.begin_restore_preview_job(
        restore["artifact_id"],
        operation_id=restore_operation,
        trusted_origin_attested=True,
    )
    preview_status = asyncio.run(service.run_job(restore_preview_job["job_id"]))
    assert preview_status["phase"] == "SUCCEEDED"
    assert preview_status["artifact_kind"] == "SQLITE_BACKUP"
    assert service.artifact_metadata(restore["artifact_id"])["kind"] == "SQLITE_BACKUP"
    restore_commit_job = service.begin_restore_commit_job(
        restore["artifact_id"],
        operation_id=restore_operation,
        state_restoration_acknowledged=True,
    )
    with pytest.raises(AdapterError, match="acknowledgement"):
        service.begin_restore_commit_job(
            restore["artifact_id"],
            operation_id=restore_operation,
            state_restoration_acknowledged=False,
        )
    with pytest.raises(AdapterError, match="authorization"):
        asyncio.run(service.run_job(restore_commit_job["job_id"]))
    assert restore_commit_job["artifact_kind"] == "SQLITE_BACKUP"
    assert service.job_status(restore_commit_job["job_id"])["phase"] == "QUEUED"
    grant_secret = "grant-do-not-persist"

    def authorize(exact_preview: str) -> None:
        assert grant_secret == "grant-do-not-persist"
        authorizations.append(exact_preview)

    commit_status = asyncio.run(
        service.run_restore_job(
            restore_commit_job["job_id"],
            authorize=authorize,
        )
    )

    assert commit_status["phase"] == "SUCCEEDED"
    assert seen_restore_preview[0]["operation_id"] == restore_operation
    assert seen_restore_preview[0]["artifact_id"] == restore["artifact_id"]
    assert seen_restore_preview[0]["artifact_kind"] == "SQLITE_BACKUP"
    assert seen_restore_preview[0]["trusted_origin_attested"] is True
    assert "path" not in seen_restore_preview[0] and "relative_path" not in seen_restore_preview[0]
    apply_binding, exact_preview = seen_restore_apply[0]
    assert apply_binding["operation_id"] == restore_operation
    assert apply_binding["artifact_id"] == restore["artifact_id"]
    assert apply_binding["artifact_kind"] == "SQLITE_BACKUP"
    assert apply_binding["trusted_origin_attested"] is True
    assert apply_binding["state_restoration_acknowledged"] is True
    assert len(apply_binding["preview_digest"]) == 64
    assert exact_preview == preview_status["result"]
    assert authorizations == [exact_preview["authorization_preview"]]
    ledger_text = (tmp_path / "artifacts" / ".factlane-database-jobs.json").read_text(encoding="utf-8")
    assert "grant-do-not-persist" not in ledger_text


def test_restore_commit_restart_retry_requires_fresh_runtime_authorize_and_reuses_durable_preview(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    preview_calls: list[dict[str, object]] = []
    apply_calls: list[tuple[dict[str, object], dict[str, object]]] = []
    grants: list[str] = []

    async def preview_hook(binding: dict[str, object]) -> dict[str, object]:
        preview_calls.append(dict(binding))
        return {
            "status": "READY",
            "operation": "FULL_RESTORE",
            "artifact_id": binding["artifact_id"],
            "artifact_sha256": binding["artifact_sha256"],
            "artifact_size_bytes": binding["artifact_size_bytes"],
            "trusted_origin_attested": binding["trusted_origin_attested"],
            "authorization_preview": f"restore:{binding['operation_id']}",
        }

    async def apply_hook(binding, preview, authorize):
        apply_calls.append((dict(binding), dict(preview)))
        authorize(preview["authorization_preview"])
        return {"status": "OK", "operation": "FULL_RESTORE"}

    service = OwnerDatabaseService(root, restore_preview=preview_hook, restore_apply=apply_hook)
    payload = _restore_sqlite_bytes(tmp_path)
    sink = service.begin_restore_upload(declared_size=len(payload))
    sink.write(payload)
    artifact = sink.seal()
    operation_id = str(uuid.uuid4())
    preview_job = service.begin_restore_preview_job(
        artifact["artifact_id"],
        operation_id=operation_id,
        trusted_origin_attested=True,
    )
    preview_status = asyncio.run(service.run_job(preview_job["job_id"]))
    assert preview_status["phase"] == "SUCCEEDED"
    commit_job = service.begin_restore_commit_job(
        artifact["artifact_id"],
        operation_id=operation_id,
        state_restoration_acknowledged=True,
    )
    ledger_path = root / ".factlane-database-jobs.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger[commit_job["job_id"]]["phase"] = "RUNNING"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    ledger_path.chmod(0o600)

    restarted = OwnerDatabaseService(root, restore_preview=preview_hook, restore_apply=apply_hook)
    interrupted = restarted.job_status(commit_job["job_id"])
    assert interrupted["phase"] == "FAILED"
    assert interrupted["error"]["code"] == "JOB_INTERRUPTED"
    assert apply_calls == []

    grant_secret = "fresh-retry-grant-never-persist"

    def authorize(exact_preview: str) -> None:
        assert grant_secret.endswith("never-persist")
        grants.append(exact_preview)

    recovered = asyncio.run(restarted.run_restore_job(commit_job["job_id"], authorize=authorize))
    assert recovered["phase"] == "SUCCEEDED"
    assert len(apply_calls) == 1
    binding, preview = apply_calls[0]
    assert preview == preview_status["result"]
    assert binding["preview_digest"] == hashlib.sha256(
        json.dumps(preview, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert binding["state_restoration_acknowledged"] is True
    assert grants == [preview["authorization_preview"]]
    serialized = ledger_path.read_text(encoding="utf-8")
    assert "fresh-retry-grant-never-persist" not in serialized
    assert "authorize" not in serialized


def test_verified_restore_result_is_projected_before_generic_job_persistence(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    database_path = "/private/factlane/active.sqlite3"
    control_socket_path = "/private/factlane/runtime-a.sock"

    async def preview_hook(binding: dict[str, object]) -> dict[str, object]:
        return {
            "status": "READY",
            "operation": "FULL_RESTORE",
            "artifact_id": binding["artifact_id"],
            "artifact_sha256": binding["artifact_sha256"],
            "artifact_size_bytes": binding["artifact_size_bytes"],
            "trusted_origin_attested": binding["trusted_origin_attested"],
            "authorization_preview": f"restore:{binding['operation_id']}",
        }

    async def apply_hook(binding, preview, authorize):
        authorize(preview["authorization_preview"])
        return _verified_restore_result(
            binding,
            database_path=database_path,
            control_socket_path=control_socket_path,
        )

    service = OwnerDatabaseService(root, restore_preview=preview_hook, restore_apply=apply_hook)
    payload = _restore_sqlite_bytes(tmp_path)
    sink = service.begin_restore_upload(declared_size=len(payload))
    sink.write(payload)
    artifact = sink.seal()
    operation_id = str(uuid.uuid4())
    preview_job = service.begin_restore_preview_job(
        artifact["artifact_id"],
        operation_id=operation_id,
        trusted_origin_attested=True,
    )
    preview_status = asyncio.run(service.run_job(preview_job["job_id"]))
    assert preview_status["phase"] == "SUCCEEDED"
    commit_job = service.begin_restore_commit_job(
        artifact["artifact_id"],
        operation_id=operation_id,
        state_restoration_acknowledged=True,
    )

    result = asyncio.run(service.run_restore_job(commit_job["job_id"], authorize=lambda _preview: None))

    assert result["phase"] == "SUCCEEDED"
    assert result["result"] == {
        "status": "OK",
        "action": "FULL_RESTORE",
        "operation_id": operation_id,
        "generation": 2,
        "activation_device": 17,
        "activation_inode": 23,
        "pre_restore_artifact_id": f"pre-restore-{operation_id}",
        "ready_instance_ids": ["runtime-a"],
        "reconnect_failures": {"runtime-b": "UNREACHABLE"},
        "maintenance_participant_failures": {},
        "post_activation_verification": {
            "status": "VERIFIED",
            "bounded_read_verified": True,
            "storage_contract_version": 3,
            "embedding_profile": {"profile_id": "service-test", "store_key": "service-test"},
            "resumed_runtime_count": 1,
            "resumed_runtime_instance_ids": ["runtime-a"],
            "reconnect_failure_count": 1,
            "reconnect_failure_instance_ids": ["runtime-b"],
        },
    }
    serialized = (root / ".factlane-database-jobs.json").read_text(encoding="utf-8")
    assert database_path not in serialized
    assert control_socket_path not in serialized
    assert "database_path" not in serialized
    assert "control_socket_path" not in serialized
    assert "resumed_runtime_evidence" not in serialized


def test_restore_result_projection_rejects_unknown_fields_without_persisting_them(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"

    async def preview_hook(binding: dict[str, object]) -> dict[str, object]:
        return {
            "status": "READY",
            "operation": "FULL_RESTORE",
            "artifact_id": binding["artifact_id"],
            "artifact_sha256": binding["artifact_sha256"],
            "artifact_size_bytes": binding["artifact_size_bytes"],
            "trusted_origin_attested": binding["trusted_origin_attested"],
            "authorization_preview": f"restore:{binding['operation_id']}",
        }

    async def apply_hook(binding, preview, authorize):
        authorize(preview["authorization_preview"])
        return {**_verified_restore_result(binding), "records": [{"fact": "must-not-persist"}]}

    service = OwnerDatabaseService(root, restore_preview=preview_hook, restore_apply=apply_hook)
    payload = _restore_sqlite_bytes(tmp_path)
    sink = service.begin_restore_upload(declared_size=len(payload))
    sink.write(payload)
    artifact = sink.seal()
    operation_id = str(uuid.uuid4())
    preview_job = service.begin_restore_preview_job(
        artifact["artifact_id"],
        operation_id=operation_id,
        trusted_origin_attested=True,
    )
    asyncio.run(service.run_job(preview_job["job_id"]))
    commit_job = service.begin_restore_commit_job(
        artifact["artifact_id"],
        operation_id=operation_id,
        state_restoration_acknowledged=True,
    )

    result = asyncio.run(service.run_restore_job(commit_job["job_id"], authorize=lambda _preview: None))

    assert result["phase"] == "FAILED"
    assert result["error"] == {"code": "JOB_RESULT_UNSAFE", "message": "database job failed"}
    serialized = (root / ".factlane-database-jobs.json").read_text(encoding="utf-8")
    assert "must-not-persist" not in serialized
    assert '"records"' not in serialized


def test_restore_apply_recovery_error_still_uses_existing_failed_job_path(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"

    async def preview_hook(binding: dict[str, object]) -> dict[str, object]:
        return {
            "status": "READY",
            "operation": "FULL_RESTORE",
            "artifact_id": binding["artifact_id"],
            "artifact_sha256": binding["artifact_sha256"],
            "artifact_size_bytes": binding["artifact_size_bytes"],
            "trusted_origin_attested": binding["trusted_origin_attested"],
            "authorization_preview": f"restore:{binding['operation_id']}",
        }

    async def apply_hook(_binding, _preview, _authorize):
        raise AdapterError("RESTORE_RECOVERY_REQUIRED", "restore requires authenticated recovery")

    service = OwnerDatabaseService(root, restore_preview=preview_hook, restore_apply=apply_hook)
    payload = _restore_sqlite_bytes(tmp_path)
    sink = service.begin_restore_upload(declared_size=len(payload))
    sink.write(payload)
    artifact = sink.seal()
    operation_id = str(uuid.uuid4())
    preview_job = service.begin_restore_preview_job(
        artifact["artifact_id"],
        operation_id=operation_id,
        trusted_origin_attested=True,
    )
    asyncio.run(service.run_job(preview_job["job_id"]))
    commit_job = service.begin_restore_commit_job(
        artifact["artifact_id"],
        operation_id=operation_id,
        state_restoration_acknowledged=True,
    )

    result = asyncio.run(service.run_restore_job(commit_job["job_id"], authorize=lambda _preview: None))

    assert result["phase"] == "FAILED"
    assert result["error"] == {"code": "RESTORE_RECOVERY_REQUIRED", "message": "database job failed"}


def test_restore_preview_digest_mismatch_is_rejected_even_if_ledger_binding_is_rehashed(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"

    async def preview_hook(binding: dict[str, object]) -> dict[str, object]:
        return {
            "status": "READY",
            "operation": "FULL_RESTORE",
            "artifact_id": binding["artifact_id"],
            "artifact_sha256": binding["artifact_sha256"],
            "artifact_size_bytes": binding["artifact_size_bytes"],
            "trusted_origin_attested": binding["trusted_origin_attested"],
            "authorization_preview": f"restore:{binding['operation_id']}",
        }

    service = OwnerDatabaseService(root, restore_preview=preview_hook, restore_apply=lambda *_args: {"status": "OK"})
    payload = _restore_sqlite_bytes(tmp_path)
    sink = service.begin_restore_upload(declared_size=len(payload))
    sink.write(payload)
    artifact = sink.seal()
    operation_id = str(uuid.uuid4())
    preview_job = service.begin_restore_preview_job(
        artifact["artifact_id"],
        operation_id=operation_id,
        trusted_origin_attested=True,
    )
    asyncio.run(service.run_job(preview_job["job_id"]))
    ledger_path = root / ".factlane-database-jobs.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    record = ledger[preview_job["job_id"]]
    record["private_inputs"]["preview_digest"] = "f" * 64
    record["binding_digest"] = service._job_binding_digest(record)
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    ledger_path.chmod(0o600)

    with pytest.raises(AdapterError, match="preview"):
        OwnerDatabaseService(root, restore_preview=preview_hook, restore_apply=lambda *_args: {"status": "OK"})


def test_restore_commit_signature_accepts_acknowledgement_not_browser_preview() -> None:
    signature = inspect.signature(OwnerDatabaseService.begin_restore_commit_job)
    assert set(signature.parameters) == {
        "self",
        "artifact_id",
        "operation_id",
        "state_restoration_acknowledged",
    }


def test_hook_results_with_content_or_grants_fail_closed_without_persisting_sensitive_values(tmp_path: Path) -> None:
    async def unsafe_hook(_payload: bytes, _binding: dict[str, object]) -> dict[str, object]:
        return {"status": "READY", "content": "private-memory", "grant_secret": "grant-value"}

    service = OwnerDatabaseService(
        tmp_path / "artifacts",
        logical_import_preview=unsafe_hook,
    )
    sink = service.begin_logical_import_upload(declared_size=2)
    sink.write(b"{}")
    artifact = sink.seal()
    job = service.begin_logical_import_preview_job(artifact["artifact_id"])
    status = asyncio.run(service.run_job(job["job_id"]))

    assert status["phase"] == "FAILED"
    assert status["error"]["code"] == "JOB_RESULT_UNSAFE"
    serialized = (tmp_path / "artifacts" / ".factlane-database-jobs.json").read_text(encoding="utf-8")
    assert "private-memory" not in serialized
    assert "grant-value" not in serialized


def test_logical_export_requires_injected_selector_and_never_accepts_record_arrays(tmp_path: Path) -> None:
    service = OwnerDatabaseService(tmp_path / "no-selector")
    with pytest.raises(AdapterError, match="selector"):
        service.begin_logical_export_job(
            selection=_export_selection(),
        )
    with pytest.raises(AdapterError, match="exact"):
        OwnerDatabaseService(
            tmp_path / "selector-rejects-records",
            logical_export_selector=lambda _selection: [],
            logical_export_producer={
                "factlane_version": "0.1.7",
                "storage_contract_version": 3,
                "embedding_profile_id": "profile-a",
            },
        ).begin_logical_export_job(
            selection={**_export_selection(), "records": []},
        )
    with pytest.raises(AdapterError, match="exact"):
        OwnerDatabaseService(
            tmp_path / "selector-rejects-paths",
            logical_export_selector=lambda _selection: [],
            logical_export_producer={
                "factlane_version": "0.1.7",
                "storage_contract_version": 3,
                "embedding_profile_id": "profile-a",
            },
        ).begin_logical_export_job(
            selection={**_export_selection(), "source_path": "/tmp/nope"},
        )

    seen: list[dict[str, object]] = []

    async def selector(selection: dict[str, object]) -> list[dict[str, object]]:
        seen.append(dict(selection))
        return []

    producer = {
        "factlane_version": "0.1.7",
        "storage_contract_version": 3,
        "embedding_profile_id": "profile-a",
    }
    root = tmp_path / "with-selector"
    selected = OwnerDatabaseService(
        root,
        logical_export_selector=selector,
        logical_export_producer=producer,
    )
    job = selected.begin_logical_export_job(
        selection=_export_selection(),
    )
    assert "private_inputs" not in job
    ledger = json.loads((root / ".factlane-database-jobs.json").read_text(encoding="utf-8"))
    assert ledger[job["job_id"]]["private_inputs"] == {
        "selection": _export_selection(),
        "producer": producer,
    }
    assert "private_inputs" not in selected.job_status(job["job_id"])

    restarted = OwnerDatabaseService(
        root,
        logical_export_selector=selector,
    )
    status = asyncio.run(restarted.run_job(job["job_id"]))
    assert status["phase"] == "SUCCEEDED"
    assert status["result"]["kind"] == "LOGICAL_EXPORT"
    assert seen == [_export_selection()]
    assert restarted.artifact_metadata(status["result"]["artifact_id"])["record_count"] == 0

    signature = inspect.signature(OwnerDatabaseService.begin_logical_export_job)
    assert set(signature.parameters) == {"self", "selection", "operation_id"}


def test_job_binding_rechecks_artifact_checksum_before_hook_execution(tmp_path: Path) -> None:
    calls = 0

    async def hook(_payload: bytes, _binding: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "READY"}

    root = tmp_path / "artifacts"
    service = OwnerDatabaseService(root, logical_import_preview=hook)
    sink = service.begin_logical_import_upload(declared_size=4)
    sink.write(b"data")
    artifact = sink.seal()
    job = service.begin_logical_import_preview_job(artifact["artifact_id"])
    _only_registered_payload(root).write_bytes(b"evil")

    status = asyncio.run(service.run_job(job["job_id"]))
    assert status["phase"] == "FAILED"
    assert status["error"]["code"] == "ARTIFACT_CHECKSUM_MISMATCH"
    assert calls == 0
