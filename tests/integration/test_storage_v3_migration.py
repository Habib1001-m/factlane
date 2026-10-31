from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
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
