from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

if sys.platform != "win32":
    pytest.skip("native Windows Owner database custody qualification only", allow_module_level=True)

from factlane.contract import AdapterError
import factlane.owner_database as owner_database_module
from factlane.owner_database import (
    ArtifactRegistry,
    create_consistent_backup,
    create_registered_logical_export,
    verify_registered_backup,
)
from factlane.owner_database_service import OwnerDatabaseService
from factlane.windows_security import (
    current_user_sid,
    protect_owner_private_path,
    validate_owner_private_path,
)


def _set_unsafe_acl(path: Path) -> None:
    import win32security

    owner_sid = current_user_sid()
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{owner_sid}D:P(A;;GA;;;{owner_sid})(A;;GR;;;WD)",
        win32security.SDDL_REVISION_1,
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | getattr(win32security, "PROTECTED_DACL_SECURITY_INFORMATION", 0x80000000),
        descriptor.GetSecurityDescriptorOwner(),
        None,
        descriptor.GetSecurityDescriptorDacl(),
        None,
    )


def _create_junction(junction: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _attempt_parent_rename_junction(parent: Path, moved: Path, redirect: Path) -> bool:
    try:
        os.replace(parent, moved)
    except OSError:
        return False
    _create_junction(parent, redirect)
    return True


def _restore_substituted_parent(parent: Path, moved: Path) -> None:
    if moved.exists():
        if parent.exists() or parent.is_symlink():
            os.rmdir(parent)
        os.replace(moved, parent)


def _attempt_leaf_substitution(source: Path, moved: Path, decoy: bytes) -> bool:
    try:
        os.replace(source, moved)
    except OSError:
        return False
    source.write_bytes(decoy)
    return True


def _active_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO adapter_meta VALUES ('contract_version', '3')")
    conn.execute(
        "INSERT INTO adapter_meta VALUES ('embedding_profile', ?)",
        (json.dumps({"profile_id": "windows-owner-db", "store_key": "windows-owner-db"}),),
    )
    conn.execute("CREATE TABLE facts (value TEXT NOT NULL)")
    conn.execute("INSERT INTO facts VALUES ('main')")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("INSERT INTO facts VALUES ('committed-in-wal')")
    conn.commit()
    return conn


def _export_selection() -> dict[str, object]:
    return {
        "scope": "PROJECT",
        "project_id": "factlane",
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "lifecycle_states": ["CANDIDATE"],
        "max_records": 100,
    }


def test_windows_artifact_crud_jobs_and_logical_publication_are_owner_private(tmp_path: Path) -> None:
    async def import_preview(_payload: bytes, _binding: dict[str, object]) -> dict[str, object]:
        return {"status": "READY", "record_count": 0}

    async def import_apply(_payload: bytes, _binding: dict[str, object]) -> dict[str, object]:
        return {"status": "OK", "imported_count": 0}

    async def export_selector(_selection: dict[str, object]) -> list[dict[str, object]]:
        return []

    producer = {
        "factlane_version": "0.1.7",
        "storage_contract_version": 3,
        "embedding_profile_id": "windows-owner-db",
    }
    root = tmp_path / "artifacts"
    service = OwnerDatabaseService(
        root,
        logical_export_selector=export_selector,
        logical_export_producer=producer,
        logical_import_preview=import_preview,
        logical_import_apply=import_apply,
    )
    sid = current_user_sid()
    validate_owner_private_path(root, owner_sid=sid, directory=True)
    validate_owner_private_path(root / ".staging", owner_sid=sid, directory=True)

    payload = b'{"format":"windows-custody-fixture"}'
    sink = service.begin_logical_import_upload(declared_size=len(payload))
    validate_owner_private_path(root / ".staging" / sink.filename, owner_sid=sid, directory=False)
    sink.write(payload)
    imported = sink.seal()
    import_entry = service._registry.get(imported["artifact_id"])
    import_path = service._registry.managed_path(import_entry["relative_path"])
    validate_owner_private_path(import_path, owner_sid=sid, directory=False)
    validate_owner_private_path(root / ".factlane-artifacts.json", owner_sid=sid, directory=False)
    assert service.list_artifacts() == [imported]

    with service.open_download(imported["artifact_id"]) as download:
        assert download.read() == payload
        assert download.read() == b""

    preview_job = service.begin_logical_import_preview_job(imported["artifact_id"])
    assert asyncio.run(service.run_job(preview_job["job_id"]))["phase"] == "SUCCEEDED"
    commit_job = service.begin_logical_import_commit_job(
        imported["artifact_id"],
        operation_id=preview_job["operation_id"],
    )
    assert asyncio.run(service.run_job(commit_job["job_id"]))["phase"] == "SUCCEEDED"

    export_job = service.begin_logical_export_job(selection=_export_selection())
    export_status = asyncio.run(service.run_job(export_job["job_id"]))
    assert export_status["phase"] == "SUCCEEDED"
    export_entry = service._registry.get(export_status["result"]["artifact_id"])
    export_path = service._registry.managed_path(export_entry["relative_path"])
    validate_owner_private_path(export_path, owner_sid=sid, directory=False)
    validate_owner_private_path(root / ".factlane-database-jobs.json", owner_sid=sid, directory=False)

    delete_preview = service.preview_artifact_delete(imported["artifact_id"])
    deleted = service.commit_artifact_delete(imported["artifact_id"], delete_preview["preview_digest"])
    assert deleted["artifact_id"] == imported["artifact_id"]
    assert not import_path.exists()
    assert [item["artifact_id"] for item in service.list_artifacts()] == [export_entry["artifact_id"]]


def test_windows_consistent_backup_captures_committed_wal_and_is_owner_private(tmp_path: Path) -> None:
    source = tmp_path / "active.db"
    conn = _active_database(source)
    try:
        wal = Path(str(source) + "-wal")
        assert wal.stat().st_size > 0
        registry = ArtifactRegistry(tmp_path / "artifacts")
        receipt = create_consistent_backup(source, registry, artifact_id="backup-wal")
        backup = registry.managed_path(receipt["relative_path"])

        sid = current_user_sid()
        validate_owner_private_path(registry.root, owner_sid=sid, directory=True)
        validate_owner_private_path(backup, owner_sid=sid, directory=False)
        validate_owner_private_path(registry.root / ".factlane-artifacts.json", owner_sid=sid, directory=False)
        verification = verify_registered_backup(registry, "backup-wal")
        assert verification["integrity_check"] == "ok"
        assert verification["storage_contract_version"] == 3
        with sqlite3.connect(backup) as restored:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert restored.execute("SELECT value FROM facts ORDER BY rowid").fetchall() == [
                ("main",),
                ("committed-in-wal",),
            ]
    finally:
        conn.close()


def test_windows_unsafe_existing_acl_fails_before_registry_or_job_mutation(tmp_path: Path) -> None:
    sid = current_user_sid()
    unsafe_root = tmp_path / "unsafe-root"
    unsafe_root.mkdir()
    protect_owner_private_path(unsafe_root, owner_sid=sid, directory=True)
    _set_unsafe_acl(unsafe_root)
    with pytest.raises(AdapterError) as root_error:
        OwnerDatabaseService(unsafe_root)
    assert root_error.value.code == "DATABASE_SERVICE_PATH_INVALID"
    assert list(unsafe_root.iterdir()) == []

    artifact_root = tmp_path / "artifact-state"
    artifact_service = OwnerDatabaseService(artifact_root)
    payload = b"owner-private"
    sink = artifact_service.begin_logical_import_upload(declared_size=len(payload))
    sink.write(payload)
    artifact = sink.seal()
    registry_path = artifact_root / ".factlane-artifacts.json"
    registry_before = registry_path.read_bytes()
    _set_unsafe_acl(registry_path)
    with pytest.raises(AdapterError) as registry_error:
        artifact_service.list_artifacts()
    assert registry_error.value.code == "ARTIFACT_REGISTRY_INVALID"
    assert registry_path.read_bytes() == registry_before
    protect_owner_private_path(registry_path, owner_sid=sid, directory=False)

    entry = artifact_service._registry.get(artifact["artifact_id"])
    artifact_path = artifact_service._registry.managed_path(entry["relative_path"])
    artifact_before = artifact_path.read_bytes()
    _set_unsafe_acl(artifact_path)
    with pytest.raises(AdapterError) as artifact_error:
        artifact_service.artifact_metadata(artifact["artifact_id"])
    assert artifact_error.value.code == "ARTIFACT_PATH_INVALID"
    assert artifact_path.read_bytes() == artifact_before

    root = tmp_path / "job-state"
    service = OwnerDatabaseService(
        root,
        logical_export_selector=lambda _selection: [],
        logical_export_producer={
            "factlane_version": "0.1.7",
            "storage_contract_version": 3,
            "embedding_profile_id": "windows-owner-db",
        },
    )
    job = service.begin_logical_export_job(selection=_export_selection())
    ledger = root / ".factlane-database-jobs.json"
    before = ledger.read_bytes()
    _set_unsafe_acl(ledger)
    with pytest.raises(AdapterError) as ledger_error:
        OwnerDatabaseService(root)
    assert ledger_error.value.code == "JOB_LEDGER_INVALID"
    assert ledger.read_bytes() == before
    assert json.loads(before.decode("utf-8"))[job["job_id"]]["phase"] == "QUEUED"


def test_windows_reparse_artifact_root_is_rejected_before_target_mutation(tmp_path: Path) -> None:
    target = tmp_path / "junction-target"
    target.mkdir()
    junction = tmp_path / "artifact-junction"
    _create_junction(junction, target)
    try:
        with pytest.raises(AdapterError) as exc_info:
            ArtifactRegistry(junction)
        assert exc_info.value.code == "ARTIFACT_PATH_INVALID"
        assert list(target.iterdir()) == []
    finally:
        os.rmdir(junction)


def test_windows_ancestor_junction_is_rejected_before_artifact_or_service_root_creation(tmp_path: Path) -> None:
    target = tmp_path / "junction-target"
    target.mkdir()
    junction = tmp_path / "artifact-ancestor"
    _create_junction(junction, target)
    try:
        nested = junction / "managed" / "artifacts"
        with pytest.raises(AdapterError) as registry_error:
            ArtifactRegistry(nested)
        assert registry_error.value.code == "ARTIFACT_PATH_INVALID"
        assert list(target.iterdir()) == []

        with pytest.raises(AdapterError) as service_error:
            OwnerDatabaseService(nested)
        assert service_error.value.code == "DATABASE_SERVICE_CONFIG_INVALID"
        assert list(target.iterdir()) == []
    finally:
        os.rmdir(junction)


def test_windows_new_private_file_has_owner_dacl_at_kernel_creation_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    service = OwnerDatabaseService(root)
    sid = current_user_sid()
    original_protect = owner_database_module.protect_owner_private_path
    observed_secure_before_post_create_protect: list[Path] = []

    def verify_before_protect(path, *, owner_sid: str, directory: bool) -> None:
        candidate = Path(path)
        if not directory and candidate.name.startswith(".factlane-artifacts-"):
            validate_owner_private_path(candidate, owner_sid=sid, directory=False)
            observed_secure_before_post_create_protect.append(candidate)
        original_protect(path, owner_sid=owner_sid, directory=directory)

    monkeypatch.setattr(owner_database_module, "protect_owner_private_path", verify_before_protect)
    payload = b"creation-time-owner-dacl"
    sink = service.begin_logical_import_upload(declared_size=len(payload))
    sink.write(payload)
    sink.seal()
    assert observed_secure_before_post_create_protect


def test_windows_artifact_create_parent_guard_blocks_rename_junction_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    service = OwnerDatabaseService(root)
    staging = root / ".staging"
    moved = tmp_path / "staging-moved"
    redirect = tmp_path / "staging-redirect"
    redirect.mkdir()
    original_open_guard = owner_database_module._open_windows_parent_guard
    substitution_attempts: list[bool] = []

    def open_guard_with_parent_race(path: Path, *, code: str, message: str):
        guard = original_open_guard(path, code=code, message=message)
        if path.parent == staging and not substitution_attempts:
            substitution_attempts.append(_attempt_parent_rename_junction(staging, moved, redirect))
        return guard

    monkeypatch.setattr(owner_database_module, "_open_windows_parent_guard", open_guard_with_parent_race)
    sink = None
    error = None
    try:
        try:
            sink = service.begin_restore_upload(declared_size=4)
        except AdapterError as exc:
            error = exc
        assert len(substitution_attempts) == 1
        if substitution_attempts[0]:
            assert error is not None and error.code == "UPLOAD_STAGE_INVALID"
        else:
            assert error is None and sink is not None
            assert (staging / sink.filename).exists()
        assert list(redirect.iterdir()) == []
    finally:
        if sink is not None:
            sink.abort()
        _restore_substituted_parent(staging, moved)


def test_windows_no_replace_publication_parent_guard_rejects_rename_junction_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    service = OwnerDatabaseService(root)
    sink = service.begin_restore_upload(declared_size=4)
    sink.write(b"data")
    staging = root / ".staging"
    stage = staging / sink.filename
    moved = tmp_path / "staging-moved"
    redirect = tmp_path / "staging-redirect"
    redirect.mkdir()
    redirected_source = redirect / sink.filename
    redirected_source.write_bytes(b"evil")
    original_move = owner_database_module._windows_move_file
    substitution_attempts: list[bool] = []

    def move_with_parent_race(source, destination, **kwargs):
        if not kwargs["replace"] and Path(source) == stage and not substitution_attempts:
            substitution_attempts.append(_attempt_parent_rename_junction(staging, moved, redirect))
        return original_move(source, destination, **kwargs)

    monkeypatch.setattr(owner_database_module, "_windows_move_file", move_with_parent_race)
    artifact = None
    error = None
    try:
        try:
            artifact = sink.seal()
        except AdapterError as exc:
            error = exc
        assert len(substitution_attempts) == 1
        if substitution_attempts[0]:
            assert error is not None and error.code == "UPLOAD_STAGE_INVALID"
            assert service.list_artifacts() == []
        else:
            assert error is None and artifact is not None
            entry = service._registry.get(artifact["artifact_id"])
            assert service._registry.managed_path(entry["relative_path"]).read_bytes() == b"data"
        assert redirected_source.read_bytes() == b"evil"
    finally:
        _restore_substituted_parent(staging, moved)
        if stage.exists():
            stage.unlink()


def test_windows_replace_publication_parent_guard_rejects_rename_junction_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    registry = ArtifactRegistry(root)
    create_registered_logical_export(
        [],
        registry,
        factlane_version="0.1.7",
        storage_contract_version=3,
        embedding_profile_id="windows-owner-db",
        artifact_id="before-race",
    )
    registry_path = root / ".factlane-artifacts.json"
    registry_before = registry_path.read_bytes()
    moved = tmp_path / "artifacts-moved"
    redirect = tmp_path / "artifacts-redirect"
    redirect.mkdir()
    original_move = owner_database_module._windows_move_file
    substitution_attempts: list[bool] = []

    def move_with_parent_race(source, destination, **kwargs):
        if kwargs["replace"] and Path(destination) == registry_path and not substitution_attempts:
            substitution_attempts.append(_attempt_parent_rename_junction(root, moved, redirect))
        return original_move(source, destination, **kwargs)

    monkeypatch.setattr(owner_database_module, "_windows_move_file", move_with_parent_race)
    created = None
    error = None
    try:
        try:
            created = create_registered_logical_export(
                [],
                registry,
                factlane_version="0.1.7",
                storage_contract_version=3,
                embedding_profile_id="windows-owner-db",
                artifact_id="during-race",
            )
        except AdapterError as exc:
            error = exc
        assert len(substitution_attempts) == 1
        if substitution_attempts[0]:
            assert error is not None and error.code == "ARTIFACT_REGISTRY_INVALID"
            assert registry_path.read_bytes() == registry_before
        else:
            assert error is None and created is not None
            assert registry.get("during-race")["artifact_id"] == "during-race"
        assert list(redirect.iterdir()) == []
    finally:
        _restore_substituted_parent(root, moved)

    orphan = root / "export-during-race.json"
    if orphan.exists():
        orphan.unlink()
    for stage in root.glob(".factlane-artifacts-*.tmp"):
        stage.unlink()


def test_windows_publication_keeps_source_leaf_handle_bound_through_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    service = OwnerDatabaseService(root)
    sink = service.begin_restore_upload(declared_size=4)
    sink.write(b"data")
    stage = root / ".staging" / sink.filename
    moved = tmp_path / "source-moved.sqlite3"
    original_move = owner_database_module._windows_move_file
    substitution_attempts: list[bool] = []

    def move_with_leaf_race(source, destination, **kwargs):
        if not kwargs["replace"] and Path(source) == stage and not substitution_attempts:
            substitution_attempts.append(_attempt_leaf_substitution(stage, moved, b"evil"))
        return original_move(source, destination, **kwargs)

    monkeypatch.setattr(owner_database_module, "_windows_move_file", move_with_leaf_race)
    artifact = None
    error = None
    try:
        try:
            artifact = sink.seal()
        except AdapterError as exc:
            error = exc
        assert len(substitution_attempts) == 1
        if substitution_attempts[0]:
            assert error is not None
            assert stage.read_bytes() == b"evil"
            assert service.list_artifacts() == []
        else:
            assert error is None and artifact is not None
            entry = service._registry.get(artifact["artifact_id"])
            published = service._registry.managed_path(entry["relative_path"])
            assert published.read_bytes() == b"data"
    finally:
        if stage.exists():
            stage.unlink()
        if moved.exists():
            moved.unlink()


def test_windows_no_replace_publication_does_not_overwrite_racing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    service = OwnerDatabaseService(root)
    sink = service.begin_restore_upload(declared_size=4)
    sink.write(b"data")
    original_move = owner_database_module._windows_move_file
    racing_destination: Path | None = None

    def move_with_destination_race(source, destination, **kwargs):
        nonlocal racing_destination
        if not kwargs["replace"] and racing_destination is None:
            racing_destination = Path(destination)
            racing_destination.write_bytes(b"keep")
        return original_move(source, destination, **kwargs)

    monkeypatch.setattr(owner_database_module, "_windows_move_file", move_with_destination_race)
    with pytest.raises(FileExistsError):
        sink.seal()

    assert racing_destination is not None
    assert racing_destination.read_bytes() == b"keep"
    assert service.list_artifacts() == []
