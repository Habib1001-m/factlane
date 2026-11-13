from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import factlane.windows_maintenance as windows_maintenance
from factlane.windows_maintenance import (
    WindowsBarrierGuard,
    WindowsExclusiveMaintenanceLease,
    WindowsInPlaceMaintenanceLease,
    WindowsMigrationArtifactCustody,
    WindowsMaintenanceError,
    WindowsRuntimeDBFamilyCustody,
    WindowsRuntimeDBLease,
    WindowsStagedMigrationLease,
)


def test_windows_maintenance_import_is_safe_off_windows(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("non-Windows fail-closed qualification")
    with pytest.raises(WindowsMaintenanceError) as caught:
        WindowsRuntimeDBLease.open(tmp_path / "memory.db")
    assert caught.value.code == "WINDOWS_MAINTENANCE_UNAVAILABLE"
    assert caught.value.safe_message
    with pytest.raises(WindowsMaintenanceError) as staged:
        WindowsStagedMigrationLease.acquire(tmp_path / "memory.db")
    assert staged.value.code == "WINDOWS_MAINTENANCE_UNAVAILABLE"
    with pytest.raises(WindowsMaintenanceError) as artifact:
        WindowsMigrationArtifactCustody.create(tmp_path / "backup.db")
    assert artifact.value.code == "WINDOWS_MAINTENANCE_UNAVAILABLE"
    with pytest.raises(WindowsMaintenanceError) as existing_artifact:
        WindowsMigrationArtifactCustody.open_existing(tmp_path / "backup.db")
    assert existing_artifact.value.code == "WINDOWS_MAINTENANCE_UNAVAILABLE"


native_windows = pytest.mark.skipif(sys.platform != "win32", reason="native Windows maintenance qualification only")


@pytest.fixture(autouse=True)
def _isolate_windows_coordination_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "win32":
        return
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir(exist_ok=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))


def _make_db(path: Path, value: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS marker(value TEXT NOT NULL)")
        connection.execute("DELETE FROM marker")
        connection.execute("INSERT INTO marker(value) VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def _make_runtime_db(path: Path, value: str) -> None:
    _make_db(path, value)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
    finally:
        connection.close()


def _read_marker(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT value FROM marker").fetchone()
        assert row is not None
        return str(row[0])
    finally:
        connection.close()


def _read_db_family_bytes(path: Path) -> dict[str, bytes]:
    result = {str(path): path.read_bytes()}
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            result[str(sidecar)] = sidecar.read_bytes()
    return result


def _assert_retained_db_family_bytes(lease: WindowsStagedMigrationLease, expected: dict[str, bytes]) -> None:
    retained_state = lease._capture_recovery_byte_state()
    assert set(retained_state) == set(expected)
    for path, (_identity, size, sha256) in retained_state.items():
        assert size == len(expected[path])
        assert sha256 == hashlib.sha256(expected[path]).hexdigest()


def _make_crashed_wal_db(path: Path, value: str) -> None:
    code = (
        "import os, sqlite3, sys\n"
        "conn=sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "conn.execute('CREATE TABLE marker(value TEXT NOT NULL)')\n"
        "conn.execute('PRAGMA journal_mode=WAL')\n"
        "conn.execute('PRAGMA wal_autocheckpoint=0')\n"
        "conn.execute('INSERT INTO marker(value) VALUES (?)', (sys.argv[2],))\n"
        "os._exit(0)\n"
    )
    crashed = subprocess.run(
        [sys.executable, "-c", code, str(path), value],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert crashed.returncode == 0, crashed.stderr
    assert Path(str(path) + "-wal").is_file()
    assert Path(str(path) + "-shm").is_file()


def _run_native_raw_writer_probe(
    path: Path,
    *,
    offset: int = 0,
    payload: bytes = b"X",
) -> subprocess.CompletedProcess[str]:
    code = (
        "import sys, win32file\n"
        "path=sys.argv[1]; offset=int(sys.argv[2]); payload=bytes.fromhex(sys.argv[3])\n"
        "try:\n"
        " h=win32file.CreateFile(path,win32file.GENERIC_WRITE,win32file.FILE_SHARE_READ|win32file.FILE_SHARE_WRITE,None,win32file.OPEN_EXISTING,win32file.FILE_ATTRIBUTE_NORMAL,None)\n"
        "except Exception as exc:\n"
        " print('OPEN_ERR',getattr(exc,'winerror',None)); raise SystemExit(0)\n"
        "print('OPEN_OK')\n"
        "try:\n"
        " win32file.SetFilePointer(h,offset,win32file.FILE_BEGIN)\n"
        " win32file.WriteFile(h,payload); win32file.FlushFileBuffers(h); print('WRITE_OK')\n"
        "except Exception as exc:\n"
        " print('WRITE_ERR',getattr(exc,'winerror',None))\n"
        "finally:\n"
        " h.Close()\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code, str(path), str(offset), payload.hex()],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _trace_maintenance_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    original_connect = sqlite3.connect
    statements: list[str] = []

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(lambda sql: statements.append(sql.strip().casefold()))
        return connection

    monkeypatch.setattr(windows_maintenance.sqlite3, "connect", traced_connect)
    return statements


def _inject_parent_substitution_race(
    monkeypatch: pytest.MonkeyPatch,
    *,
    leaf: Path,
    parent: Path,
    substituted_target: Path,
) -> dict[str, bool]:
    original_open = windows_maintenance._open_relative_open_always
    retired = parent.with_name(parent.name + "-retired")
    state = {"attempted": False, "rename_blocked": False, "renamed": False}

    def racing_open(parent_handle, path: str, **kwargs):
        if (
            not state["attempted"]
            and windows_maintenance._path_key(path) == windows_maintenance._path_key(str(leaf))
        ):
            state["attempted"] = True
            try:
                parent.rename(retired)
            except OSError:
                state["rename_blocked"] = True
            else:
                state["renamed"] = True
                completed = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(parent), str(substituted_target)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    pytest.fail(f"junction substitution setup failed: {completed.stderr or completed.stdout}")
        return original_open(parent_handle, path, **kwargs)

    monkeypatch.setattr(windows_maintenance, "_open_relative_open_always", racing_open)
    return state


@native_windows
def test_staged_migration_phase1_is_sqlite_free_and_byte_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    before = db.read_bytes()
    sidecar_presence = {suffix: Path(str(db) + suffix).exists() for suffix in ("-wal", "-shm")}
    original_connect = sqlite3.connect

    def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("migration Phase 1 must not open SQLite")

    monkeypatch.setattr(windows_maintenance.sqlite3, "connect", forbidden_connect)
    lease = WindowsStagedMigrationLease.acquire(db)
    try:
        assert lease.phase == WindowsStagedMigrationLease.PHASE_CUSTODY
        assert lease.connection is None
        assert lease.revalidate().file_id == lease.identity.file_id
        assert db.read_bytes() == before
        assert {suffix: Path(str(db) + suffix).exists() for suffix in ("-wal", "-shm")} == sidecar_presence
        with pytest.raises(WindowsMaintenanceError) as phase_invalid:
            lease.transition_to_mutation(backup_manifest_verified=False)
        assert phase_invalid.value.code == "MIGRATION_PHASE_INVALID"
        assert lease.connection is None
    finally:
        lease.release_unmutated()

    monkeypatch.setattr(windows_maintenance.sqlite3, "connect", original_connect)
    assert db.read_bytes() == before
    probe = original_connect(db, isolation_level=None)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert probe.execute("PRAGMA locking_mode").fetchone() == ("normal",)
        assert probe.execute("SELECT value FROM marker").fetchone() == ("old",)
    finally:
        probe.close()


@native_windows
def test_staged_migration_phase1_blocks_runtime_sentinel(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    runtime = WindowsRuntimeDBLease.open(db)
    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            WindowsStagedMigrationLease.acquire(db)
        assert caught.value.code == "MAINTENANCE_IN_PROGRESS"
    finally:
        runtime.close()


@native_windows
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_staged_migration_phase1_rejects_existing_sidecar_holder(tmp_path: Path, suffix: str) -> None:
    import win32con
    import win32file

    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    sidecar = Path(str(db) + suffix)
    sidecar.write_bytes(b"migration-sidecar-holder")
    holder = win32file.CreateFile(
        str(sidecar),
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        None,
        win32con.OPEN_EXISTING,
        win32con.FILE_ATTRIBUTE_NORMAL,
        None,
    )
    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            WindowsStagedMigrationLease.acquire(db)
        assert caught.value.code == "MIGRATION_HOLD_CONTENTION"
    finally:
        holder.Close()


@native_windows
def test_staged_migration_backup_guard_reads_existing_wal_without_checkpoint_or_journal_mutation(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    code = (
        "import os, sqlite3, sys\n"
        "conn=sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "conn.execute('CREATE TABLE marker(value TEXT NOT NULL)')\n"
        "conn.execute('PRAGMA journal_mode=WAL')\n"
        "conn.execute('PRAGMA wal_autocheckpoint=0')\n"
        "conn.execute(\"INSERT INTO marker(value) VALUES ('wal-only')\")\n"
        "os._exit(0)\n"
    )
    crashed = subprocess.run([sys.executable, "-c", code, str(db)], capture_output=True, text=True, check=False, timeout=10)
    assert crashed.returncode == 0, crashed.stderr
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    assert wal.is_file()
    assert shm.is_file()
    family_before = _read_db_family_bytes(db)

    lease = WindowsStagedMigrationLease.acquire(db)
    try:
        source = lease.open_backup_source()
        assert lease.phase == WindowsStagedMigrationLease.PHASE_BACKUP_GUARD
        assert lease.connection is source
        assert source.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert source.execute("PRAGMA locking_mode").fetchone() == ("exclusive",)
        assert Path(str(source.execute("PRAGMA database_list").fetchone()[2])).resolve() != db.resolve()
        _assert_retained_db_family_bytes(lease, family_before)
        for sidecar in (wal, shm):
            contender = _run_native_raw_writer_probe(sidecar)
            assert contender.returncode == 0, contender.stderr
            assert contender.stdout.splitlines() == ["OPEN_ERR 32"]

        backup = tmp_path / "consistent-backup.db"
        destination = sqlite3.connect(backup, isolation_level=None)
        try:
            source.backup(destination)
        finally:
            destination.close()
        _assert_retained_db_family_bytes(lease, family_before)
        assert source.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        verified = sqlite3.connect(backup)
        try:
            assert verified.execute("SELECT value FROM marker").fetchone() == ("wal-only",)
        finally:
            verified.close()

        with pytest.raises(WindowsMaintenanceError) as proof_required:
            lease.transition_to_mutation(backup_manifest_verified=False)
        assert proof_required.value.code == "MIGRATION_BACKUP_PROOF_REQUIRED"
        assert source.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        lease.release_unmutated()


@native_windows
def test_staged_migration_phase2_transition_and_runtime_activation(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    active_before = db.read_bytes()
    lease = WindowsStagedMigrationLease.acquire(db)
    try:
        held = _run_native_raw_writer_probe(db)
        assert held.returncode == 0, held.stderr
        assert held.stdout.splitlines() == ["OPEN_ERR 32"]

        backup_source = lease.open_backup_source()
        assert lease.phase == WindowsStagedMigrationLease.PHASE_BACKUP_GUARD
        assert backup_source.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        connection = lease.transition_to_mutation(backup_manifest_verified=True)
        assert connection is backup_source
        assert lease.phase == WindowsStagedMigrationLease.PHASE_MUTATION
        assert lease.connection is connection
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert connection.execute("PRAGMA locking_mode").fetchone() == ("exclusive",)
        database_list = connection.execute("PRAGMA database_list").fetchall()
        assert len(database_list) == 1
        assert Path(str(database_list[0][2])).resolve() != db.resolve()
        connection.execute("UPDATE marker SET value='migrated'")
        assert db.read_bytes() == active_before
        during_stage = _run_native_raw_writer_probe(db)
        assert during_stage.returncode == 0, during_stage.stderr
        assert during_stage.stdout.splitlines() == ["OPEN_ERR 32"]

        stage = lease.freeze_staged_image()
        frozen = _run_native_raw_writer_probe(db)
        assert frozen.returncode == 0, frozen.stderr
        assert frozen.stdout.splitlines() == ["OPEN_ERR 32"]
        promoted = lease.promote_frozen_stage(
            rollback_intent_verified=True,
            expected_sha256=str(stage["sha256"]),
            expected_size=int(stage["size"]),
        )
        assert promoted["sha256"] == stage["sha256"]
        lease.prepare_runtime_activation()
        after_proof = _run_native_raw_writer_probe(db)
        assert after_proof.returncode == 0, after_proof.stderr
        assert after_proof.stdout.splitlines() == ["OPEN_ERR 32"]
    finally:
        lease.close()

    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()
    assert _read_marker(db) == "migrated"
    released = sqlite3.connect(db, timeout=0.0, isolation_level=None)
    try:
        released.execute("BEGIN IMMEDIATE")
        released.rollback()
        assert released.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert released.execute("PRAGMA locking_mode").fetchone() == ("normal",)
    finally:
        released.close()


@native_windows
def test_staged_migration_exact_restore_preserves_target_identity_and_backup_sha(tmp_path: Path) -> None:
    from factlane.windows_security import current_user_sid, ensure_owner_private_directory

    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    original_bytes = db.read_bytes()
    original_sha256 = hashlib.sha256(original_bytes).hexdigest()
    original_size = len(original_bytes)

    private = tmp_path / "rollback-artifacts"
    ensure_owner_private_directory(private, owner_sid=current_user_sid())
    backup = private / "pre-v3-exact.db"
    artifact = WindowsMigrationArtifactCustody.create(backup)
    try:
        artifact.write_bytes(original_bytes)
        artifact.verify(expected_sha256=original_sha256, expected_size=original_size)
    finally:
        artifact.close()

    lease = WindowsStagedMigrationLease.acquire(db)
    original_file_id = lease.identity.file_id
    lease.open_backup_source()
    lease.transition_to_mutation(backup_manifest_verified=True)
    try:
        assert lease.connection is not None
        lease.connection.execute("UPDATE marker SET value='mutated-v3'")
        assert lease.connection.execute("SELECT value FROM marker").fetchone() == ("mutated-v3",)
        assert hashlib.sha256(db.read_bytes()).hexdigest() == original_sha256

        with pytest.raises(WindowsMaintenanceError) as intent_required:
            lease.restore_exact_from_backup(
                backup,
                rollback_intent_verified=False,
                expected_sha256=original_sha256,
                expected_size=original_size,
            )
        assert intent_required.value.code == "MIGRATION_ROLLBACK_INTENT_REQUIRED"
        assert hashlib.sha256(db.read_bytes()).hexdigest() == original_sha256

        lease.restore_exact_from_backup(
            backup,
            rollback_intent_verified=True,
            expected_sha256=original_sha256,
            expected_size=original_size,
        )
        assert lease.phase == WindowsStagedMigrationLease.PHASE_EXACT_RESTORED
        assert lease.revalidate().file_id == original_file_id
        assert hashlib.sha256(db.read_bytes()).hexdigest() == original_sha256
        held = _run_native_raw_writer_probe(db)
        assert held.returncode == 0, held.stderr
        assert held.stdout.splitlines() == ["OPEN_ERR 32"]
        lease.prepare_exact_restore_release()
        assert lease.connection is None
        assert lease._exact_release_prepared is True
        assert hashlib.sha256(db.read_bytes()).hexdigest() == original_sha256
        postproof = _run_native_raw_writer_probe(db)
        assert postproof.returncode == 0, postproof.stderr
        assert postproof.stdout.splitlines() == ["OPEN_ERR 32"]
    finally:
        lease.close()

    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == original_sha256
    reopened = sqlite3.connect(db, timeout=0.0, isolation_level=None)
    try:
        assert reopened.execute("SELECT value FROM marker").fetchone() == ("old",)
    finally:
        reopened.close()


@native_windows
@pytest.mark.parametrize("initial_journal", ["wal", "delete"])
def test_staged_migration_verified_recovery_guard_is_byte_stable_and_releases_unmutated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_journal: str,
) -> None:
    db = tmp_path / "memory.db"
    if initial_journal == "wal":
        _make_crashed_wal_db(db, "interrupted")
    else:
        _make_db(db, "interrupted")
    before = _read_db_family_bytes(db)
    statements = _trace_maintenance_sqlite(monkeypatch)

    lease = WindowsStagedMigrationLease.acquire(db)
    try:
        with pytest.raises(WindowsMaintenanceError) as proof_required:
            lease.open_verified_recovery_source(recovery_proof_verified=False)
        assert proof_required.value.code == "MIGRATION_RECOVERY_PROOF_REQUIRED"
        assert lease.phase == WindowsStagedMigrationLease.PHASE_CUSTODY
        assert lease.connection is None
        _assert_retained_db_family_bytes(lease, before)

        connection = lease.open_verified_recovery_source(recovery_proof_verified=True)
        assert lease.phase == WindowsStagedMigrationLease.PHASE_RECOVERY_GUARD
        assert connection is lease.connection
        assert connection.execute("PRAGMA locking_mode").fetchone() == ("exclusive",)
        assert connection.execute("PRAGMA journal_mode").fetchone() == (initial_journal,)
        _assert_retained_db_family_bytes(lease, before)
        assert not any(sql.startswith("pragma wal_checkpoint") for sql in statements)
        assert "pragma journal_mode=delete" not in statements
        assert "pragma synchronous=full" not in statements

        with pytest.raises(WindowsMaintenanceError) as target_proof:
            lease.transition_verified_recovery_guard_to_mutation(recovery_target_verified=False)
        assert target_proof.value.code == "MIGRATION_RECOVERY_TARGET_PROOF_REQUIRED"
        _assert_retained_db_family_bytes(lease, before)

        lease.release_verified_recovery_guard_unmutated()
        assert lease._closed is True
    finally:
        if not lease._closed:
            lease.close()
    assert _read_db_family_bytes(db) == before


@native_windows
@pytest.mark.parametrize("initial_journal", ["wal", "delete"])
def test_staged_migration_verified_recovery_mutates_only_after_target_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_journal: str,
) -> None:
    db = tmp_path / "memory.db"
    if initial_journal == "wal":
        _make_crashed_wal_db(db, "interrupted")
    else:
        _make_db(db, "interrupted")
    before = _read_db_family_bytes(db)
    statements = _trace_maintenance_sqlite(monkeypatch)

    lease = WindowsStagedMigrationLease.acquire(db)
    try:
        connection = lease.open_verified_recovery_source(recovery_proof_verified=True)
        assert lease.phase == WindowsStagedMigrationLease.PHASE_RECOVERY_GUARD
        _assert_retained_db_family_bytes(lease, before)
        with pytest.raises(WindowsMaintenanceError) as target_proof:
            lease.transition_verified_recovery_guard_to_mutation(recovery_target_verified=False)
        assert target_proof.value.code == "MIGRATION_RECOVERY_TARGET_PROOF_REQUIRED"
        _assert_retained_db_family_bytes(lease, before)
        assert not any(sql.startswith("pragma wal_checkpoint") for sql in statements)
        assert "pragma journal_mode=delete" not in statements

        connection = lease.transition_verified_recovery_guard_to_mutation(recovery_target_verified=True)
        assert lease.phase == WindowsStagedMigrationLease.PHASE_MUTATION
        assert connection is lease.connection
        database_list = connection.execute("PRAGMA database_list").fetchall()
        assert len(database_list) == 1
        assert Path(str(database_list[0][2])).resolve() != db.resolve()
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert connection.execute("PRAGMA locking_mode").fetchone() == ("exclusive",)
        connection.execute("UPDATE marker SET value='live-mutated'")
        assert connection.execute("SELECT value FROM marker").fetchone() == ("live-mutated",)
        _assert_retained_db_family_bytes(lease, before)
        contender = _run_native_raw_writer_probe(db)
        assert contender.returncode == 0, contender.stderr
        assert contender.stdout.splitlines() == ["OPEN_ERR 32"]
        checkpoint_statements = [sql for sql in statements if sql.startswith("pragma wal_checkpoint")]
        assert bool(checkpoint_statements) is (initial_journal == "wal")

        lease.release_staged_mutation_unmutated()
    finally:
        if not lease._closed:
            lease.close()
    assert _read_db_family_bytes(db) == before
    assert _read_marker(db) == "interrupted"


@native_windows
def test_staged_migration_verified_recovery_guard_wal_mode_without_sidecars_is_byte_stable(tmp_path: Path) -> None:
    db = tmp_path / "wal-no-sidecars.db"
    _make_runtime_db(db, "stable")
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()
    before = _read_db_family_bytes(db)

    lease = WindowsStagedMigrationLease.acquire(db)
    try:
        connection = lease.open_verified_recovery_source(recovery_proof_verified=True)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        _assert_retained_db_family_bytes(lease, before)
        assert not Path(str(db) + "-wal").exists()
        assert not Path(str(db) + "-shm").exists()
        lease.release_verified_recovery_guard_unmutated()
    finally:
        if not lease._closed:
            lease.close()
    assert _read_db_family_bytes(db) == before


@native_windows
def test_staged_migration_verified_exact_v2_restart_releases_without_sqlite_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "memory.db"
    _make_db(db, "pre-v3")
    expected_bytes = db.read_bytes()
    expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()
    expected_size = len(expected_bytes)

    lease = WindowsStagedMigrationLease.acquire(db)

    def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("exact-restored v2 recovery must not open SQLite")

    monkeypatch.setattr(windows_maintenance.sqlite3, "connect", forbidden_connect)
    try:
        with pytest.raises(WindowsMaintenanceError) as proof_required:
            lease.prepare_verified_exact_restore_release(
                recovery_proof_verified=False,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )
        assert proof_required.value.code == "MIGRATION_RECOVERY_PROOF_REQUIRED"
        assert lease.phase == WindowsStagedMigrationLease.PHASE_CUSTODY
        assert db.read_bytes() == expected_bytes

        lease.prepare_verified_exact_restore_release(
            recovery_proof_verified=True,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
        assert lease.phase == WindowsStagedMigrationLease.PHASE_EXACT_RESTORED
        assert lease.connection is None
        assert lease._exact_release_prepared is True
        assert db.read_bytes() == expected_bytes
        assert not Path(str(db) + "-wal").exists()
        assert not Path(str(db) + "-shm").exists()
    finally:
        lease.close()

    monkeypatch.undo()
    assert db.read_bytes() == expected_bytes
    reopened = sqlite3.connect(db, timeout=0.0, isolation_level=None)
    try:
        assert reopened.execute("SELECT value FROM marker").fetchone() == ("pre-v3",)
        assert reopened.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        reopened.close()


@native_windows
def test_staged_migration_post_checkpoint_crash_can_resume_through_verified_recovery(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_crashed_wal_db(db, "interrupted")
    code = (
        "import os, sys\n"
        "import factlane.windows_maintenance as wm\n"
        "lease=wm.WindowsStagedMigrationLease.acquire(sys.argv[1])\n"
        "lease.open_verified_recovery_source(recovery_proof_verified=True)\n"
        "def crash(stage):\n"
        "    if stage == 'after_wal_checkpoint':\n"
        "        os._exit(73)\n"
        "wm._MIGRATION_RECOVERY_FAULT_HOOK=crash\n"
        "lease.transition_verified_recovery_guard_to_mutation(recovery_target_verified=True)\n"
        "os._exit(74)\n"
    )
    crashed = subprocess.run(
        [sys.executable, "-c", code, str(db)],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert crashed.returncode == 73, crashed.stderr

    lease = WindowsStagedMigrationLease.acquire(db)
    try:
        connection = lease.open_verified_recovery_source(recovery_proof_verified=True)
        assert lease.phase == WindowsStagedMigrationLease.PHASE_RECOVERY_GUARD
        connection = lease.transition_verified_recovery_guard_to_mutation(recovery_target_verified=True)
        assert lease.phase == WindowsStagedMigrationLease.PHASE_MUTATION
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert Path(str(connection.execute("PRAGMA database_list").fetchone()[2])).resolve() != db.resolve()
        lease.release_staged_mutation_unmutated()
    finally:
        if not lease._closed:
            lease.close()

    assert _read_marker(db) == "interrupted"


@native_windows
def test_staged_migration_mid_raw_restore_crash_retries_exactly_from_fresh_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from factlane.windows_security import current_user_sid, ensure_owner_private_directory

    def make_large_db(path: Path, value: str, *, random_payload: bool = False) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
            connection.execute("INSERT INTO marker(value) VALUES (?)", (value,))
            connection.execute("CREATE TABLE payload(data BLOB NOT NULL)")
            if random_payload:
                connection.execute("INSERT INTO payload(data) VALUES (randomblob(?))", (3 * 1024 * 1024,))
            else:
                connection.execute("INSERT INTO payload(data) VALUES (zeroblob(?))", (3 * 1024 * 1024,))
            connection.commit()
        finally:
            connection.close()

    source = tmp_path / "pre-v3-source.db"
    make_large_db(source, "pre-v3")
    expected_bytes = source.read_bytes()
    assert len(expected_bytes) > 2 * 1024 * 1024
    expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()
    expected_size = len(expected_bytes)

    private = tmp_path / "rollback-retry-artifacts"
    ensure_owner_private_directory(private, owner_sid=current_user_sid())
    backup = private / "pre-v3-exact.db"
    with WindowsMigrationArtifactCustody.create(backup) as artifact:
        artifact.write_bytes(expected_bytes)

    db = tmp_path / "memory.db"
    make_large_db(db, "migrated-v3", random_payload=True)
    initial = WindowsStagedMigrationLease.acquire(db)
    original_file_id = initial.identity.file_id
    initial.release_unmutated()

    code = (
        "import os, sys\n"
        "import factlane.windows_maintenance as wm\n"
        "def crash(written, total):\n"
        "    if 0 < written < total:\n"
        "        os._exit(79)\n"
        "wm._MIGRATION_RAW_COPY_FAULT_HOOK=crash\n"
        "lease=wm.WindowsStagedMigrationLease.acquire(sys.argv[1])\n"
        "lease.restore_exact_from_backup_from_custody(\n"
        "    sys.argv[2], recovery_proof_verified=True,\n"
        "    expected_sha256=sys.argv[3], expected_size=int(sys.argv[4])\n"
        ")\n"
        "os._exit(80)\n"
    )
    crashed = subprocess.run(
        [sys.executable, "-c", code, str(db), str(backup), expected_sha256, str(expected_size)],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert crashed.returncode == 79, crashed.stderr
    assert hashlib.sha256(db.read_bytes()).hexdigest() != expected_sha256
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()

    retry = WindowsStagedMigrationLease.acquire(db)
    assert retry.identity.file_id == original_file_id

    def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("fresh-custody exact rollback retry must not open SQLite")

    monkeypatch.setattr(windows_maintenance.sqlite3, "connect", forbidden_connect)
    try:
        with pytest.raises(WindowsMaintenanceError) as proof_required:
            retry.restore_exact_from_backup_from_custody(
                backup,
                recovery_proof_verified=False,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )
        assert proof_required.value.code == "MIGRATION_RECOVERY_PROOF_REQUIRED"

        retry.restore_exact_from_backup_from_custody(
            backup,
            recovery_proof_verified=True,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
        assert retry.phase == WindowsStagedMigrationLease.PHASE_EXACT_RESTORED
        assert retry.revalidate().file_id == original_file_id
        assert hashlib.sha256(db.read_bytes()).hexdigest() == expected_sha256
        assert db.read_bytes() == expected_bytes
        held = _run_native_raw_writer_probe(db)
        assert held.returncode == 0, held.stderr
        assert held.stdout.splitlines() == ["OPEN_ERR 32"]
        retry.prepare_exact_restore_release()
    finally:
        retry.close()
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()


@native_windows
def test_staged_migration_plain_close_of_exact_restored_state_retains_fail_closed(tmp_path: Path) -> None:
    from factlane.windows_security import current_user_sid, ensure_owner_private_directory

    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    original_bytes = db.read_bytes()
    original_sha256 = hashlib.sha256(original_bytes).hexdigest()
    original_size = len(original_bytes)
    private = tmp_path / "rollback-close-guard"
    ensure_owner_private_directory(private, owner_sid=current_user_sid())
    backup = private / "pre-v3-exact.db"
    with WindowsMigrationArtifactCustody.create(backup) as artifact:
        artifact.write_bytes(original_bytes)

    retained_start = len(windows_maintenance._RETAINED_EXCLUSION_HANDLES)
    lease = WindowsStagedMigrationLease.acquire(db)
    lease.open_backup_source()
    lease.transition_to_mutation(backup_manifest_verified=True)
    lease.restore_exact_from_backup(
        backup,
        rollback_intent_verified=True,
        expected_sha256=original_sha256,
        expected_size=original_size,
    )
    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            lease.close()
        assert caught.value.code == "MIGRATION_EXACT_RELEASE_REQUIRED"
        assert lease._retained is True
        assert hashlib.sha256(db.read_bytes()).hexdigest() == original_sha256
    finally:
        retained = windows_maintenance._RETAINED_EXCLUSION_HANDLES[retained_start:]
        del windows_maintenance._RETAINED_EXCLUSION_HANDLES[retained_start:]
        for item in retained:
            closer = getattr(item, "close", None)
            if not callable(closer):
                closer = getattr(item, "Close", None)
            if callable(closer):
                closer()


@native_windows
def test_staged_migration_mutation_transition_failure_retains_sqlite_exclusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    lease = WindowsStagedMigrationLease.acquire(db)
    original_connect = sqlite3.connect
    retained_start = len(windows_maintenance._RETAINED_EXCLUSION_HANDLES)
    source = lease.open_backup_source()

    class FailingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection
            self.injected = False

        def execute(self, sql: str, *args, **kwargs):
            normalized = sql.strip().casefold()
            if normalized == "pragma journal_mode=delete" and not self.injected:
                result = self._connection.execute(sql, *args, **kwargs)
                self.injected = True
                raise sqlite3.OperationalError("injected migration mutation transition failure")
            return self._connection.execute(sql, *args, **kwargs)

        def close(self) -> None:
            self._connection.close()

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

    wrapped = FailingConnection(source)
    lease.connection = wrapped
    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            lease.transition_to_mutation(backup_manifest_verified=True)
        assert caught.value.code == "MIGRATION_MUTATION_STAGE_UNPROVEN"
        assert lease._retained is True
        assert wrapped.injected is True

        foreign = _run_native_raw_writer_probe(db)
        assert foreign.returncode == 0, foreign.stderr
        assert foreign.stdout.splitlines() == ["OPEN_ERR 32"]
        lease.close()
        foreign_after_close = _run_native_raw_writer_probe(db)
        assert foreign_after_close.returncode == 0, foreign_after_close.stderr
        assert foreign_after_close.stdout.splitlines() == ["OPEN_ERR 32"]
    finally:
        retained = windows_maintenance._RETAINED_EXCLUSION_HANDLES[retained_start:]
        del windows_maintenance._RETAINED_EXCLUSION_HANDLES[retained_start:]
        for item in retained:
            closer = getattr(item, "close", None)
            if not callable(closer):
                closer = getattr(item, "Close", None)
            if callable(closer):
                closer()


@native_windows
def test_migration_artifact_custody_is_owner_private_create_new_and_verifiable(tmp_path: Path) -> None:
    from factlane.windows_security import (
        WindowsSecurityError,
        current_user_sid,
        ensure_owner_private_directory,
        validate_owner_private_path,
    )

    sid = current_user_sid()
    private = tmp_path / "migration-artifacts"
    ensure_owner_private_directory(private, owner_sid=sid)
    target = private / "backup.manifest"
    payload = b'{"kind":"FACTLANE_PRE_V3_BACKUP"}\n'
    expected_sha256 = hashlib.sha256(payload).hexdigest()

    artifact = WindowsMigrationArtifactCustody.create(target)
    try:
        artifact.write_bytes(payload)
        receipt = artifact.verify(expected_sha256=expected_sha256, expected_size=len(payload))
        assert receipt["path"] == str(target)
        assert receipt["size"] == len(payload)
        assert receipt["sha256"] == expected_sha256
        assert receipt["file_id"] == artifact.identity.file_id_hex
        validate_owner_private_path(target, owner_sid=sid, directory=False)

        with pytest.raises(WindowsMaintenanceError) as exists:
            WindowsMigrationArtifactCustody.create(target)
        assert exists.value.code == "MIGRATION_ARTIFACT_EXISTS"

        replacement = private / "replacement.tmp"
        replacement.write_bytes(b"replacement")
        with pytest.raises(OSError):
            os.replace(replacement, target)
        assert target.read_bytes() == payload
    finally:
        artifact.close()


@native_windows
def test_migration_artifact_verified_delete_preserves_governed_error_on_early_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from factlane.windows_security import current_user_sid, ensure_owner_private_directory

    sid = current_user_sid()
    private = tmp_path / "migration-artifacts-delete-failure"
    ensure_owner_private_directory(private, owner_sid=sid)
    target = private / "rollback-intent.json"
    payload = b'{"kind":"FACTLANE_PRE_V3_ROLLBACK_INTENT"}\n'
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    artifact = WindowsMigrationArtifactCustody.create(target)
    try:
        artifact.write_bytes(payload)
        receipt = artifact.verify(expected_sha256=expected_sha256, expected_size=len(payload))
    finally:
        artifact.close()

    def fail_relative_open(*_args, **_kwargs):
        raise WindowsMaintenanceError("MIGRATION_ARTIFACT_BUSY", "injected early verified-delete contention")

    monkeypatch.setattr(windows_maintenance, "_open_relative_open_always", fail_relative_open)
    with pytest.raises(WindowsMaintenanceError) as caught:
        WindowsMigrationArtifactCustody.delete_existing_verified(
            target,
            expected_sha256=expected_sha256,
            expected_size=len(payload),
            expected_volume_serial_number=int(receipt["volume_serial_number"]),
            expected_file_id=str(receipt["file_id"]),
        )
    assert caught.value.code == "MIGRATION_ARTIFACT_BUSY"
    assert target.read_bytes() == payload


@native_windows
def test_migration_artifact_custody_rejects_reparse_parent(tmp_path: Path) -> None:
    from factlane.windows_security import current_user_sid, ensure_owner_private_directory

    sid = current_user_sid()
    real = tmp_path / "artifact-real"
    junction = tmp_path / "artifact-junction"
    ensure_owner_private_directory(real, owner_sid=sid)
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(real)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr or completed.stdout}")
    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            WindowsMigrationArtifactCustody.create(junction / "backup.db")
        assert caught.value.code == "WINDOWS_REPARSE_DENIED"
        assert not (real / "backup.db").exists()
    finally:
        try:
            junction.rmdir()
        except OSError:
            pass


@native_windows
def test_migration_artifact_existing_custody_reads_exact_bytes_and_blocks_substitution(tmp_path: Path) -> None:
    from factlane.windows_security import current_user_sid, ensure_owner_private_directory

    sid = current_user_sid()
    private = tmp_path / "migration-existing"
    ensure_owner_private_directory(private, owner_sid=sid)
    target = private / "backup.db"
    payload = (b"factlane-pre-v3-backup\x00" * 4096) + b"tail"
    expected_sha256 = hashlib.sha256(payload).hexdigest()

    with WindowsMigrationArtifactCustody.create(target) as created:
        created.write_bytes(payload)

    existing = WindowsMigrationArtifactCustody.open_existing(target)
    try:
        receipt = existing.read_exact()
        assert receipt["path"] == str(target)
        assert receipt["data"] == payload
        assert receipt["size"] == len(payload)
        assert receipt["sha256"] == expected_sha256
        assert receipt["file_id"] == existing.identity.file_id_hex

        replacement = private / "replacement.tmp"
        replacement.write_bytes(b"replacement")
        with pytest.raises(OSError):
            os.replace(replacement, target)
        assert existing.read_exact()["data"] == payload

        with pytest.raises(WindowsMaintenanceError) as read_only:
            existing.write_bytes(b"forbidden")
        assert read_only.value.code == "MIGRATION_ARTIFACT_WRITE_INVALID"
    finally:
        existing.close()


@native_windows
@pytest.mark.parametrize("unsafe_target", ["parent", "file"])
def test_migration_artifact_existing_custody_rejects_unsafe_dacl(
    tmp_path: Path,
    unsafe_target: str,
) -> None:
    import win32security

    from factlane.windows_security import current_user_sid, ensure_owner_private_directory

    sid = current_user_sid()
    private = tmp_path / "migration-unsafe"
    ensure_owner_private_directory(private, owner_sid=sid)
    target = private / "backup.manifest"
    with WindowsMigrationArtifactCustody.create(target) as created:
        created.write_bytes(b"manifest")

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{sid}D:P(A;;GA;;;{sid})(A;;GR;;;WD)",
        win32security.SDDL_REVISION_1,
    )
    owner = descriptor.GetSecurityDescriptorOwner()
    dacl = descriptor.GetSecurityDescriptorDacl()
    win32security.SetNamedSecurityInfo(
        str(private if unsafe_target == "parent" else target),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | getattr(win32security, "PROTECTED_DACL_SECURITY_INFORMATION", 0x80000000),
        owner,
        None,
        dacl,
        None,
    )

    with pytest.raises(WindowsMaintenanceError) as caught:
        WindowsMigrationArtifactCustody.open_existing(target)
    assert caught.value.code == "MIGRATION_ARTIFACT_PATH_UNSAFE"


@native_windows
def test_migration_artifact_existing_custody_rejects_reparse_parent(tmp_path: Path) -> None:
    from factlane.windows_security import current_user_sid, ensure_owner_private_directory

    sid = current_user_sid()
    real = tmp_path / "existing-artifact-real"
    junction = tmp_path / "existing-artifact-junction"
    ensure_owner_private_directory(real, owner_sid=sid)
    target = real / "backup.db"
    with WindowsMigrationArtifactCustody.create(target) as created:
        created.write_bytes(b"backup")

    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(real)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr or completed.stdout}")
    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            WindowsMigrationArtifactCustody.open_existing(junction / target.name)
        assert caught.value.code == "WINDOWS_REPARSE_DENIED"
    finally:
        try:
            junction.rmdir()
        except OSError:
            pass


@native_windows
def test_runtime_shared_lease_can_create_main_database_file(tmp_path: Path) -> None:
    db = tmp_path / "created.db"
    assert not db.exists()
    first = WindowsRuntimeDBLease.open(db)
    second = WindowsRuntimeDBLease.open(db)
    try:
        assert db.is_file()
        assert first.identity.file_id == second.identity.file_id
    finally:
        second.close()
        first.close()

    _make_db(db, "created")
    assert _read_marker(db) == "created"


@native_windows
def test_runtime_shared_lease_secure_mode_creates_owner_private_main_database(tmp_path: Path) -> None:
    from factlane.windows_security import current_user_sid, ensure_owner_private_directory, validate_owner_private_path

    sid = current_user_sid()
    private = tmp_path / "private-runtime"
    ensure_owner_private_directory(private, owner_sid=sid)
    db = private / "created.db"

    lease = WindowsRuntimeDBLease.open(db, owner_sid=sid)
    try:
        validate_owner_private_path(db, owner_sid=sid, directory=False)
        assert lease.revalidate().file_id == lease.identity.file_id
    finally:
        lease.close()


@native_windows
def test_runtime_family_custody_preserves_existing_wal_and_securely_creates_missing_shm(tmp_path: Path) -> None:
    from factlane.windows_security import (
        current_user_sid,
        ensure_owner_private_directory,
        protect_owner_private_path,
        validate_owner_private_path,
    )

    sid = current_user_sid()
    private = tmp_path / "private-family"
    ensure_owner_private_directory(private, owner_sid=sid)
    db = private / "memory.db"
    lease = WindowsRuntimeDBLease.open(db, owner_sid=sid)
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    wal.write_bytes(b"non-empty-wal-sentinel")
    protect_owner_private_path(wal, owner_sid=sid, directory=False)
    wal_before = wal.read_bytes()
    wal_identity_before = windows_maintenance._probe_identity(str(wal))

    custody = WindowsRuntimeDBFamilyCustody.acquire_existing(db, lease, owner_sid=sid)
    try:
        custody.create_missing()
        custody.revalidate()
        validate_owner_private_path(wal, owner_sid=sid, directory=False)
        validate_owner_private_path(shm, owner_sid=sid, directory=False)
        assert wal.read_bytes() == wal_before
        assert windows_maintenance._same_file(
            windows_maintenance._probe_identity(str(wal)),
            wal_identity_before,
        )
        assert shm.read_bytes() == b""
    finally:
        custody.close()
        lease.close()


@native_windows
def test_runtime_family_custody_rejects_unsafe_existing_sidecar_before_creating_missing_peer(tmp_path: Path) -> None:
    import win32security

    from factlane.windows_security import current_user_sid, ensure_owner_private_directory, validate_owner_private_path

    sid = current_user_sid()
    private = tmp_path / "private-unsafe-family"
    ensure_owner_private_directory(private, owner_sid=sid)
    db = private / "memory.db"
    lease = WindowsRuntimeDBLease.open(db, owner_sid=sid)
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    wal.write_bytes(b"unsafe-wal-sentinel")
    owner = win32security.ConvertStringSidToSid(sid)
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{sid}D:(A;;GA;;;{sid})",
        win32security.SDDL_REVISION_1,
    )
    win32security.SetNamedSecurityInfo(
        str(wal),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | getattr(win32security, "UNPROTECTED_DACL_SECURITY_INFORMATION", 0x20000000),
        owner,
        None,
        descriptor.GetSecurityDescriptorDacl(),
        None,
    )
    with pytest.raises(WindowsSecurityError) as unsafe:
        validate_owner_private_path(wal, owner_sid=sid, directory=False)
    assert unsafe.value.code == "WINDOWS_DACL_UNSAFE"
    before = wal.read_bytes()

    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            WindowsRuntimeDBFamilyCustody.acquire_existing(db, lease, owner_sid=sid)
        assert caught.value.code == "RUNTIME_DATABASE_PATH_UNSAFE"
        assert wal.read_bytes() == before
        assert not shm.exists()
    finally:
        lease.close()


@native_windows
@pytest.mark.parametrize("guard_kind", ["runtime", "barrier"])
def test_open_always_parent_guard_blocks_parent_substitution_before_leaf_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard_kind: str,
) -> None:
    parent = tmp_path / f"{guard_kind}-parent"
    substituted = tmp_path / f"{guard_kind}-substituted"
    substituted.mkdir()

    owner_sid = None
    if guard_kind == "barrier":
        from factlane.windows_security import current_user_sid, ensure_owner_private_directory

        owner_sid = current_user_sid()
        ensure_owner_private_directory(parent, owner_sid=owner_sid)
        leaf = parent / "maintenance.lock"
    else:
        parent.mkdir()
        leaf = parent / "memory.db"

    state = _inject_parent_substitution_race(
        monkeypatch,
        leaf=leaf,
        parent=parent,
        substituted_target=substituted,
    )
    expected_code = "BARRIER_IDENTITY_DRIFT" if guard_kind == "barrier" else "RUNTIME_LEASE_IDENTITY_DRIFT"
    with pytest.raises(WindowsMaintenanceError) as caught:
        if guard_kind == "barrier":
            WindowsBarrierGuard.acquire(leaf, owner_sid=owner_sid)
        else:
            WindowsRuntimeDBLease.open(leaf)

    assert caught.value.code == expected_code
    assert state["attempted"] is True
    assert state["renamed"] is True
    assert not (substituted / leaf.name).exists()


@native_windows
def test_runtime_shared_leases_coexist_with_sqlite_and_exclusive_blocks_both_directions(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_db(db, "initial")

    first = WindowsRuntimeDBLease.open(db)
    second = WindowsRuntimeDBLease.open(db)
    try:
        assert len(first.identity.file_id) == 16
        assert first.identity.volume_serial_number > 0
        assert first.identity.file_id == second.identity.file_id
        connection = sqlite3.connect(db)
        try:
            connection.execute("UPDATE marker SET value='runtime-write'")
            connection.commit()
            assert connection.execute("SELECT value FROM marker").fetchone() == ("runtime-write",)
        finally:
            connection.close()

        with pytest.raises(WindowsMaintenanceError) as busy:
            WindowsExclusiveMaintenanceLease.acquire(db)
        assert busy.value.code == "MAINTENANCE_HOLD_CONTENTION"
    finally:
        second.close()
        first.close()

    exclusive = WindowsExclusiveMaintenanceLease.acquire(db)
    blocked_done = threading.Event()
    blocked_errors: list[BaseException] = []

    def blocked_runtime_open() -> None:
        try:
            with WindowsRuntimeDBLease.open(db):
                pass
        except BaseException as exc:
            blocked_errors.append(exc)
        finally:
            blocked_done.set()

    blocked_thread = threading.Thread(target=blocked_runtime_open, daemon=True)
    try:
        blocked_thread.start()
        time.sleep(0.1)
        assert blocked_done.is_set() is False
    finally:
        exclusive.close()
        blocked_thread.join(timeout=5)

    assert blocked_done.is_set() is True
    assert blocked_errors == []

    with WindowsRuntimeDBLease.open(db):
        pass


@native_windows
def test_runtime_lease_denies_delete_sharing_and_blocks_path_replacement(tmp_path: Path) -> None:
    import win32con
    import win32file

    db = tmp_path / "memory.db"
    replacement = tmp_path / "other.db"
    _make_db(db, "old")
    _make_db(replacement, "new")

    lease = WindowsRuntimeDBLease.open(db)
    old_id = lease.identity.file_id
    try:
        source = win32file.CreateFile(
            str(replacement),
            win32con.GENERIC_READ | win32con.GENERIC_WRITE | win32con.DELETE,
            win32con.FILE_SHARE_DELETE,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_ATTRIBUTE_NORMAL | getattr(win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0x00200000),
            None,
        )
        try:
            with pytest.raises(WindowsMaintenanceError) as blocked:
                WindowsExclusiveMaintenanceLease._rename_handle_replace(source, str(db))
            assert blocked.value.code in {"MAINTENANCE_HOLD_CONTENTION", "PROMOTION_FAILED"}
        finally:
            source.Close()
        lease.revalidate()
        current = WindowsRuntimeDBLease.open(db)
        try:
            assert old_id == current.identity.file_id
        finally:
            current.close()
    finally:
        lease.close()


@native_windows
def test_runtime_leases_share_db_scoped_owner_private_sentinel_and_hash_binding(tmp_path: Path) -> None:
    from factlane.windows_security import current_user_sid, validate_owner_private_path

    first_db = tmp_path / "one" / "memory.db"
    second_db = tmp_path / "two" / "memory.db"
    first_db.parent.mkdir()
    second_db.parent.mkdir()
    _make_db(first_db, "one")
    _make_db(second_db, "two")

    first = WindowsRuntimeDBLease.open(first_db)
    peer = WindowsRuntimeDBLease.open(first_db)
    second = WindowsRuntimeDBLease.open(second_db)
    try:
        assert first._sentinel.path == peer._sentinel.path
        assert first._sentinel.identity.file_id == peer._sentinel.identity.file_id
        assert first._sentinel.path != second._sentinel.path
        assert Path(first._sentinel.path).name.startswith("db-")
        assert Path(first._sentinel.path).name.endswith(".lease")
        assert len(Path(first._sentinel.path).stem.removeprefix("db-")) == 64
        sid = current_user_sid()
        validate_owner_private_path(Path(first._sentinel.path).parent, owner_sid=sid, directory=True)
        validate_owner_private_path(first._sentinel.path, owner_sid=sid, directory=False)
    finally:
        second.close()
        peer.close()
        first.close()


@native_windows
def test_shared_sentinel_handle_blocks_parent_namespace_substitution_while_held(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_db(db, "old")
    lease = WindowsRuntimeDBLease.open(db)
    sentinel_parent = Path(lease._sentinel.path).parent
    moved_parent = sentinel_parent.with_name(sentinel_parent.name + "-moved")
    try:
        with pytest.raises(OSError):
            sentinel_parent.rename(moved_parent)
        assert sentinel_parent.is_dir()
        assert Path(lease._sentinel.path).is_file()
        lease._sentinel.revalidate()
    finally:
        lease.close()


@native_windows
def test_coordination_state_rejects_localappdata_reparse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real-local"
    junction = tmp_path / "junction-local"
    real.mkdir()
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(real)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr or completed.stdout}")
    db = tmp_path / "memory.db"
    _make_db(db, "old")
    monkeypatch.setenv("LOCALAPPDATA", str(junction))
    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            WindowsRuntimeDBLease.open(db)
        assert caught.value.code in {"WINDOWS_REPARSE_DENIED", "WINDOWS_COORDINATION_PATH_UNSAFE"}
    finally:
        try:
            junction.rmdir()
        except OSError:
            pass


@native_windows
def test_coordination_sentinel_blocks_cross_process_runtime_and_inplace_both_directions(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    inplace_attempt = (
        "import sys\n"
        "from factlane.windows_maintenance import WindowsInPlaceMaintenanceLease, WindowsMaintenanceError\n"
        "try:\n"
        "    lease=WindowsInPlaceMaintenanceLease.acquire(sys.argv[1])\n"
        "except WindowsMaintenanceError as exc:\n"
        "    print(exc.code)\n"
        "else:\n"
        "    lease.close(); print('ACQUIRED')\n"
    )
    runtime_attempt = (
        "import sys\n"
        "from factlane.windows_maintenance import WindowsRuntimeDBLease, WindowsMaintenanceError\n"
        "try:\n"
        "    lease=WindowsRuntimeDBLease.open(sys.argv[1])\n"
        "except WindowsMaintenanceError as exc:\n"
        "    print(exc.code)\n"
        "else:\n"
        "    lease.close(); print('ACQUIRED')\n"
    )

    runtime = WindowsRuntimeDBLease.open(db)
    try:
        blocked = subprocess.run(
            [sys.executable, "-c", inplace_attempt, str(db)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert blocked.returncode == 0, blocked.stderr
        assert blocked.stdout.strip() == "MAINTENANCE_IN_PROGRESS"
    finally:
        runtime.close()

    inplace = WindowsInPlaceMaintenanceLease.acquire(db)
    try:
        blocked = subprocess.run(
            [sys.executable, "-c", runtime_attempt, str(db)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert blocked.returncode == 0, blocked.stderr
        assert blocked.stdout.strip() == "MAINTENANCE_IN_PROGRESS"
    finally:
        inplace.prepare_runtime_activation()
        inplace.close()


@native_windows
def test_inplace_maintenance_holds_external_sqlite_before_a3_mutation(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_db(db, "old")
    setup = sqlite3.connect(db, isolation_level=None)
    try:
        assert setup.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    finally:
        setup.close()

    external = sqlite3.connect(db, timeout=0.0, isolation_level=None)
    external.execute("BEGIN IMMEDIATE")
    external.execute("UPDATE marker SET value='uncommitted-external'")
    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            WindowsInPlaceMaintenanceLease.acquire(db)
        assert caught.value.code == "MAINTENANCE_HOLD_SQLITE_BUSY"
        assert external.execute("SELECT value FROM marker").fetchone() == ("uncommitted-external",)
    finally:
        external.rollback()
        external.close()

    probe = sqlite3.connect(db)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert probe.execute("SELECT value FROM marker").fetchone() == ("old",)
    finally:
        probe.close()


@native_windows
def test_inplace_acquire_race_writer_wins_before_exclusive_proof_without_mode_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    original_connect = sqlite3.connect
    adversary: dict[str, sqlite3.Connection] = {}
    injected = False

    class RacingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, sql: str, *args, **kwargs):
            nonlocal injected
            if sql.strip().casefold() == "begin exclusive" and not injected:
                injected = True
                foreign = original_connect(db, timeout=0.0, isolation_level=None)
                foreign.execute("BEGIN IMMEDIATE")
                foreign.execute("UPDATE marker SET value='racer-uncommitted'")
                adversary["connection"] = foreign
            return self._connection.execute(sql, *args, **kwargs)

        def close(self) -> None:
            self._connection.close()

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

    def racing_connect(database, *args, **kwargs):
        return RacingConnection(original_connect(database, *args, **kwargs))

    monkeypatch.setattr(windows_maintenance.sqlite3, "connect", racing_connect)
    with pytest.raises(WindowsMaintenanceError) as caught:
        WindowsInPlaceMaintenanceLease.acquire(db)
    assert caught.value.code == "MAINTENANCE_HOLD_SQLITE_BUSY"
    assert injected is True
    foreign = adversary["connection"]
    try:
        assert foreign.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert foreign.execute("SELECT value FROM marker").fetchone() == ("racer-uncommitted",)
        foreign.rollback()
    finally:
        foreign.close()

    monkeypatch.setattr(windows_maintenance.sqlite3, "connect", original_connect)
    probe = original_connect(db)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert probe.execute("PRAGMA locking_mode").fetchone() == ("normal",)
        assert int(probe.execute("PRAGMA busy_timeout").fetchone()[0]) >= 5000
        assert int(probe.execute("PRAGMA synchronous").fetchone()[0]) >= 1
        assert probe.execute("SELECT value FROM marker").fetchone() == ("old",)
    finally:
        probe.close()


@native_windows
def test_inplace_acquire_failure_after_delete_restores_runtime_regime_before_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    original_connect = sqlite3.connect
    injected = False

    class FailingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, sql: str, *args, **kwargs):
            nonlocal injected
            if sql.strip().casefold() == "pragma synchronous=full" and not injected:
                injected = True
                raise sqlite3.OperationalError("injected post-DELETE acquisition failure")
            return self._connection.execute(sql, *args, **kwargs)

        def close(self) -> None:
            self._connection.close()

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

    def failing_connect(database, *args, **kwargs):
        return FailingConnection(original_connect(database, *args, **kwargs))

    monkeypatch.setattr(windows_maintenance.sqlite3, "connect", failing_connect)
    with pytest.raises(WindowsMaintenanceError) as caught:
        WindowsInPlaceMaintenanceLease.acquire(db)
    assert caught.value.code == "MAINTENANCE_HOLD_SQLITE_BUSY"
    assert injected is True

    monkeypatch.setattr(windows_maintenance.sqlite3, "connect", original_connect)
    probe = original_connect(db)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert probe.execute("PRAGMA locking_mode").fetchone() == ("normal",)
        assert int(probe.execute("PRAGMA busy_timeout").fetchone()[0]) >= 5000
        assert int(probe.execute("PRAGMA synchronous").fetchone()[0]) >= 1
        assert probe.execute("SELECT value FROM marker").fetchone() == ("old",)
    finally:
        probe.close()


@native_windows
def test_inplace_maintenance_uses_own_sqlite_connection_holds_lock_and_never_promotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")

    def forbidden_rename(*_args, **_kwargs):
        raise AssertionError("A3 in-place maintenance must never invoke same-path promotion")

    monkeypatch.setattr(WindowsExclusiveMaintenanceLease, "_rename_handle_replace", staticmethod(forbidden_rename))
    lease = WindowsInPlaceMaintenanceLease.acquire(db)
    try:
        assert lease.connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert lease.connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert lease.connection.execute("PRAGMA locking_mode").fetchone() == ("exclusive",)
        assert lease.connection.execute("SELECT value FROM marker").fetchone() == ("old",)
        lease.connection.execute("UPDATE marker SET value='in-place'")
        lease.flush_file_buffers()
        lease.revalidate()

        outsider = sqlite3.connect(db, timeout=0.0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError):
                outsider.execute("SELECT value FROM marker").fetchone()
        finally:
            outsider.close()
        lease.prepare_runtime_activation()
        assert lease.connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert lease.connection.execute("PRAGMA synchronous").fetchone() == (1,)
        assert lease.connection.execute("PRAGMA locking_mode").fetchone() == ("normal",)
        assert int(lease.connection.execute("PRAGMA busy_timeout").fetchone()[0]) >= 5000
        direct_writer = sqlite3.connect(db, timeout=0.0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError):
                direct_writer.execute("BEGIN IMMEDIATE")
        finally:
            direct_writer.close()
        with pytest.raises(WindowsMaintenanceError) as runtime_still_blocked:
            WindowsRuntimeDBLease.open(db)
        assert runtime_still_blocked.value.code == "MAINTENANCE_IN_PROGRESS"
    finally:
        lease.close()

    post_release = sqlite3.connect(db, timeout=0.0, isolation_level=None)
    try:
        assert post_release.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert post_release.execute("PRAGMA locking_mode").fetchone() == ("normal",)
    finally:
        post_release.close()
    assert _read_marker(db) == "in-place"


@native_windows
def test_inplace_backup_activation_restores_wal_normal_without_self_checkpoint_lock(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    stage = tmp_path / "stage.db"
    _make_runtime_db(db, "old")
    _make_db(stage, "replacement")

    lease = WindowsInPlaceMaintenanceLease.acquire(db)
    try:
        stage_conn = sqlite3.connect(stage, timeout=0.0, isolation_level=None)
        try:
            stage_conn.backup(lease.connection)
        finally:
            stage_conn.close()

        assert lease.connection.execute("SELECT value FROM marker").fetchone() == ("replacement",)
        lease.prepare_runtime_activation()
        assert lease.connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert lease.connection.execute("PRAGMA synchronous").fetchone() == (1,)
        assert lease.connection.execute("PRAGMA locking_mode").fetchone() == ("normal",)
        assert int(lease.connection.execute("PRAGMA busy_timeout").fetchone()[0]) >= 5000

        foreign = sqlite3.connect(db, timeout=0.0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError):
                foreign.execute("BEGIN IMMEDIATE")
        finally:
            foreign.close()
    finally:
        lease.close()

    assert _read_marker(db) == "replacement"


@native_windows
def test_activation_failure_retains_same_live_sqlite_exclusion_after_wal_transition(
    tmp_path: Path,
) -> None:
    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    lease = WindowsInPlaceMaintenanceLease.acquire(db)
    original_connection = lease.connection
    original_connect = sqlite3.connect
    observations: dict[str, object] = {}
    injected = False

    class ActivationFailureConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, sql: str, *args, **kwargs):
            nonlocal injected
            normalized = sql.strip().casefold()
            if normalized == "pragma journal_mode" and not injected:
                injected = True
                observations["journal_before_failure"] = self._connection.execute("PRAGMA journal_mode").fetchone()
                observations["locking_before_failure"] = self._connection.execute("PRAGMA locking_mode").fetchone()
                foreign = original_connect(db, timeout=0.0, isolation_level=None)
                try:
                    with pytest.raises(sqlite3.OperationalError):
                        foreign.execute("BEGIN IMMEDIATE")
                    observations["foreign_blocked_before_failure"] = True
                finally:
                    foreign.close()
                raise sqlite3.OperationalError("injected activation failure after WAL/NORMAL configuration")
            return self._connection.execute(sql, *args, **kwargs)

        def close(self) -> None:
            self._connection.close()

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

    proxy = ActivationFailureConnection(original_connection)
    lease.connection = proxy
    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            lease.prepare_runtime_activation()
        assert caught.value.code == "MAINTENANCE_ACTIVATION_UNPROVEN"
        assert observations == {
            "journal_before_failure": ("wal",),
            "locking_before_failure": ("normal",),
            "foreign_blocked_before_failure": True,
        }
        assert lease._retained is True

        foreign_after = original_connect(db, timeout=0.0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError):
                foreign_after.execute("BEGIN IMMEDIATE")
        finally:
            foreign_after.close()
        lease.close()
        foreign_after_close = original_connect(db, timeout=0.0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError):
                foreign_after_close.execute("BEGIN IMMEDIATE")
        finally:
            foreign_after_close.close()
    finally:
        original_connection.close()


@native_windows
def test_inplace_maintenance_native_handle_blocks_rename_and_replacement(tmp_path: Path) -> None:
    import pywintypes
    import win32con
    import win32file

    db_parent = tmp_path / "db-root"
    db_parent.mkdir()
    db = db_parent / "memory.db"
    replacement = db_parent / "replacement.db"
    moved = db_parent / "moved.db"
    moved_parent = tmp_path / "db-root-moved"
    _make_runtime_db(db, "old")
    _make_db(replacement, "new")

    lease = WindowsInPlaceMaintenanceLease.acquire(db)
    try:
        with pytest.raises(pywintypes.error) as delete_open:
            win32file.CreateFile(
                str(db),
                win32con.DELETE,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
                None,
                win32con.OPEN_EXISTING,
                win32con.FILE_ATTRIBUTE_NORMAL,
                None,
            )
        assert delete_open.value.winerror == 32

        source = win32file.CreateFile(
            str(replacement),
            win32con.GENERIC_READ | win32con.GENERIC_WRITE | win32con.DELETE,
            win32con.FILE_SHARE_DELETE,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_ATTRIBUTE_NORMAL | getattr(win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0x00200000),
            None,
        )
        try:
            with pytest.raises(WindowsMaintenanceError) as replace_blocked:
                WindowsExclusiveMaintenanceLease._rename_handle_replace(source, str(db))
            assert replace_blocked.value.code in {"MAINTENANCE_HOLD_CONTENTION", "PROMOTION_FAILED"}
        finally:
            source.Close()

        with pytest.raises(OSError):
            db.rename(moved)
        assert db.exists()
        assert not moved.exists()
        with pytest.raises(OSError):
            db_parent.rename(moved_parent)
        assert db_parent.is_dir()
        assert not moved_parent.exists()
        assert lease.revalidate().file_id == lease.identity.file_id
    finally:
        lease.prepare_runtime_activation()
        lease.close()


@native_windows
def test_inplace_maintenance_crash_releases_sentinel_and_sqlite_lock(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    code = (
        "import sys\n"
        "from factlane.windows_maintenance import WindowsInPlaceMaintenanceLease\n"
        "lease=WindowsInPlaceMaintenanceLease.acquire(sys.argv[1])\n"
        "print('READY', flush=True)\n"
        "sys.stdin.read()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(db)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        with pytest.raises(WindowsMaintenanceError) as blocked:
            WindowsRuntimeDBLease.open(db)
        assert blocked.value.code == "MAINTENANCE_IN_PROGRESS"
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    with WindowsRuntimeDBLease.open(db):
        pass


@native_windows
def test_inplace_maintenance_release_unmutated_restores_runtime_regime_and_releases_sentinel(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")

    lease = WindowsInPlaceMaintenanceLease.acquire(db)
    assert lease.connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    lease.release_unmutated()

    runtime = WindowsRuntimeDBLease.open(db)
    try:
        connection = sqlite3.connect(db)
        try:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            assert int(connection.execute("PRAGMA synchronous").fetchone()[0]) >= 1
            assert connection.execute("SELECT value FROM marker").fetchone() == ("old",)
        finally:
            connection.close()
    finally:
        runtime.close()


@native_windows
def test_inplace_maintenance_plain_close_is_fail_closed_until_process_exit(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_runtime_db(db, "old")
    code = (
        "import sys\n"
        "from factlane.windows_maintenance import WindowsInPlaceMaintenanceLease, WindowsMaintenanceError, WindowsRuntimeDBLease\n"
        "lease=WindowsInPlaceMaintenanceLease.acquire(sys.argv[1])\n"
        "try:\n"
        "    lease.close()\n"
        "except WindowsMaintenanceError as exc:\n"
        "    print('CLOSE', exc.code, flush=True)\n"
        "try:\n"
        "    runtime=WindowsRuntimeDBLease.open(sys.argv[1])\n"
        "except WindowsMaintenanceError as exc:\n"
        "    print('RUNTIME', exc.code, flush=True)\n"
        "else:\n"
        "    runtime.close(); print('RUNTIME ACQUIRED', flush=True)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(db)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "CLOSE MAINTENANCE_ACTIVATION_REQUIRED",
        "RUNTIME MAINTENANCE_IN_PROGRESS",
    ]

    with WindowsRuntimeDBLease.open(db):
        pass


@native_windows
def test_reparse_parent_is_rejected_before_runtime_database_creation(tmp_path: Path) -> None:
    real = tmp_path / "real"
    junction = tmp_path / "junction"
    real.mkdir()
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(real)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr or completed.stdout}")
    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            WindowsRuntimeDBLease.open(junction / "memory.db")
        assert caught.value.code == "WINDOWS_REPARSE_DENIED"
        assert not (real / "memory.db").exists()
    finally:
        try:
            junction.rmdir()
        except OSError:
            pass


@native_windows
def test_barrier_and_replacement_paths_reject_junctions_before_mutation(tmp_path: Path) -> None:
    from factlane.windows_security import current_user_sid, ensure_owner_private_directory

    sid = current_user_sid()
    real = tmp_path / "real-owner-private"
    junction = tmp_path / "owner-private-junction"
    ensure_owner_private_directory(real, owner_sid=sid)
    replacement = real / "replacement.db"
    _make_db(replacement, "replacement")
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(real)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr or completed.stdout}")
    try:
        with pytest.raises(WindowsMaintenanceError) as barrier_error:
            WindowsBarrierGuard.acquire(junction / "maintenance.lock", owner_sid=sid)
        assert barrier_error.value.code == "WINDOWS_REPARSE_DENIED"
        assert not (real / "maintenance.lock").exists()

        db = tmp_path / "memory.db"
        _make_db(db, "old")
        lease = WindowsExclusiveMaintenanceLease.acquire(db)
        try:
            with pytest.raises(WindowsMaintenanceError) as replacement_error:
                lease.promote_replacement(junction / "replacement.db")
            assert replacement_error.value.code == "PROMOTION_UNSUPPORTED"
            assert replacement.exists()
        finally:
            lease.close()
        assert _read_marker(db) == "old"
    finally:
        try:
            junction.rmdir()
        except OSError:
            pass


@native_windows
def test_barrier_is_owner_private_nonblocking_and_rejects_unsafe_dacl(tmp_path: Path) -> None:
    import win32security

    from factlane.windows_security import (
        current_user_sid,
        ensure_owner_private_directory,
        validate_owner_private_path,
    )

    sid = current_user_sid()
    private = tmp_path / "owner-private"
    ensure_owner_private_directory(private, owner_sid=sid)
    lock = private / "maintenance.lock"

    with pytest.raises(WindowsMaintenanceError) as foreign_owner:
        WindowsBarrierGuard.acquire(lock, owner_sid="S-1-5-18")
    assert foreign_owner.value.code == "BARRIER_OWNER_MISMATCH"
    assert not lock.exists()

    first = WindowsBarrierGuard.acquire(lock, owner_sid=sid)
    try:
        validate_owner_private_path(lock, owner_sid=sid, directory=False)
        with pytest.raises(WindowsMaintenanceError) as busy:
            WindowsBarrierGuard.acquire(lock, owner_sid=sid)
        assert busy.value.code == "BARRIER_BUSY"
    finally:
        first.close()

    with WindowsBarrierGuard.acquire(lock, owner_sid=sid):
        pass

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{sid}D:P(A;;GA;;;{sid})(A;;GR;;;WD)",
        win32security.SDDL_REVISION_1,
    )
    owner = descriptor.GetSecurityDescriptorOwner()
    dacl = descriptor.GetSecurityDescriptorDacl()
    win32security.SetNamedSecurityInfo(
        str(lock),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | getattr(win32security, "PROTECTED_DACL_SECURITY_INFORMATION", 0x80000000),
        owner,
        None,
        dacl,
        None,
    )
    with pytest.raises(WindowsMaintenanceError) as unsafe:
        WindowsBarrierGuard.acquire(lock, owner_sid=sid)
    assert unsafe.value.code == "BARRIER_PATH_UNSAFE"


@native_windows
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_exclusive_maintenance_rejects_existing_sidecar_holder_contention(tmp_path: Path, suffix: str) -> None:
    import win32con
    import win32file

    db = tmp_path / "memory.db"
    _make_db(db, "old")
    sidecar = Path(str(db) + suffix)
    sidecar.write_bytes(b"sidecar-holder")
    holder = win32file.CreateFile(
        str(sidecar),
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        None,
        win32con.OPEN_EXISTING,
        win32con.FILE_ATTRIBUTE_NORMAL,
        None,
    )
    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            WindowsExclusiveMaintenanceLease.acquire(db)
        assert caught.value.code == "MAINTENANCE_HOLD_CONTENTION"
    finally:
        holder.Close()


@native_windows
def test_exclusive_maintenance_rejects_delete_capable_rename_holder(tmp_path: Path) -> None:
    import win32con
    import win32file

    db = tmp_path / "memory.db"
    moved = tmp_path / "memory-moved.db"
    _make_db(db, "old")
    holder = win32file.CreateFile(
        str(db),
        win32con.DELETE,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_DELETE,
        None,
        win32con.OPEN_EXISTING,
        win32con.FILE_ATTRIBUTE_NORMAL | getattr(win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0x00200000),
        None,
    )
    try:
        with pytest.raises(WindowsMaintenanceError) as caught:
            WindowsExclusiveMaintenanceLease.acquire(db)
        assert caught.value.code == "MAINTENANCE_HOLD_CONTENTION"

        WindowsExclusiveMaintenanceLease._rename_handle_replace(holder, str(moved))
        assert moved.is_file()
        WindowsExclusiveMaintenanceLease._rename_handle_replace(holder, str(db))
        assert db.is_file()
    finally:
        holder.Close()


@native_windows
def test_exclusive_maintenance_keeps_existing_sidecars_share_zero_while_held(tmp_path: Path) -> None:
    import pywintypes
    import win32con
    import win32file

    db = tmp_path / "memory.db"
    _make_db(db, "old")
    sidecars = [Path(str(db) + suffix) for suffix in ("-wal", "-shm")]
    for sidecar in sidecars:
        sidecar.write_bytes(b"sidecar")

    lease = WindowsExclusiveMaintenanceLease.acquire(db)
    try:
        for sidecar in sidecars:
            with pytest.raises(pywintypes.error) as caught:
                win32file.CreateFile(
                    str(sidecar),
                    win32con.GENERIC_READ,
                    win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
                    None,
                    win32con.OPEN_EXISTING,
                    win32con.FILE_ATTRIBUTE_NORMAL,
                    None,
                )
            assert caught.value.winerror == 32
    finally:
        lease.close()


@native_windows
def test_competitor_first_delete_open_breaks_oplock_and_promotion_holds_without_mutation(tmp_path: Path) -> None:
    import win32con
    import win32file

    db = tmp_path / "memory.db"
    replacement = tmp_path / "replacement.db"
    _make_db(db, "old")
    _make_db(replacement, "replacement")

    lease = WindowsExclusiveMaintenanceLease.acquire(db)
    old_id = lease.main_identity.file_id
    competitor_started = threading.Event()
    competitor_returned = threading.Event()
    competitor_handles: list[object] = []
    competitor_errors: list[BaseException] = []

    def competitor() -> None:
        competitor_started.set()
        try:
            competitor_handles.append(
                win32file.CreateFile(
                    str(db),
                    win32con.DELETE,
                    win32con.FILE_SHARE_READ | win32con.FILE_SHARE_DELETE,
                    None,
                    win32con.OPEN_EXISTING,
                    win32con.FILE_ATTRIBUTE_NORMAL | getattr(win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0x00200000),
                    None,
                )
            )
        except BaseException as exc:
            competitor_errors.append(exc)
        finally:
            competitor_returned.set()

    thread = threading.Thread(target=competitor, daemon=True)
    thread.start()
    try:
        assert competitor_started.wait(timeout=2)
        assert lease._main_oplock is not None
        assert lease._main_oplock.wait_for_break(5000)
        break_info = lease._main_oplock.consume_break()
        assert break_info.original_level == windows_maintenance._OPLOCK_LEVEL_BATCH_EQUIVALENT
        assert break_info.new_level == windows_maintenance._OPLOCK_LEVEL_CACHE_READ | windows_maintenance._OPLOCK_LEVEL_CACHE_HANDLE
        assert break_info.acknowledgment_required is True
        assert break_info.modes_provided is False
        assert competitor_returned.is_set() is False

        with pytest.raises(WindowsMaintenanceError) as caught:
            lease.promote_replacement(replacement)
        assert caught.value.code == "PROMOTION_UNSUPPORTED"
        assert replacement.exists()
        assert lease.main_identity.file_id == old_id
        assert competitor_returned.is_set() is False
    finally:
        lease.close()
        thread.join(timeout=5)
        for handle in competitor_handles:
            handle.Close()

    assert thread.is_alive() is False
    assert not competitor_errors
    assert _read_marker(db) == "old"
    assert _read_marker(replacement) == "replacement"


@native_windows
def test_legacy_promotion_is_quarantined_before_any_rename_or_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "memory.db"
    replacement = tmp_path / "replacement.db"
    _make_db(db, "old")
    _make_db(replacement, "replacement")
    calls = {"rename": 0, "ack": 0}

    def forbidden_rename(*_args, **_kwargs):
        calls["rename"] += 1
        raise AssertionError("quarantined promotion must not reach rename")

    def forbidden_ack(*_args, **_kwargs):
        calls["ack"] += 1
        raise AssertionError("quarantined promotion must not ACK an oplock break")

    monkeypatch.setattr(WindowsExclusiveMaintenanceLease, "_rename_handle_replace", staticmethod(forbidden_rename))
    monkeypatch.setattr(windows_maintenance._WindowsBatchOplock, "acknowledge_to_none", forbidden_ack)
    lease = WindowsExclusiveMaintenanceLease.acquire(db)
    try:
        with pytest.raises(WindowsMaintenanceError) as blocked:
            lease.promote_replacement(replacement)
        assert blocked.value.code == "PROMOTION_UNSUPPORTED"
        assert calls == {"rename": 0, "ack": 0}
    finally:
        lease.close()

    assert _read_marker(db) == "old"
    assert _read_marker(replacement) == "replacement"


@native_windows
def test_exclusive_maintenance_rejects_sidecar_set_that_changes_during_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "memory.db"
    _make_db(db, "old")
    original_scan = windows_maintenance._scan_sidecars
    calls = 0

    def changing_scan(path: str) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        if calls == 2:
            Path(path + "-wal").write_bytes(b"appeared-during-acquire")
        return original_scan(path)

    monkeypatch.setattr(windows_maintenance, "_scan_sidecars", changing_scan)
    with pytest.raises(WindowsMaintenanceError) as caught:
        WindowsExclusiveMaintenanceLease.acquire(db)
    assert caught.value.code == "MAINTENANCE_HOLD_SIDECAR_UNCERTAIN"
    assert calls >= 2


@native_windows
def test_native_promotion_api_is_fail_closed_and_does_not_open_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "memory.db"
    replacement = tmp_path / "replacement.db"
    _make_db(db, "old")
    _make_db(replacement, "replacement")

    original_open = windows_maintenance._open_file
    promotion_source_share_modes: list[int] = []

    def recording_open(path: str, **kwargs):
        if (
            windows_maintenance._path_key(path) == windows_maintenance._path_key(str(replacement))
            and int(kwargs.get("desired_access", 0)) & windows_maintenance._DELETE
        ):
            promotion_source_share_modes.append(int(kwargs["share_mode"]))
        return original_open(path, **kwargs)

    monkeypatch.setattr(windows_maintenance, "_open_file", recording_open)

    lease = WindowsExclusiveMaintenanceLease.acquire(db)
    try:
        with pytest.raises(WindowsMaintenanceError) as blocked:
            lease.promote_replacement(replacement)
        assert blocked.value.code == "PROMOTION_UNSUPPORTED"
        assert promotion_source_share_modes == []
    finally:
        lease.close()

    assert _read_marker(db) == "old"
    assert _read_marker(replacement) == "replacement"


@native_windows
def test_retain_fail_closed_keeps_exclusion_until_process_exit(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _make_db(db, "old")
    code = (
        "import sys\n"
        "from factlane.windows_maintenance import WindowsExclusiveMaintenanceLease\n"
        "lease=WindowsExclusiveMaintenanceLease.acquire(sys.argv[1])\n"
        "lease.retain_fail_closed()\n"
        "lease.close()\n"
        "print('READY', flush=True)\n"
        "sys.stdin.read()\n"
    )
    runtime_done = threading.Event()
    runtime_errors: list[BaseException] = []

    def runtime_open() -> None:
        try:
            with WindowsRuntimeDBLease.open(db):
                pass
        except BaseException as exc:
            runtime_errors.append(exc)
        finally:
            runtime_done.set()

    child = subprocess.Popen(
        [sys.executable, "-c", code, str(db)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        runtime_thread = threading.Thread(target=runtime_open, daemon=True)
        runtime_thread.start()
        time.sleep(0.1)
        assert runtime_done.is_set() is False
    finally:
        if child.stdin is not None:
            child.stdin.close()
        child.wait(timeout=10)

    runtime_thread.join(timeout=5)
    assert runtime_done.is_set() is True
    assert runtime_errors == []


@native_windows
def test_malformed_windows_path_is_rejected_without_file_creation(tmp_path: Path) -> None:
    relative = Path("relative-maintenance.db")
    with pytest.raises(WindowsMaintenanceError) as caught:
        WindowsRuntimeDBLease.open(relative)
    assert caught.value.code == "WINDOWS_PATH_INVALID"
    assert not relative.exists()
