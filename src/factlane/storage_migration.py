from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .contract import AdapterError, canonical_json, iso_now, parse_iso
from .embedding_compatibility import parse_embedding_profile_metadata
from .recovery import (
    MaintenanceLease,
    RecoveryHold,
    SensitiveMemoryRecoveryOperator,
    _KNOWN_RECOVERY_TABLES,
)
from .storage import (
    STORAGE_CONTRACT_VERSION,
    _CASEFOLD_FUNCTION,
    _LEGACY_WRITER_FUNCTION,
    _RECOVERY_WRITER_FUNCTION,
    _WRITER_FUNCTION,
    _expected_v3_table_sql,
    _normalize_schema_sql,
    inspect_storage_contract_version,
    register_storage_legacy_maintenance_writer,
)


class MigrationHold(AdapterError):
    """Fail-closed migration/restore outcome with no silent compatibility fallback."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _integrity(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    if row is None or str(row[0]).lower() != "ok":
        raise MigrationHold("MIGRATION_HOLD", "database integrity check failed")


def _require_v2_schema(conn: sqlite3.Connection) -> None:
    required = {
        "adapter_meta",
        "adapter_records",
        "memories",
        "memory_embeddings",
        "memory_embeddings_chunks",
        "memory_embeddings_info",
        "memory_embeddings_rowids",
        "memory_embeddings_vector_chunks00",
        "memory_content_fts",
        "memory_content_fts_config",
        "memory_content_fts_data",
        "memory_content_fts_docsize",
        "memory_content_fts_idx",
        "metadata",
        "migration_registry",
        "beliefs",
        "memory_graph",
    }
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    expected_v2_tables = set(_KNOWN_RECOVERY_TABLES) - {"lifecycle_events", "lifecycle_operations"}
    if present != expected_v2_tables:
        raise MigrationHold("SCHEMA_MISMATCH", "storage v2 pinned table allowlist is not exact")
    row = conn.execute("SELECT value FROM adapter_meta WHERE key='contract_version'").fetchone()
    if row is None or str(row[0]) != "2":
        raise MigrationHold("SCHEMA_MISMATCH", "migration input is not storage contract v2")
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(adapter_records)").fetchall()}
    required_columns = {
        "record_id", "memory_id", "revision", "parent_record_id", "scope", "project_id", "worktree_id",
        "workflow_id", "agent_id", "memory_type", "fact", "source_provenance", "source_timestamp",
        "created_at", "last_verified_at", "verified_by", "authority_role", "contribution_origin",
        "freshness_policy", "supersedes", "contradiction_key", "contradiction_state", "confidence", "tags",
        "lifecycle_state", "native_content_hash", "payload_fingerprint", "idempotency_key", "embedding_profile_id",
        "embedding_model_digest", "embedding_output_dimension",
    }
    if not required_columns.issubset(columns):
        raise MigrationHold("SCHEMA_MISMATCH", "storage v2 adapter_records schema is incomplete")
    native_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    required_native_columns = {
        "id",
        "content_hash",
        "content",
        "metadata",
        "created_at",
        "updated_at",
        "deleted_at",
        "store",
    }
    if not required_native_columns.issubset(native_columns):
        raise MigrationHold("SCHEMA_MISMATCH", "storage v2 pinned native memories schema is incomplete")
    adapter_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='adapter_records'"
    ).fetchone()
    if (
        adapter_sql is None
        or _normalize_schema_sql(adapter_sql[0])
        != _normalize_schema_sql(_expected_v3_table_sql()["adapter_records"])
    ):
        raise MigrationHold("SCHEMA_MISMATCH", "storage v2 adapter_records definition is not exact")


def _require_v2_native_profile_parity(conn: sqlite3.Connection) -> None:
    adapter_count = int(conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0])
    profile_row = conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone()
    if adapter_count and profile_row is None:
        raise MigrationHold("PROFILE_MISMATCH", "storage v2 materialized adapter state has no embedding profile binding")
    if profile_row is None:
        return
    try:
        decoded_profile = json.loads(str(profile_row[0]))
    except json.JSONDecodeError as exc:
        raise MigrationHold("PROFILE_MISMATCH", "storage v2 embedding profile metadata is invalid") from exc
    profile = parse_embedding_profile_metadata(decoded_profile)
    if profile is None:
        raise MigrationHold("PROFILE_MISMATCH", "storage v2 embedding profile metadata is invalid")
    store_key = str(profile["store_key"])
    identity = profile["semantic_identity"]
    runtime = profile["runtime_provenance"]
    output_dimension = identity.get("output_dimension") if isinstance(identity, dict) else None
    model_digest = runtime.get("model_digest") if isinstance(runtime, dict) else None
    if not isinstance(output_dimension, int) or output_dimension <= 0 or not isinstance(model_digest, str):
        raise MigrationHold("PROFILE_MISMATCH", "storage v2 embedding profile binding is incomplete")
    bindings = {
        (str(row[0]), str(row[1]), int(row[2]))
        for row in conn.execute(
            "SELECT DISTINCT embedding_profile_id,embedding_model_digest,embedding_output_dimension FROM adapter_records"
        ).fetchall()
    }
    if bindings and bindings != {(store_key, model_digest, output_dimension)}:
        raise MigrationHold("PROFILE_MISMATCH", "storage v2 adapter rows do not match the bound embedding profile")
    vector_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_embeddings'"
    ).fetchone()
    vector_sql = _normalize_schema_sql(vector_sql_row[0] if vector_sql_row else None)
    if (
        f"content_embeddingfloat[{output_dimension}]" not in vector_sql
        or "distance_metric=cosine" not in vector_sql
        or "storetextpartitionkey" not in vector_sql
    ):
        raise MigrationHold("PROFILE_MISMATCH", "storage v2 vector table does not match the bound embedding profile")

    native_rows = {
        str(row[1]): (int(row[0]), str(row[2] or ""), row[3], str(row[4] or ""))
        for row in conn.execute("SELECT id,content_hash,metadata,deleted_at,store FROM memories").fetchall()
    }
    vector_rowids = {
        int(row[0]) for row in conn.execute("SELECT rowid FROM memory_embeddings_rowids").fetchall()
    }
    materialized_states = {"VALIDATED_CURRENT", "CANDIDATE", "STALE"}
    for record_id, lifecycle_state, native_hash in conn.execute(
        "SELECT record_id,lifecycle_state,native_content_hash FROM adapter_records"
    ).fetchall():
        if str(lifecycle_state) not in materialized_states:
            continue
        native = native_rows.get(str(native_hash))
        if native is None or native[2] is not None or native[3] != store_key:
            raise MigrationHold("MIGRATION_HOLD", "storage v2 active adapter row is not materialized in the bound native store")
        try:
            metadata = json.loads(native[1]) if native[1] else {}
        except json.JSONDecodeError as exc:
            raise MigrationHold("MIGRATION_HOLD", "storage v2 native metadata is invalid") from exc
        if metadata.get("adapter_record_id") != str(record_id) or native[0] not in vector_rowids:
            raise MigrationHold("MIGRATION_HOLD", "storage v2 native/vector identity does not match adapter state")


def _logical_parity_snapshot(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    table_rows = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    excluded = {"lifecycle_events", "lifecycle_operations"}
    for raw_name, raw_sql in table_rows:
        name = str(raw_name)
        sql = str(raw_sql or "")
        if name in excluded or _normalize_schema_sql(sql).startswith("createvirtualtable"):
            continue
        columns = [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{name}")').fetchall()]
        if not columns:
            continue
        if name == "adapter_meta":
            rows = conn.execute(
                "SELECT key,value FROM adapter_meta WHERE key NOT IN ('contract_version','trust_clock_high_water_at') ORDER BY key"
            ).fetchall()
        else:
            quoted = ",".join(f'"{column}"' for column in columns)
            rows = conn.execute(f'SELECT {quoted} FROM "{name}"').fetchall()
        normalized = sorted(repr(tuple(row)) for row in rows)
        digest = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()
        snapshot[name] = {"rows": len(rows), "sha256": digest}
    return snapshot


def _require_migration_capacity_and_permissions(db: Path, backup: Path) -> None:
    if not db.is_file() or not os.access(db, os.R_OK | os.W_OK):
        raise MigrationHold("MIGRATION_HOLD", "storage v2 database is not readable and writable by the migration operator")
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not os.access(db.parent, os.W_OK) or not os.access(backup.parent, os.W_OK):
        raise MigrationHold("MIGRATION_HOLD", "migration and backup directories must be writable by the operator")
    source_bytes = db.stat().st_size + sum(
        sidecar.stat().st_size for sidecar in (Path(str(db) + "-wal"), Path(str(db) + "-shm")) if sidecar.exists()
    )
    required_free = max(source_bytes * 2, 16 * 1024 * 1024)
    if shutil.disk_usage(db.parent).free < required_free or shutil.disk_usage(backup.parent).free < source_bytes:
        raise MigrationHold("MIGRATION_HOLD", "insufficient free capacity for verified migration and backup")


def _backup_manifest_path(backup_path: Path) -> Path:
    return Path(str(backup_path) + ".factlane-v3-backup.json")


def _write_backup_manifest(backup_path: Path, *, source_db: Path, backup_sha256: str, snapshot: dict[str, Any]) -> None:
    manifest_path = _backup_manifest_path(backup_path)
    if manifest_path.exists():
        raise MigrationHold("BACKUP_EXISTS", "backup verification manifest already exists")
    payload = {
        "schema_version": 1,
        "kind": "FACTLANE_PRE_V3_BACKUP",
        "source_path_sha256": hashlib.sha256(str(source_db).encode("utf-8")).hexdigest(),
        "backup_sha256": backup_sha256,
        "logical_snapshot_sha256": hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
        "created_at": iso_now(),
    }
    tmp = manifest_path.with_name(manifest_path.name + f".{uuid.uuid4()}.tmp")
    tmp.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, manifest_path)


def _verify_backup_manifest(backup_path: Path, *, source_db: Path, snapshot: dict[str, Any]) -> str:
    manifest_path = _backup_manifest_path(backup_path)
    if not manifest_path.is_file():
        raise MigrationHold("BACKUP_INVALID", "rollback requires the operator-owned pre-v3 backup manifest")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationHold("BACKUP_INVALID", "pre-v3 backup manifest is invalid") from exc
    backup_sha256 = _sha256(backup_path)
    expected = {
        "schema_version": 1,
        "kind": "FACTLANE_PRE_V3_BACKUP",
        "source_path_sha256": hashlib.sha256(str(source_db).encode("utf-8")).hexdigest(),
        "backup_sha256": backup_sha256,
        "logical_snapshot_sha256": hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise MigrationHold("BACKUP_INVALID", "pre-v3 backup manifest does not bind the requested restore")
    return backup_sha256


def _require_lineage_compatibility(conn: sqlite3.Connection) -> None:
    duplicate_revision = conn.execute(
        "SELECT memory_id,revision FROM adapter_records GROUP BY memory_id,revision HAVING COUNT(*)>1 LIMIT 1"
    ).fetchone()
    if duplicate_revision is not None:
        raise MigrationHold("MIGRATION_HOLD", "legacy lineage has duplicate memory_id/revision identity")
    duplicate_parent = conn.execute(
        "SELECT parent_record_id FROM adapter_records WHERE parent_record_id IS NOT NULL "
        "GROUP BY parent_record_id HAVING COUNT(*)>1 LIMIT 1"
    ).fetchone()
    if duplicate_parent is not None:
        raise MigrationHold("MIGRATION_HOLD", "legacy lineage has more than one successor for a parent record")


def _create_v3_governance_schema(conn: sqlite3.Connection) -> None:
    # Migration is the privileged point where legacy v2 index definitions are
    # reconciled to the exact Storage v3 identity. Ordinary runtime never
    # repairs same-name drift; it fails closed in the pre-native preflight.
    for name in (
        "idx_adapter_scope_current",
        "idx_adapter_memory_id",
        "idx_adapter_contradiction",
        "uq_adapter_memory_revision",
        "uq_adapter_parent_record",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute(
        "CREATE INDEX idx_adapter_scope_current "
        "ON adapter_records(scope,project_id,worktree_id,workflow_id,agent_id,lifecycle_state)"
    )
    conn.execute("CREATE INDEX idx_adapter_memory_id ON adapter_records(memory_id,revision)")
    conn.execute("CREATE INDEX idx_adapter_contradiction ON adapter_records(contradiction_key,lifecycle_state)")
    conn.execute("CREATE UNIQUE INDEX uq_adapter_memory_revision ON adapter_records(memory_id,revision)")
    conn.execute(
        "CREATE UNIQUE INDEX uq_adapter_parent_record ON adapter_records(parent_record_id) "
        "WHERE parent_record_id IS NOT NULL"
    )
    conn.execute(
        """
        CREATE TABLE lifecycle_events (
            event_id TEXT PRIMARY KEY,
            operation_id TEXT,
            memory_id TEXT NOT NULL,
            record_id TEXT NOT NULL,
            target_record_id TEXT,
            target_revision INTEGER,
            action TEXT NOT NULL,
            prior_lifecycle_state TEXT,
            result_lifecycle_state TEXT,
            prior_contradiction_state TEXT,
            result_contradiction_state TEXT,
            reason_code TEXT,
            actor_class TEXT NOT NULL,
            actor_ref_digest TEXT,
            grant_ref_digest TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            expected_revision INTEGER,
            result_revision INTEGER,
            related_record_id TEXT,
            related_memory_id TEXT,
            freshness_outcome TEXT,
            observation_origin TEXT,
            observed_at TEXT,
            accepted_at TEXT,
            evidence_ref_digest TEXT,
            freshness_generation INTEGER,
            clock_anomaly TEXT NOT NULL DEFAULT 'NONE',
            CHECK (clock_anomaly IN ('NONE','CLOCK_UNTRUSTED_TIME')),
            CHECK (freshness_generation IS NULL OR freshness_generation > 0)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE lifecycle_operations (
            idempotency_key TEXT PRIMARY KEY,
            request_fingerprint TEXT,
            operation_id TEXT NOT NULL UNIQUE,
            request_id TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            actor_class TEXT NOT NULL,
            actor_ref_digest TEXT,
            grant_ref_digest TEXT NOT NULL,
            scope_digest TEXT NOT NULL,
            target_memory_id TEXT,
            target_record_id TEXT,
            winner_memory_id TEXT,
            winner_record_id TEXT,
            winner_parent_record_id TEXT,
            result_record_id TEXT,
            result_revision INTEGER,
            result_lifecycle_state TEXT,
            result_contradiction_state TEXT,
            result_outcome TEXT,
            observed_at TEXT,
            affected_members_json TEXT,
            event_ids_json TEXT,
            result_group_token TEXT,
            result_group_count INTEGER,
            replay_status TEXT NOT NULL,
            commit_timestamp TEXT NOT NULL,
            decision_error TEXT,
            decision_at TEXT,
            accepted_event_freshness_generation INTEGER,
            resulting_member_freshness_generation INTEGER,
            recovery_event_id TEXT
        )
        """
    )
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
    conn.execute(
        "CREATE INDEX idx_lifecycle_operation_target ON lifecycle_operations(target_memory_id,target_record_id)"
    )
    for name in (
        "factlane_v2_adapter_insert_fence",
        "factlane_v2_adapter_update_fence",
        "factlane_v2_adapter_delete_fence",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name, action in (
        ("factlane_v3_adapter_insert_fence", "INSERT"),
        ("factlane_v3_adapter_update_fence", "UPDATE"),
        ("factlane_v3_adapter_delete_fence", "DELETE"),
    ):
        conn.execute(
            f"CREATE TRIGGER {name} BEFORE {action} ON adapter_records "
            f"WHEN {_WRITER_FUNCTION}() != 1 BEGIN "
            "SELECT RAISE(ABORT, 'FACTLANE_STORAGE_V3_WRITER_REQUIRED'); END"
        )
    for table in ("lifecycle_events", "lifecycle_operations"):
        for action in ("INSERT", "UPDATE", "DELETE"):
            name = f"factlane_v3_{table}_{action.lower()}_fence"
            if action == "INSERT":
                predicate = f"{_WRITER_FUNCTION}() != 1"
                error = "FACTLANE_STORAGE_V3_WRITER_REQUIRED"
            elif table == "lifecycle_operations" and action == "UPDATE":
                predicate = f"{_RECOVERY_WRITER_FUNCTION}() != 1"
                error = "FACTLANE_RECOVERY_V3_WRITER_REQUIRED"
            else:
                predicate = "1 = 1"
                error = "FACTLANE_V3_AUDIT_IMMUTABLE"
            conn.execute(
                f"CREATE TRIGGER {name} BEFORE {action} ON {table} "
                f"WHEN {predicate} BEGIN SELECT RAISE(ABORT, '{error}'); END"
            )
    conn.execute(
        "CREATE TRIGGER factlane_v3_adapter_global_idempotency BEFORE INSERT ON adapter_records "
        "WHEN EXISTS (SELECT 1 FROM lifecycle_operations WHERE idempotency_key=NEW.idempotency_key) "
        "BEGIN SELECT RAISE(ABORT,'FACTLANE_GLOBAL_IDEMPOTENCY_CONFLICT'); END"
    )
    conn.execute(
        "CREATE TRIGGER factlane_v3_operation_global_idempotency BEFORE INSERT ON lifecycle_operations "
        "WHEN EXISTS (SELECT 1 FROM adapter_records WHERE idempotency_key=NEW.idempotency_key) "
        "BEGIN SELECT RAISE(ABORT,'FACTLANE_GLOBAL_IDEMPOTENCY_CONFLICT'); END"
    )
    highwater_text = "1970-01-01T00:00:00Z"
    highwater_dt = parse_iso(highwater_text, required=True)
    for (created_at,) in conn.execute("SELECT created_at FROM adapter_records"):
        candidate_text = str(created_at)
        candidate_dt = parse_iso(candidate_text, required=True)
        if candidate_dt > highwater_dt:
            highwater_text = candidate_text
            highwater_dt = candidate_dt
    conn.execute(
        "INSERT OR REPLACE INTO adapter_meta(key,value) VALUES ('trust_clock_high_water_at',?)",
        (highwater_text,),
    )


def _backup_database(conn: sqlite3.Connection, backup_path: Path, *, source_db: Path, source_snapshot: dict[str, Any]) -> str:
    if backup_path.exists():
        raise MigrationHold("BACKUP_EXISTS", "backup destination already exists")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup = sqlite3.connect(backup_path)
    try:
        conn.backup(backup)
        backup.commit()
        _integrity(backup)
        _require_v2_schema(backup)
        _require_lineage_compatibility(backup)
        _require_v2_native_profile_parity(backup)
        if _logical_parity_snapshot(backup) != source_snapshot:
            raise MigrationHold("BACKUP_INVALID", "pre-v3 backup does not preserve the source logical state")
    finally:
        backup.close()
    os.chmod(backup_path, 0o600)
    backup_sha256 = _sha256(backup_path)
    _write_backup_manifest(
        backup_path,
        source_db=source_db,
        backup_sha256=backup_sha256,
        snapshot=source_snapshot,
    )
    return backup_sha256


def migrate_storage_v3(db_path: str, backup_path: str) -> dict[str, Any]:
    db = Path(db_path).resolve()
    backup = Path(backup_path).resolve()
    _require_migration_capacity_and_permissions(db, backup)
    if inspect_storage_contract_version(str(db)) != 2:
        raise MigrationHold("SCHEMA_MISMATCH", "migrate-storage-v3 requires an exact storage-v2 database")
    operation_id = str(uuid.uuid4())
    try:
        lease_context = MaintenanceLease(str(db), f"migration:{operation_id}")
        with lease_context:
            conn = _connect(db)
            try:
                register_storage_legacy_maintenance_writer(conn)
                _integrity(conn)
                _require_v2_schema(conn)
                _require_lineage_compatibility(conn)
                _require_v2_native_profile_parity(conn)
                source_snapshot = _logical_parity_snapshot(conn)
                backup_sha256 = _backup_database(
                    conn,
                    backup,
                    source_db=db,
                    source_snapshot=source_snapshot,
                )
                conn.execute("BEGIN IMMEDIATE")
                try:
                    _create_v3_governance_schema(conn)
                    if _logical_parity_snapshot(conn) != source_snapshot:
                        raise MigrationHold("MIGRATION_HOLD", "migration changed historical native/vector/adapter rows")
                    # Contract version moves last, after every schema/index/fence succeeds.
                    conn.execute(
                        "UPDATE adapter_meta SET value=? WHERE key='contract_version'",
                        (str(STORAGE_CONTRACT_VERSION),),
                    )
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                _integrity(conn)
                if _logical_parity_snapshot(conn) != source_snapshot:
                    raise MigrationHold("MIGRATION_HOLD", "post-migration native/vector/adapter parity check failed")
                try:
                    SensitiveMemoryRecoveryOperator._require_known_recovery_schema(conn)
                except RecoveryHold as exc:
                    raise MigrationHold("MIGRATION_HOLD", "post-migration Recovery schema preflight failed") from exc
            finally:
                conn.close()
    except RecoveryHold as exc:
        raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc
    if inspect_storage_contract_version(str(db)) != STORAGE_CONTRACT_VERSION:
        raise MigrationHold("MIGRATION_HOLD", "post-migration storage-v3 schema verification failed")
    return {
        "status": "OK",
        "operation": "MIGRATE_STORAGE_V3",
        "operation_id": operation_id,
        "database": str(db),
        "from_contract": 2,
        "to_contract": STORAGE_CONTRACT_VERSION,
        "backup": str(backup),
        "backup_sha256": backup_sha256,
        "completed_at": iso_now(),
    }


def restore_pre_v3_backup(db_path: str, backup_path: str) -> dict[str, Any]:
    db = Path(db_path).resolve()
    backup = Path(backup_path).resolve()
    if not backup.is_file() or inspect_storage_contract_version(str(backup)) != 2:
        raise MigrationHold("BACKUP_INVALID", "rollback requires an exact verified storage-v2 backup")
    backup_conn = _connect(backup)
    try:
        _integrity(backup_conn)
        _require_v2_schema(backup_conn)
        _require_lineage_compatibility(backup_conn)
        _require_v2_native_profile_parity(backup_conn)
        backup_snapshot = _logical_parity_snapshot(backup_conn)
    finally:
        backup_conn.close()
    backup_sha256 = _verify_backup_manifest(backup, source_db=db, snapshot=backup_snapshot)
    operation_id = str(uuid.uuid4())
    try:
        with MaintenanceLease(str(db), f"restore:{operation_id}") as lease:
            with tempfile.NamedTemporaryFile(
                prefix=db.name + ".restore-", dir=db.parent, delete=False
            ) as tmp:
                stage = Path(tmp.name)
            try:
                shutil.copy2(backup, stage)
                with stage.open("rb") as handle:
                    os.fsync(handle.fileno())
                lease.promote_sanitized_image(stage)
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(db) + suffix)
                    if sidecar.exists():
                        sidecar.unlink()
            finally:
                if stage.exists():
                    stage.unlink()
    except RecoveryHold as exc:
        raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc
    if inspect_storage_contract_version(str(db)) != 2:
        raise MigrationHold("ROLLBACK_HOLD", "restored database did not verify as storage v2")
    restored_conn = _connect(db)
    try:
        _integrity(restored_conn)
        _require_v2_schema(restored_conn)
        _require_lineage_compatibility(restored_conn)
        _require_v2_native_profile_parity(restored_conn)
        if _logical_parity_snapshot(restored_conn) != backup_snapshot:
            raise MigrationHold("ROLLBACK_HOLD", "restored database does not match the verified pre-v3 backup")
    finally:
        restored_conn.close()
    return {
        "status": "OK",
        "operation": "RESTORE_PRE_V3_BACKUP",
        "operation_id": operation_id,
        "database": str(db),
        "restored_contract": 2,
        "backup": str(backup),
        "backup_sha256": backup_sha256,
        "completed_at": iso_now(),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="FactLane trusted storage-v3 migration/restore operator")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--db", required=True)
    migrate.add_argument("--backup", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--db", required=True)
    restore.add_argument("--backup", required=True)
    args = parser.parse_args(argv)
    try:
        result = (
            migrate_storage_v3(args.db, args.backup)
            if args.command == "migrate"
            else restore_pre_v3_backup(args.db, args.backup)
        )
    except AdapterError as exc:
        print(canonical_json(exc.envelope()))
        raise SystemExit(2) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
