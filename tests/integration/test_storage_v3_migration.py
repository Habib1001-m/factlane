from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import urllib.parse
from pathlib import Path

import pytest

import factlane.storage as storage_module
import factlane.storage_migration as migration_module
from factlane.contract import AdapterError
from factlane.embeddings import EmbeddingProfile
from factlane.storage import STORAGE_CONTRACT_VERSION, SQLiteVecEngine, inspect_storage_contract_version
from factlane.storage_migration import migrate_storage_v3, restore_pre_v3_backup


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="v3-test-256",
        provider_kind="OLLAMA_LOCAL",
        base_model_identity="nomic-embed-text:latest",
        model_digest="0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
        source_dimension=768,
        output_dimension=256,
        normalization_policy="OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
        distance_metric="cosine",
        projection_version="ollama-dimensions-v1",
        document_prefix="search_document: ",
        query_prefix="search_query: ",
    )


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _assert_windows_maintenance_hold_no_mutation(db: Path, backup: Path) -> None:
    """Windows qualifies the unsupported maintenance boundary, not POSIX migration execution."""
    before = _digest(db)
    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "MIGRATION_HOLD"
    assert "POSIX fcntl/flock" in exc_info.value.safe_message
    assert _digest(db) == before
    assert not backup.exists()
    assert not Path(str(backup) + ".factlane-v3-backup.json").exists()
    assert inspect_storage_contract_version(str(db)) == 2


def _seed_contract_only(path: Path, version: int) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO adapter_meta(key,value) VALUES ('contract_version',?)", (str(version),))
        conn.commit()
    finally:
        conn.close()


def _seed_full_v2(path: Path, *, duplicate_revision: bool = False) -> None:
    async def seed_pinned_native_image() -> None:
        engine = SQLiteVecEngine(str(path), _profile())
        await engine.open()
        try:
            assert engine.conn is not None
            profile = _profile()
            profile_metadata = {
                "metadata_revision": 2,
                "store_key": profile.profile_id,
                "semantic_identity": profile.semantic_identity(),
                "compatibility_proof": {
                    "state": "COMPATIBLE",
                    "basis": "TEST_PINNED_NATIVE_V2_FIXTURE",
                    "reason": None,
                    "anchor_bundle_id": None,
                    "anchor_bundle_sha256": None,
                    "minimum_document_cross_space_cosine": None,
                    "minimum_query_match_cosine": None,
                    "minimum_query_top1_margin": None,
                },
                "runtime_provenance": {
                    "provider_kind": profile.provider_kind,
                    "model": profile.base_model_identity,
                    "model_digest": profile.model_digest,
                    "ollama_version": "test-fixture",
                    "semantic_family": profile.semantic_family,
                    "native_dimension": profile.source_dimension,
                    "output_dimension": profile.output_dimension,
                    "effective_context_window": profile.minimum_context_window,
                },
                "runtime_fingerprint": "f" * 64,
            }
            engine.conn.execute(
                "INSERT INTO adapter_meta(key,value) VALUES ('embedding_profile',?)",
                (json.dumps(profile_metadata, sort_keys=True, separators=(",", ":")),),
            )
            engine.conn.commit()
            record = {
                "record_id": "11111111-1111-4111-8111-111111111111",
                "memory_id": "22222222-2222-4222-8222-222222222222",
                "revision": 1,
                "parent_record_id": None,
                "scope": "PROJECT",
                "project_id": "p1",
                "worktree_id": None,
                "workflow_id": None,
                "agent_id": None,
                "memory_type": "PROJECT_LEARNED_FACT",
                "fact": "The legacy fact remains unchanged.",
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "legacy-v2-fixture",
                    "source_hash": "a" * 64,
                    "review_ref": "legacy-v2-fixture",
                    "extraction_method": "fixture",
                },
                "source_timestamp": "2026-10-01T00:00:00Z",
                "created_at": "2026-10-01T00:00:00Z",
                "last_verified_at": "2026-10-01T00:00:00Z",
                "verified_by": "OWNER",
                "authority_role": "PROJECT_CURRENT",
                "contribution_origin": {"contributor_class": "OWNER", "contributor_ref": "migration-fixture"},
                "freshness_policy": {"kind": "manual"},
                "supersedes": [],
                "contradiction_key": "k1",
                "contradiction_state": "NONE",
                "confidence": 1.0,
                "tags": [],
                "lifecycle_state": "VALIDATED_CURRENT",
                "native_content_hash": "a" * 64,
                "payload_fingerprint": "b" * 64,
                "idempotency_key": "legacy-key-1",
                "embedding_profile_id": profile.profile_id,
                "embedding_model_digest": profile.model_digest,
                "embedding_output_dimension": profile.output_dimension,
            }
            await engine.write_record(record, [1.0] + [0.0] * 255)
        finally:
            await engine.close()

    asyncio.run(seed_pinned_native_image())
    conn = sqlite3.connect(path)
    try:
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'factlane_v3_%'"
        ).fetchall():
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP TABLE lifecycle_events")
        conn.execute("DROP TABLE lifecycle_operations")
        conn.execute("DROP INDEX uq_adapter_memory_revision")
        conn.execute("DROP INDEX uq_adapter_parent_record")
        conn.execute("UPDATE adapter_meta SET value='2' WHERE key='contract_version'")
        conn.execute("DELETE FROM adapter_meta WHERE key='trust_clock_high_water_at'")
        for name, action in (
            ("factlane_v2_adapter_insert_fence", "INSERT"),
            ("factlane_v2_adapter_update_fence", "UPDATE"),
            ("factlane_v2_adapter_delete_fence", "DELETE"),
        ):
            conn.execute(
                f"CREATE TRIGGER {name} BEFORE {action} ON adapter_records "
                "WHEN factlane_contract_v2_compat_writer() != 1 BEGIN "
                "SELECT RAISE(ABORT, 'FACTLANE_STORAGE_V2_COMPAT_WRITER_REQUIRED'); END"
            )
        if duplicate_revision:
            conn.create_function("factlane_contract_v2_compat_writer", 0, lambda: 1)
            columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(adapter_records)").fetchall()]
            original = dict(zip(columns, conn.execute("SELECT * FROM adapter_records LIMIT 1").fetchone(), strict=True))
            original.update(
                record_id="33333333-3333-4333-8333-333333333333",
                native_content_hash="d" * 64,
                payload_fingerprint="e" * 64,
                idempotency_key="legacy-key-2",
                lifecycle_state="HISTORICAL",
                contradiction_state="RESOLVED",
            )
            placeholders = ",".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO adapter_records({','.join(columns)}) VALUES ({placeholders})",
                tuple(original[column] for column in columns),
            )
        conn.commit()
    finally:
        conn.close()


def test_fresh_runtime_creates_storage_v3_schema() -> None:
    async def run(tmp_path: Path) -> None:
        db = tmp_path / "fresh-v3.db"
        engine = SQLiteVecEngine(str(db), _profile())
        await engine.open()
        try:
            assert STORAGE_CONTRACT_VERSION == 3
            assert engine.conn is not None
            assert engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='contract_version'"
            ).fetchone()[0] == "3"
            tables = {
                row[0]
                for row in engine.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert {"adapter_records", "lifecycle_events", "lifecycle_operations"}.issubset(tables)
            indexes = {
                row[0]
                for row in engine.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            assert {
                "uq_adapter_memory_revision",
                "uq_adapter_parent_record",
                "uq_lifecycle_event_target_generation",
                "idx_lifecycle_event_target_generation",
            }.issubset(indexes)
            assert engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
            ).fetchone()[0] == "1970-01-01T00:00:00Z"
        finally:
            await engine.close()
        assert inspect_storage_contract_version(str(db)) == 3

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        asyncio.run(run(Path(td)))


def test_v2_open_fails_before_backend_initialization_and_preserves_bytes(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "legacy-v2.db"
    _seed_contract_only(db, 2)
    before = {suffix: _digest(Path(str(db) + suffix)) for suffix in ("", "-wal", "-shm")}

    def backend_must_not_load():
        raise AssertionError("pinned backend must not load before the v2 migration barrier")

    monkeypatch.setattr(storage_module, "load_pinned_sqlite_vec_storage", backend_must_not_load)
    engine = SQLiteVecEngine(str(db), _profile())
    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(engine.open())
    assert exc_info.value.code == "MIGRATION_REQUIRED"
    after = {suffix: _digest(Path(str(db) + suffix)) for suffix in ("", "-wal", "-shm")}
    assert after == before
    assert engine.storage is None
    assert engine.conn is None


@pytest.mark.parametrize("version", [1, 4, 99])
def test_unknown_or_unsupported_contract_fails_before_backend(tmp_path: Path, monkeypatch, version: int) -> None:
    db = tmp_path / f"unsupported-{version}.db"
    _seed_contract_only(db, version)
    monkeypatch.setattr(
        storage_module,
        "load_pinned_sqlite_vec_storage",
        lambda: (_ for _ in ()).throw(AssertionError("backend must not load")),
    )
    engine = SQLiteVecEngine(str(db), _profile())
    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(engine.open())
    assert exc_info.value.code == "SCHEMA_MISMATCH"


def test_v3_preflight_rejects_partial_schema(tmp_path: Path) -> None:
    db = tmp_path / "partial-v3.db"
    _seed_contract_only(db, 3)
    with pytest.raises(AdapterError) as exc_info:
        inspect_storage_contract_version(str(db))
    assert exc_info.value.code == "SCHEMA_MISMATCH"


@pytest.mark.parametrize("invalid_value", [None, "not-a-timestamp"])
def test_v3_preflight_rejects_missing_or_invalid_trust_clock_before_runtime(tmp_path: Path, invalid_value) -> None:
    db = tmp_path / "bad-v3-trust-clock.db"

    async def seed() -> None:
        engine = SQLiteVecEngine(str(db), _profile())
        await engine.open()
        await engine.close()

    asyncio.run(seed())
    conn = sqlite3.connect(db)
    try:
        if invalid_value is None:
            conn.execute("DELETE FROM adapter_meta WHERE key='trust_clock_high_water_at'")
        else:
            conn.execute("UPDATE adapter_meta SET value=? WHERE key='trust_clock_high_water_at'", (invalid_value,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(AdapterError) as exc_info:
        inspect_storage_contract_version(str(db))
    assert exc_info.value.code == "SCHEMA_MISMATCH"
    with pytest.raises(AdapterError) as runtime_error:
        asyncio.run(SQLiteVecEngine(str(db), _profile()).open())
    assert runtime_error.value.code == "SCHEMA_MISMATCH"


def test_v3_preflight_rejects_same_name_nonunique_lineage_guard_before_backend(tmp_path: Path, monkeypatch) -> None:
    async def seed() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "drifted-index-v3.db"), _profile())
        await engine.open()
        await engine.close()

    asyncio.run(seed())
    db = tmp_path / "drifted-index-v3.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP INDEX uq_adapter_memory_revision")
        conn.execute("CREATE INDEX uq_adapter_memory_revision ON adapter_records(memory_id, revision)")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        storage_module,
        "load_pinned_sqlite_vec_storage",
        lambda: (_ for _ in ()).throw(AssertionError("backend must not load for malformed v3")),
    )
    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(SQLiteVecEngine(str(db), _profile()).open())
    assert exc_info.value.code == "SCHEMA_MISMATCH"


def test_v3_preflight_rejects_same_name_drifted_governance_trigger_before_backend(tmp_path: Path, monkeypatch) -> None:
    async def seed() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "drifted-trigger-v3.db"), _profile())
        await engine.open()
        await engine.close()

    asyncio.run(seed())
    db = tmp_path / "drifted-trigger-v3.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TRIGGER factlane_v3_operation_global_idempotency")
        conn.execute(
            "CREATE TRIGGER factlane_v3_operation_global_idempotency BEFORE INSERT ON lifecycle_operations BEGIN SELECT 1; END"
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        storage_module,
        "load_pinned_sqlite_vec_storage",
        lambda: (_ for _ in ()).throw(AssertionError("backend must not load for malformed v3")),
    )
    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(SQLiteVecEngine(str(db), _profile()).open())
    assert exc_info.value.code == "SCHEMA_MISMATCH"


def test_v3_preflight_rejects_weakened_lifecycle_table_check_before_backend(tmp_path: Path, monkeypatch) -> None:
    async def seed() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "drifted-table-v3.db"), _profile())
        await engine.open()
        await engine.close()

    asyncio.run(seed())
    db = tmp_path / "drifted-table-v3.db"
    conn = sqlite3.connect(db)
    try:
        for name in (
            "factlane_v3_lifecycle_events_insert_fence",
            "factlane_v3_lifecycle_events_update_fence",
            "factlane_v3_lifecycle_events_delete_fence",
        ):
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP INDEX uq_lifecycle_event_target_generation")
        conn.execute("DROP INDEX idx_lifecycle_event_target_generation")
        conn.execute("DROP INDEX idx_lifecycle_event_memory")
        conn.execute("ALTER TABLE lifecycle_events RENAME TO lifecycle_events_strict")
        strict_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='lifecycle_events_strict'"
        ).fetchone()[0]
        weakened_sql = strict_sql.replace("lifecycle_events_strict", "lifecycle_events", 1)
        weakened_sql = weakened_sql.replace(
            ",\n                    CHECK (freshness_generation IS NULL OR freshness_generation > 0)",
            "",
        )
        conn.execute(weakened_sql)
        conn.execute("DROP TABLE lifecycle_events_strict")
        conn.execute(
            "CREATE UNIQUE INDEX uq_lifecycle_event_target_generation "
            "ON lifecycle_events(target_record_id,target_revision,freshness_generation) "
            "WHERE freshness_generation IS NOT NULL"
        )
        conn.execute(
            "CREATE INDEX idx_lifecycle_event_target_generation "
            "ON lifecycle_events(target_record_id,target_revision,freshness_generation DESC) "
            "WHERE freshness_generation IS NOT NULL"
        )
        conn.execute("CREATE INDEX idx_lifecycle_event_memory ON lifecycle_events(memory_id,occurred_at,event_id)")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        storage_module,
        "load_pinned_sqlite_vec_storage",
        lambda: (_ for _ in ()).throw(AssertionError("backend must not load for malformed v3")),
    )
    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(SQLiteVecEngine(str(db), _profile()).open())
    assert exc_info.value.code == "SCHEMA_MISMATCH"


def test_explicit_v2_to_v3_migration_creates_verified_backup_and_restores(tmp_path: Path) -> None:
    db = tmp_path / "migrate.db"
    backup = tmp_path / "migrate.pre-v3.db"
    _seed_full_v2(db)
    if os.name == "nt":
        _assert_windows_maintenance_hold_no_mutation(db, backup)
        return
    result = migrate_storage_v3(str(db), str(backup))
    assert result["status"] == "OK"
    assert result["from_contract"] == 2
    assert result["to_contract"] == 3
    assert backup.is_file()
    assert result["backup_sha256"] == _digest(backup)
    assert Path(str(backup) + ".factlane-v3-backup.json").is_file()
    assert inspect_storage_contract_version(str(backup)) == 2
    assert inspect_storage_contract_version(str(db)) == 3
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT fact FROM adapter_records WHERE record_id='11111111-1111-4111-8111-111111111111'"
        ).fetchone()[0] == "The legacy fact remains unchanged."
        native = conn.execute("SELECT id,content_hash,store FROM memories WHERE deleted_at IS NULL").fetchone()
        assert native[1:] == ("a" * 64, _profile().profile_id)
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_embeddings_rowids WHERE rowid=?", (native[0],)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
        ).fetchone()[0] == "2026-10-01T00:00:00Z"
    finally:
        conn.close()
    restored = restore_pre_v3_backup(str(db), str(backup))
    assert restored["status"] == "OK"
    assert restored["restored_contract"] == 2
    assert inspect_storage_contract_version(str(db)) == 2
    assert _digest(db) == _digest(backup)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT fact FROM adapter_records WHERE record_id='11111111-1111-4111-8111-111111111111'"
        ).fetchone()[0] == "The legacy fact remains unchanged."
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE content_hash=?", ("a" * 64,)).fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_backup_stage_is_private_before_copy_even_with_permissive_umask(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    db = tmp_path / "private-stage.db"
    backup = tmp_path / "private-stage.pre-v3.db"
    _seed_full_v2(db)
    source = sqlite3.connect(db)
    parent_fd = migration_module._open_private_backup_parent(backup)
    observed_during_copy: list[tuple[int, int]] = []
    try:
        source_snapshot = migration_module._logical_parity_snapshot(source)

        class SourceProxy:
            def backup(self, destination: sqlite3.Connection) -> None:
                stages = list(tmp_path.glob(".factlane-*.stage"))
                assert len(stages) == 1
                stage_stat = os.stat(stages[0], follow_symlinks=False)
                parent_stat = os.stat(tmp_path, follow_symlinks=False)
                observed_during_copy.append(
                    (stat.S_IMODE(stage_stat.st_mode), stat.S_IMODE(parent_stat.st_mode))
                )
                assert not backup.exists()
                source.backup(destination)

        prior_umask = os.umask(0)
        try:
            backup_sha = migration_module._backup_database(
                SourceProxy(),  # type: ignore[arg-type]
                backup,
                parent_fd=parent_fd,
                source_db=db,
                source_snapshot=source_snapshot,
            )
        finally:
            os.umask(prior_umask)
    finally:
        os.close(parent_fd)
        source.close()

    manifest = Path(str(backup) + ".factlane-v3-backup.json")
    assert observed_during_copy == [(0o600, 0o700)]
    assert backup_sha == _digest(backup)
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(manifest).st_mode) == 0o600
    assert not list(tmp_path.glob(".factlane-*.stage*"))


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_backup_verification_failure_removes_private_stage_without_publishing(tmp_path: Path, monkeypatch) -> None:
    os.chmod(tmp_path, 0o700)
    db = tmp_path / "backup-failure.db"
    backup = tmp_path / "backup-failure.pre-v3.db"
    _seed_full_v2(db)
    source_before = _digest(db)
    original_integrity = migration_module._integrity
    calls = 0

    def fail_backup_integrity(conn: sqlite3.Connection) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise migration_module.MigrationHold("MIGRATION_HOLD", "injected backup verification failure")
        original_integrity(conn)

    monkeypatch.setattr(migration_module, "_integrity", fail_backup_integrity)
    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "MIGRATION_HOLD"
    assert _digest(db) == source_before
    assert inspect_storage_contract_version(str(db)) == 2
    assert not backup.exists()
    assert not Path(str(backup) + ".factlane-v3-backup.json").exists()
    assert not list(tmp_path.glob(".factlane-*.stage*"))


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
@pytest.mark.parametrize("destination_kind", ["regular", "symlink", "directory"])
def test_backup_existing_destination_is_never_followed_or_overwritten(
    tmp_path: Path,
    destination_kind: str,
) -> None:
    os.chmod(tmp_path, 0o700)
    db = tmp_path / f"existing-{destination_kind}.db"
    backup = tmp_path / f"existing-{destination_kind}.pre-v3.db"
    sentinel = tmp_path / f"sentinel-{destination_kind}.bin"
    _seed_full_v2(db)
    sentinel.write_bytes(b"sentinel-bytes")
    if destination_kind == "regular":
        backup.write_bytes(b"existing-backup")
        expected = b"existing-backup"
    elif destination_kind == "symlink":
        backup.symlink_to(sentinel)
        expected = b"sentinel-bytes"
    else:
        backup.mkdir()
        expected = None

    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "BACKUP_EXISTS"
    assert inspect_storage_contract_version(str(db)) == 2
    if destination_kind == "regular":
        assert backup.read_bytes() == expected
    elif destination_kind == "symlink":
        assert backup.is_symlink()
        assert sentinel.read_bytes() == expected
    else:
        assert backup.is_dir()
    assert not Path(str(backup) + ".factlane-v3-backup.json").exists()
    assert not list(tmp_path.glob(".factlane-*.stage*"))


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_backup_parent_must_be_private_before_sensitive_copy(tmp_path: Path) -> None:
    db = tmp_path / "shared-parent.db"
    backup_parent = tmp_path / "shared-backups"
    backup_parent.mkdir(mode=0o700)
    os.chmod(backup_parent, 0o750)
    backup = backup_parent / "shared-parent.pre-v3.db"
    _seed_full_v2(db)
    before = _digest(db)

    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "MIGRATION_HOLD"
    assert "operator-owned and private" in exc_info.value.safe_message
    assert _digest(db) == before
    assert not backup.exists()
    assert not list(backup_parent.glob(".factlane-*.stage*"))


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_backup_parent_must_be_owned_by_current_operator_before_sensitive_copy(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "wrong-owner.db"
    backup_parent = tmp_path / "wrong-owner-backups"
    backup_parent.mkdir(mode=0o700)
    os.chmod(backup_parent, 0o700)
    backup = backup_parent / "wrong-owner.pre-v3.db"
    _seed_full_v2(db)
    before = _digest(db)
    real_euid = os.geteuid()
    monkeypatch.setattr(migration_module.os, "geteuid", lambda: real_euid + 1)

    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "MIGRATION_HOLD"
    assert "operator-owned and private" in exc_info.value.safe_message
    assert _digest(db) == before
    assert not backup.exists()
    assert not list(backup_parent.glob(".factlane-*.stage*"))


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_backup_path_rejects_third_party_owned_ancestor_before_sensitive_copy(tmp_path: Path, monkeypatch) -> None:
    ancestor = tmp_path / "foreign-ancestor"
    backup_parent = ancestor / "private-backups"
    backup_parent.mkdir(parents=True, mode=0o700)
    os.chmod(ancestor, 0o755)
    os.chmod(backup_parent, 0o700)
    backup = backup_parent / "ancestor-owner.pre-v3.db"
    real_stat = os.stat
    real_euid = os.geteuid()

    def stat_with_foreign_ancestor(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if kwargs.get("dir_fd") is None and Path(path) == ancestor:
            values = list(result)
            values[4] = real_euid + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(migration_module.os, "stat", stat_with_foreign_ancestor)
    monkeypatch.setattr(
        migration_module.os,
        "supports_dir_fd",
        {*os.supports_dir_fd, stat_with_foreign_ancestor},
    )
    monkeypatch.setattr(
        migration_module.os,
        "supports_follow_symlinks",
        {*os.supports_follow_symlinks, stat_with_foreign_ancestor},
    )
    with pytest.raises(AdapterError) as exc_info:
        migration_module._open_private_backup_parent(backup)
    assert exc_info.value.code == "MIGRATION_HOLD"
    assert "untrusted OS identity" in exc_info.value.safe_message
    assert not backup.exists()
    assert not list(backup_parent.glob(".factlane-*.stage*"))


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_backup_concurrent_final_creator_wins_without_overwrite(tmp_path: Path, monkeypatch) -> None:
    os.chmod(tmp_path, 0o700)
    db = tmp_path / "concurrent-backup.db"
    backup = tmp_path / "concurrent-backup.pre-v3.db"
    _seed_full_v2(db)
    original_publish = migration_module._publish_private_stage_noreplace
    injected = False

    def racing_publish(parent_fd: int, stage_name: str, stage_identity: tuple[int, int], final_name: str, **kwargs) -> None:
        nonlocal injected
        if final_name == backup.name and not injected:
            injected = True
            fd = os.open(
                final_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                os.write(fd, b"concurrent-winner")
                os.fsync(fd)
            finally:
                os.close(fd)
        original_publish(parent_fd, stage_name, stage_identity, final_name, **kwargs)

    monkeypatch.setattr(migration_module, "_publish_private_stage_noreplace", racing_publish)
    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "BACKUP_EXISTS"
    assert backup.read_bytes() == b"concurrent-winner"
    assert inspect_storage_contract_version(str(db)) == 2
    assert not Path(str(backup) + ".factlane-v3-backup.json").exists()
    assert not list(tmp_path.glob(".factlane-*.stage*"))


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_backup_stage_path_replacement_is_detected_before_source_copy(tmp_path: Path, monkeypatch) -> None:
    os.chmod(tmp_path, 0o700)
    db = tmp_path / "stage-replacement.db"
    backup = tmp_path / "stage-replacement.pre-v3.db"
    _seed_full_v2(db)
    source = sqlite3.connect(db)
    parent_fd = migration_module._open_private_backup_parent(backup)
    source_snapshot = migration_module._logical_parity_snapshot(source)
    original_connect = sqlite3.connect
    replaced_paths: list[Path] = []

    def replace_before_open(database, *args, **kwargs):
        if kwargs.get("uri") and isinstance(database, str) and database.startswith("file:"):
            encoded_path = database[5:].split("?", 1)[0]
            stage = Path(urllib.parse.unquote(encoded_path))
            stage.unlink()
            stage.write_bytes(b"same-user-replacement")
            os.chmod(stage, 0o600)
            replaced_paths.append(stage)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(migration_module.sqlite3, "connect", replace_before_open)
    try:
        with pytest.raises(AdapterError) as exc_info:
            migration_module._backup_database(
                source,
                backup,
                parent_fd=parent_fd,
                source_db=db,
                source_snapshot=source_snapshot,
            )
        assert exc_info.value.code == "MIGRATION_HOLD"
    finally:
        os.close(parent_fd)
        source.close()

    assert replaced_paths
    assert not backup.exists()
    assert all(path.read_bytes() == b"same-user-replacement" for path in replaced_paths)
    assert b"The legacy fact remains unchanged." not in replaced_paths[0].read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_backup_stage_removal_cannot_be_recreated_by_sqlite(tmp_path: Path, monkeypatch) -> None:
    os.chmod(tmp_path, 0o700)
    db = tmp_path / "stage-removal.db"
    backup = tmp_path / "stage-removal.pre-v3.db"
    _seed_full_v2(db)
    source = sqlite3.connect(db)
    parent_fd = migration_module._open_private_backup_parent(backup)
    source_snapshot = migration_module._logical_parity_snapshot(source)
    original_connect = sqlite3.connect
    removed_paths: list[Path] = []

    def remove_before_open(database, *args, **kwargs):
        if kwargs.get("uri") and isinstance(database, str) and database.startswith("file:"):
            encoded_path = database[5:].split("?", 1)[0]
            stage = Path(urllib.parse.unquote(encoded_path))
            stage.unlink()
            removed_paths.append(stage)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(migration_module.sqlite3, "connect", remove_before_open)
    try:
        with pytest.raises(AdapterError) as exc_info:
            migration_module._backup_database(
                source,
                backup,
                parent_fd=parent_fd,
                source_db=db,
                source_snapshot=source_snapshot,
            )
        assert exc_info.value.code == "MIGRATION_HOLD"
        assert "existing-only" in exc_info.value.safe_message
    finally:
        os.close(parent_fd)
        source.close()

    assert removed_paths
    assert not removed_paths[0].exists()
    assert not backup.exists()
    assert not Path(str(backup) + ".factlane-v3-backup.json").exists()
    assert not list(tmp_path.glob(".factlane-*.stage*"))


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
@pytest.mark.parametrize("manifest_kind", ["regular", "symlink", "directory"])
def test_backup_manifest_collision_blocks_before_backup_copy(tmp_path: Path, manifest_kind: str) -> None:
    os.chmod(tmp_path, 0o700)
    db = tmp_path / "manifest-collision.db"
    backup = tmp_path / "manifest-collision.pre-v3.db"
    manifest = Path(str(backup) + ".factlane-v3-backup.json")
    sentinel = tmp_path / "manifest-sentinel.bin"
    _seed_full_v2(db)
    sentinel.write_bytes(b"manifest-sentinel")
    if manifest_kind == "regular":
        manifest.write_bytes(b"existing-manifest")
        os.chmod(manifest, 0o600)
    elif manifest_kind == "symlink":
        manifest.symlink_to(sentinel)
    else:
        manifest.mkdir()

    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "BACKUP_EXISTS"
    if manifest_kind == "regular":
        assert manifest.read_bytes() == b"existing-manifest"
    elif manifest_kind == "symlink":
        assert manifest.is_symlink()
        assert sentinel.read_bytes() == b"manifest-sentinel"
    else:
        assert manifest.is_dir()
    assert not backup.exists()
    assert inspect_storage_contract_version(str(db)) == 2
    assert not list(tmp_path.glob(".factlane-*.stage*"))


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_manifest_publish_collision_retains_verified_backup_and_keeps_source_v2(tmp_path: Path, monkeypatch) -> None:
    os.chmod(tmp_path, 0o700)
    db = tmp_path / "manifest-race.db"
    backup = tmp_path / "manifest-race.pre-v3.db"
    manifest = Path(str(backup) + ".factlane-v3-backup.json")
    _seed_full_v2(db)
    source_before = _digest(db)
    original_publish = migration_module._publish_private_stage_noreplace
    injected = False

    def racing_publish(parent_fd: int, stage_name: str, stage_identity: tuple[int, int], final_name: str, **kwargs) -> None:
        nonlocal injected
        if final_name == manifest.name and not injected:
            injected = True
            fd = os.open(
                final_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                os.write(fd, b"concurrent-manifest")
                os.fsync(fd)
            finally:
                os.close(fd)
        original_publish(parent_fd, stage_name, stage_identity, final_name, **kwargs)

    monkeypatch.setattr(migration_module, "_publish_private_stage_noreplace", racing_publish)
    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "BACKUP_EXISTS"
    assert injected is True
    assert source_before == _digest(db)
    assert inspect_storage_contract_version(str(db)) == 2
    assert backup.is_file()
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    assert manifest.read_bytes() == b"concurrent-manifest"
    assert not list(tmp_path.glob(".factlane-*.stage*"))


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_manifest_failure_after_backup_publication_retains_verified_backup(tmp_path: Path, monkeypatch) -> None:
    os.chmod(tmp_path, 0o700)
    db = tmp_path / "manifest-interruption.db"
    backup = tmp_path / "manifest-interruption.pre-v3.db"
    manifest = Path(str(backup) + ".factlane-v3-backup.json")
    _seed_full_v2(db)
    source_before = _digest(db)
    observed_backup_sha: list[str] = []

    def fail_manifest(backup_path: Path, **kwargs) -> None:
        assert backup_path == backup
        assert backup.is_file()
        assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
        observed_backup_sha.append(_digest(backup) or "")
        raise migration_module.MigrationHold("MIGRATION_HOLD", "injected interruption before manifest publication")

    monkeypatch.setattr(migration_module, "_write_backup_manifest", fail_manifest)
    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "MIGRATION_HOLD"
    assert observed_backup_sha and observed_backup_sha[0] == _digest(backup)
    assert source_before == _digest(db)
    assert inspect_storage_contract_version(str(db)) == 2
    assert backup.is_file()
    assert not manifest.exists()
    assert not list(tmp_path.glob(".factlane-*.stage*"))

    retained_sha = _digest(backup)
    with pytest.raises(AdapterError) as retry:
        migrate_storage_v3(str(db), str(backup))
    assert retry.value.code == "BACKUP_EXISTS"
    assert _digest(backup) == retained_sha
    assert not manifest.exists()


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_private_stage_cleanup_removes_regular_sqlite_sidecars(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    backup = tmp_path / "sidecar-cleanup.pre-v3.db"
    parent_fd = migration_module._open_private_backup_parent(backup)
    try:
        stage_name, stage_fd, stage_identity = migration_module._create_private_stage(parent_fd)
        os.close(stage_fd)
        for suffix in ("-journal", "-wal", "-shm"):
            fd = os.open(
                stage_name + suffix,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
            os.close(fd)
        migration_module._cleanup_private_stage(
            parent_fd,
            stage_name,
            stage_identity,
            include_sqlite_sidecars=True,
        )
        assert not list(tmp_path.glob(".factlane-*.stage*"))
    finally:
        os.close(parent_fd)


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_private_stage_cleanup_refuses_symlink_sidecar_without_following(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    backup = tmp_path / "sidecar-symlink.pre-v3.db"
    sentinel = tmp_path / "sidecar-sentinel.bin"
    sentinel.write_bytes(b"sidecar-sentinel")
    parent_fd = migration_module._open_private_backup_parent(backup)
    stage_name, stage_fd, stage_identity = migration_module._create_private_stage(parent_fd)
    os.close(stage_fd)
    sidecar = tmp_path / (stage_name + "-wal")
    sidecar.symlink_to(sentinel)
    try:
        with pytest.raises(AdapterError) as exc_info:
            migration_module._cleanup_private_stage(
                parent_fd,
                stage_name,
                stage_identity,
                include_sqlite_sidecars=True,
            )
        assert exc_info.value.code == "MIGRATION_HOLD"
        assert sentinel.read_bytes() == b"sidecar-sentinel"
        assert sidecar.is_symlink()
        assert (tmp_path / stage_name).is_file()
    finally:
        sidecar.unlink(missing_ok=True)
        (tmp_path / stage_name).unlink(missing_ok=True)
        os.close(parent_fd)


@pytest.mark.skipif(os.name == "nt", reason="secure migration execution is POSIX-only")
def test_missing_secure_backup_primitive_fails_closed_before_sensitive_copy(tmp_path: Path, monkeypatch) -> None:
    os.chmod(tmp_path, 0o700)
    db = tmp_path / "missing-primitive.db"
    backup = tmp_path / "missing-primitive.pre-v3.db"
    _seed_full_v2(db)
    source_before = _digest(db)
    reduced_dir_fd = set(os.supports_dir_fd)
    reduced_dir_fd.discard(os.link)
    monkeypatch.setattr(migration_module.os, "supports_dir_fd", reduced_dir_fd)

    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "MIGRATION_HOLD"
    assert "primitives are unavailable" in exc_info.value.safe_message
    assert source_before == _digest(db)
    assert inspect_storage_contract_version(str(db)) == 2
    assert not backup.exists()
    assert not Path(str(backup) + ".factlane-v3-backup.json").exists()
    assert not list(tmp_path.glob(".factlane-*.stage*"))


def test_restore_refuses_valid_v2_image_without_operator_backup_manifest(tmp_path: Path) -> None:
    db = tmp_path / "restore-target.db"
    unverified = tmp_path / "unverified-v2.db"
    _seed_full_v2(db)
    shutil.copy2(db, unverified)
    before = _digest(db)
    with pytest.raises(AdapterError) as exc_info:
        restore_pre_v3_backup(str(db), str(unverified))
    assert exc_info.value.code == "BACKUP_INVALID"
    assert _digest(db) == before


def test_migration_refuses_materialized_v2_without_profile_binding_before_backup(tmp_path: Path) -> None:
    db = tmp_path / "missing-profile.db"
    backup = tmp_path / "missing-profile.pre-v3.db"
    _seed_full_v2(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM adapter_meta WHERE key='embedding_profile'")
        conn.commit()
    finally:
        conn.close()
    if os.name == "nt":
        _assert_windows_maintenance_hold_no_mutation(db, backup)
        return
    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "PROFILE_MISMATCH"
    assert not backup.exists()
    assert inspect_storage_contract_version(str(db)) == 2


def test_migration_refuses_incomplete_pinned_native_schema_before_backup(tmp_path: Path) -> None:
    db = tmp_path / "missing-native.db"
    backup = tmp_path / "missing-native.pre-v3.db"
    _seed_full_v2(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TABLE memory_embeddings_rowids")
        conn.commit()
    finally:
        conn.close()
    if os.name == "nt":
        _assert_windows_maintenance_hold_no_mutation(db, backup)
        return
    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "SCHEMA_MISMATCH"
    assert not backup.exists()
    assert inspect_storage_contract_version(str(db)) == 2


def test_migration_refuses_unknown_v2_table_before_backup(tmp_path: Path) -> None:
    db = tmp_path / "unknown-table.db"
    backup = tmp_path / "unknown-table.pre-v3.db"
    _seed_full_v2(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE unexpected_extension(id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    if os.name == "nt":
        _assert_windows_maintenance_hold_no_mutation(db, backup)
        return
    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "SCHEMA_MISMATCH"
    assert not backup.exists()
    assert inspect_storage_contract_version(str(db)) == 2


def test_migration_schema_failure_rolls_back_transaction_and_keeps_verified_backup(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "crash-atomic.db"
    backup = tmp_path / "crash-atomic.pre-v3.db"
    _seed_full_v2(db)
    original = migration_module._create_v3_governance_schema

    def fail_after_schema(conn: sqlite3.Connection) -> None:
        original(conn)
        raise RuntimeError("injected-after-v3-schema")

    if os.name == "nt":
        _assert_windows_maintenance_hold_no_mutation(db, backup)
        return
    monkeypatch.setattr(migration_module, "_create_v3_governance_schema", fail_after_schema)
    with pytest.raises(RuntimeError, match="injected-after-v3-schema"):
        migrate_storage_v3(str(db), str(backup))
    assert inspect_storage_contract_version(str(db)) == 2
    assert backup.is_file()
    assert Path(str(backup) + ".factlane-v3-backup.json").is_file()
    conn = sqlite3.connect(db)
    try:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "lifecycle_events" not in tables
        assert "lifecycle_operations" not in tables
        assert "uq_adapter_memory_revision" not in indexes
        assert "uq_adapter_parent_record" not in indexes
        assert conn.execute(
            "SELECT fact FROM adapter_records WHERE record_id='11111111-1111-4111-8111-111111111111'"
        ).fetchone()[0] == "The legacy fact remains unchanged."
    finally:
        conn.close()


def test_migration_holds_on_ambiguous_legacy_lineage_without_version_change(tmp_path: Path) -> None:
    db = tmp_path / "ambiguous.db"
    backup = tmp_path / "ambiguous.pre-v3.db"
    _seed_full_v2(db, duplicate_revision=True)
    before = _digest(db)
    with pytest.raises(AdapterError) as exc_info:
        migrate_storage_v3(str(db), str(backup))
    assert exc_info.value.code == "MIGRATION_HOLD"
    assert not backup.exists()
    assert _digest(db) == before
    assert inspect_storage_contract_version(str(db)) == 2
