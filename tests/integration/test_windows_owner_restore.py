from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

if sys.platform != "win32":
    pytest.skip("native Windows Owner restore custody qualification only", allow_module_level=True)

from factlane.contract import AdapterError
import factlane.owner_restore as owner_restore_module
from factlane.owner_database import ArtifactRegistry, create_consistent_backup
from factlane.owner_restore import FullRestoreService
from factlane.windows_maintenance import (
    WindowsExclusiveMaintenanceLease,
    WindowsMaintenanceError,
    WindowsRuntimeDBLease,
)
from factlane.windows_owner_restore import WindowsOwnerRestoreOperator
from factlane.windows_security import (
    current_user_sid,
    ensure_owner_private_directory,
    protect_owner_private_path,
    validate_owner_private_path,
)


_OPERATION_ID = "11111111-1111-4111-8111-111111111111"


def _service(*, active_db: Path, state_dir: Path) -> FullRestoreService:
    return FullRestoreService(
        active_db=active_db,
        registry=object(),  # type: ignore[arg-type]
        state_dir=state_dir,
        service_state=object(),
    )


def _db(path: Path, value: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO adapter_meta(key,value) VALUES ('contract_version','3')")
        conn.execute("CREATE TABLE facts(value TEXT NOT NULL)")
        conn.execute("INSERT INTO facts(value) VALUES (?)", (value,))
        conn.commit()
    finally:
        conn.close()


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


def test_windows_restore_state_and_journal_are_owner_private_across_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "restore-state"
    service = _service(active_db=tmp_path / "active.db", state_dir=state_dir)
    owner_sid = current_user_sid()
    validate_owner_private_path(state_dir, owner_sid=owner_sid, directory=True)

    original_validate_handle = owner_restore_module._windows_validate_private_handle
    creation_time_private: list[str] = []

    def observe_private_handle(handle, *, message: str) -> None:
        original_validate_handle(handle, message=message)
        if "journal staging file" in message:
            creation_time_private.append(message)

    monkeypatch.setattr(owner_restore_module, "_windows_validate_private_handle", observe_private_handle)

    first = service._journal(_OPERATION_ID, phase="PREPARED", attempt=1)
    assert first["phase"] == "PREPARED"
    journal_path = service._journal_path(_OPERATION_ID)
    validate_owner_private_path(journal_path, owner_sid=owner_sid, directory=False)
    assert service.read_journal(_OPERATION_ID)["attempt"] == 1

    second = service._journal(_OPERATION_ID, phase="RETRY", attempt=2)
    assert second["phase"] == "RETRY"
    validate_owner_private_path(journal_path, owner_sid=owner_sid, directory=False)
    assert service.read_journal(_OPERATION_ID)["attempt"] == 2
    assert len(creation_time_private) >= 2


def test_windows_restore_rejects_unsafe_existing_state_and_journal_acl(tmp_path: Path) -> None:
    owner_sid = current_user_sid()
    unsafe_state = tmp_path / "unsafe-state"
    ensure_owner_private_directory(unsafe_state, owner_sid=owner_sid)
    _set_unsafe_acl(unsafe_state)

    with pytest.raises(AdapterError) as state_error:
        _service(active_db=tmp_path / "active.db", state_dir=unsafe_state)
    assert state_error.value.code == "RESTORE_STATE_UNAVAILABLE"
    assert list(unsafe_state.iterdir()) == []

    service = _service(active_db=tmp_path / "active-2.db", state_dir=tmp_path / "safe-state")
    service._journal(_OPERATION_ID, phase="PREPARED")
    journal_path = service._journal_path(_OPERATION_ID)
    before = journal_path.read_bytes()
    _set_unsafe_acl(journal_path)

    with pytest.raises(AdapterError) as journal_error:
        service.read_journal(_OPERATION_ID)
    assert journal_error.value.code == "RESTORE_STATE_UNAVAILABLE"
    assert journal_path.read_bytes() == before


def test_windows_restore_rejects_ancestor_junction_before_state_mutation(tmp_path: Path) -> None:
    target = tmp_path / "junction-target"
    target.mkdir()
    junction = tmp_path / "restore-junction"
    _create_junction(junction, target)
    try:
        with pytest.raises(AdapterError) as caught:
            _service(
                active_db=tmp_path / "active.db",
                state_dir=junction / "nested" / "restore-state",
            )
        assert caught.value.code == "RESTORE_STATE_UNAVAILABLE"
        assert list(target.iterdir()) == []
    finally:
        os.rmdir(junction)


def test_windows_restore_stage_is_owner_private_at_creation_and_on_recreate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(b"restore-stage-payload")
    stage = tmp_path / ".active.db.restore-stage.stage"
    owner_sid = current_user_sid()
    original_validate_handle = owner_restore_module._windows_validate_private_handle
    creation_time_private: list[str] = []

    def observe_private_handle(handle, *, message: str) -> None:
        original_validate_handle(handle, message=message)
        if "restore staging file" in message:
            creation_time_private.append(message)

    monkeypatch.setattr(owner_restore_module, "_windows_validate_private_handle", observe_private_handle)

    assert owner_restore_module._windows_create_restore_stage(source, stage) == stage
    assert stage.read_bytes() == source.read_bytes()
    validate_owner_private_path(stage, owner_sid=owner_sid, directory=False)
    stage.unlink()

    assert owner_restore_module._windows_create_restore_stage(source, stage) == stage
    assert stage.read_bytes() == source.read_bytes()
    validate_owner_private_path(stage, owner_sid=owner_sid, directory=False)
    assert len(creation_time_private) >= 2


def test_windows_restore_stage_collision_is_zero_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(b"replacement")
    stage = tmp_path / ".active.db.restore-collision.stage"
    stage.write_bytes(b"existing-stage")
    protect_owner_private_path(stage, owner_sid=current_user_sid(), directory=False)
    before = stage.read_bytes()

    with pytest.raises(AdapterError) as caught:
        owner_restore_module._windows_create_restore_stage(source, stage)
    assert caught.value.code == "RESTORE_STAGE_COLLISION"
    assert stage.read_bytes() == before


def test_windows_restore_journal_parent_swap_to_junction_cannot_redirect_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "restore-state"
    redirect = tmp_path / "redirect"
    moved = tmp_path / "moved-state"
    redirect.mkdir()
    service = _service(active_db=tmp_path / "active.db", state_dir=state_dir)
    original_rename = owner_restore_module._windows_rename_handle_replace_relative
    journal_name = f"restore-{_OPERATION_ID}.json"
    swap = {"attempted": False, "blocked": False, "renamed": False}

    def inject_parent_swap(source_handle, parent_handle, destination: Path) -> None:
        swap["attempted"] = True
        try:
            os.rename(state_dir, moved)
        except OSError:
            swap["blocked"] = True
        else:
            swap["renamed"] = True
            _create_junction(state_dir, redirect)
        original_rename(source_handle, parent_handle, destination)

    monkeypatch.setattr(owner_restore_module, "_windows_rename_handle_replace_relative", inject_parent_swap)
    try:
        if os.name != "nt":
            pytest.fail("native Windows qualification only")
        try:
            result = service._journal(_OPERATION_ID, phase="PREPARED")
        except AdapterError as caught:
            assert swap["renamed"] is True
            assert caught.code == "RESTORE_STATE_UNAVAILABLE"
            assert (moved / journal_name).exists()
        else:
            assert swap["blocked"] is True
            assert result["phase"] == "PREPARED"
            assert (state_dir / journal_name).exists()
        assert swap["attempted"] is True
        assert list(redirect.iterdir()) == []
        assert not (redirect / journal_name).exists()
    finally:
        if moved.exists():
            if state_dir.exists():
                os.rmdir(state_dir)
            os.rename(moved, state_dir)


def test_windows_restore_stage_parent_swap_to_junction_cannot_redirect_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    redirect = tmp_path / "redirect"
    redirect.mkdir()
    moved = tmp_path / "moved-work"
    source = work / "source.db"
    payload = b"stage-parent-substitution" * 1024
    source.write_bytes(payload)
    stage = work / ".active.db.restore-race.stage"
    original_verify = owner_restore_module._windows_verify_pinned_parent
    swap_succeeded = False

    def inject_parent_swap(parent, handle, identity, *, message: str) -> None:
        nonlocal swap_succeeded
        if not swap_succeeded and message == "restore staging parent changed during creation":
            os.rename(work, moved)
            swap_succeeded = True
            _create_junction(work, redirect)
        original_verify(parent, handle, identity, message=message)

    monkeypatch.setattr(owner_restore_module, "_windows_verify_pinned_parent", inject_parent_swap)
    try:
        with pytest.raises(AdapterError) as caught:
            owner_restore_module._windows_create_restore_stage(source, stage)
        assert caught.value.code == "RESTORE_STATE_UNAVAILABLE"
        assert swap_succeeded is True
        assert list(redirect.iterdir()) == []
        assert (moved / stage.name).read_bytes() == payload
        assert not (redirect / stage.name).exists()
    finally:
        if moved.exists():
            if work.exists():
                os.rmdir(work)
            os.rename(moved, work)


def test_windows_restore_operator_inplace_snapshot_activation_and_runtime_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_sid = current_user_sid()
    managed = tmp_path / "managed"
    ensure_owner_private_directory(managed, owner_sid=owner_sid)
    active = managed / "active.db"
    replacement_source = managed / "replacement-source.db"
    _db(active, "current")
    _db(replacement_source, "replacement")
    protect_owner_private_path(active, owner_sid=owner_sid, directory=False)
    protect_owner_private_path(replacement_source, owner_sid=owner_sid, directory=False)
    setup = sqlite3.connect(active)
    try:
        assert setup.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        setup.execute("PRAGMA wal_autocheckpoint=0")
        setup.execute("UPDATE facts SET value='committed-in-wal'")
        setup.commit()
        wal = Path(str(active) + "-wal")
        shm = Path(str(active) + "-shm")
        assert wal.stat().st_size > 0
        saved_main = managed / "saved-main.db"
        saved_wal = managed / "saved-main.db-wal"
        saved_shm = managed / "saved-main.db-shm"
        shutil.copyfile(active, saved_main)
        shutil.copyfile(wal, saved_wal)
        if shm.exists():
            shutil.copyfile(shm, saved_shm)
    finally:
        setup.close()
    shutil.copyfile(saved_main, active)
    shutil.copyfile(saved_wal, Path(str(active) + "-wal"))
    if saved_shm.exists():
        shutil.copyfile(saved_shm, Path(str(active) + "-shm"))
    registry = ArtifactRegistry(managed / "artifacts")
    replacement = create_consistent_backup(replacement_source, registry, artifact_id="replacement")
    replacement_path = registry.managed_path(replacement["relative_path"])
    state_dir = managed / "restore-state"
    ensure_owner_private_directory(state_dir, owner_sid=owner_sid)
    before_identity = active.stat().st_ino

    def forbidden_promotion(*_args, **_kwargs):
        pytest.fail("Windows full restore must never invoke rename/promotion maintenance")

    monkeypatch.setattr(WindowsExclusiveMaintenanceLease, "promote_replacement", forbidden_promotion)
    monkeypatch.setattr(
        WindowsExclusiveMaintenanceLease,
        "_rename_handle_replace",
        staticmethod(forbidden_promotion),
    )

    operator = WindowsOwnerRestoreOperator(
        active_db=active,
        state_dir=state_dir,
        registry=registry,
        operation_id=_OPERATION_ID,
    )
    with operator:
        snapshot, snapshot_receipt = operator.capture_pre_restore_backup(
            artifact_id=f"pre-restore-{_OPERATION_ID}"
        )
        assert snapshot["kind"] == "SQLITE_BACKUP"
        assert snapshot_receipt["active_identity"]

        stage = owner_restore_module._windows_create_restore_stage(
            replacement_path,
            managed / f".{active.name}.restore-{_OPERATION_ID}.stage",
        )
        owner_restore_module._validate_sqlite_image(stage)
        owner_restore_module._write_interlock(stage, _OPERATION_ID)
        intent = operator.prepare_activation(stage)
        moved_stage = managed / "stage-substitution-attempt.db"
        with pytest.raises(OSError):
            os.replace(stage, moved_stage)
        assert stage.exists()
        assert not moved_stage.exists()
        proof = operator.activate_and_verify(intent)

        assert active.stat().st_ino == before_identity
        assert proof["active_identity"] == intent["active_identity"]
        assert proof["interlock_present"] is True
        assert not stage.exists()

        handoff = operator.prepare_runtime_handoff()
        assert handoff == {
            "journal_mode": "wal",
            "synchronous": 1,
            "busy_timeout_ms": 5000,
            "locking_mode": "normal",
        }
        with pytest.raises(WindowsMaintenanceError) as blocked:
            WindowsRuntimeDBLease.open(active)
        assert blocked.value.code == "MAINTENANCE_IN_PROGRESS"

        operator.clear_interlock()
        cleared = operator.verify_active(intent, proof, interlock_required=False)
        assert cleared["interlock_present"] is False

    runtime = WindowsRuntimeDBLease.open(active)
    runtime.close()
    active_conn = sqlite3.connect(active)
    try:
        assert active_conn.execute("SELECT value FROM facts").fetchone() == ("replacement",)
        assert active_conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        active_conn.close()
    snapshot_path = registry.managed_path(snapshot["relative_path"])
    snapshot_conn = sqlite3.connect(snapshot_path)
    try:
        assert snapshot_conn.execute("SELECT value FROM facts").fetchone() == ("committed-in-wal",)
    finally:
        snapshot_conn.close()
