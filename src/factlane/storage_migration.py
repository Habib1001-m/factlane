from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import urllib.parse
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
    _validate_existing_v3_schema,
    inspect_storage_contract_version,
    register_storage_legacy_maintenance_writer,
)

_WINDOWS_MIGRATION_BINDING_KEY = "windows_v2_v3_migration_binding"
_WINDOWS_MANIFEST_SCHEMA_VERSION = 2
_WINDOWS_ROLLBACK_INTENT_SCHEMA_VERSION = 1


def _windows_migration_checkpoint(_name: str) -> None:
    """Private fault-injection seam for native migration crash acceptance."""


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


def _quick_integrity(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA quick_check").fetchone()
    if row is None or str(row[0]).lower() != "ok":
        raise MigrationHold("MIGRATION_HOLD", "database quick check failed")
    _integrity(conn)


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


def _logical_parity_snapshot(
    conn: sqlite3.Connection,
    *,
    excluded_meta_keys: tuple[str, ...] = (),
) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    table_rows = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    excluded_tables = {"lifecycle_events", "lifecycle_operations"}
    for raw_name, raw_sql in table_rows:
        name = str(raw_name)
        sql = str(raw_sql or "")
        if name in excluded_tables or _normalize_schema_sql(sql).startswith("createvirtualtable"):
            continue
        columns = [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{name}")').fetchall()]
        if not columns:
            continue
        if name == "adapter_meta":
            excluded_meta = ("contract_version", "trust_clock_high_water_at", *excluded_meta_keys)
            placeholders = ",".join("?" for _ in excluded_meta)
            rows = conn.execute(
                f"SELECT key,value FROM adapter_meta WHERE key NOT IN ({placeholders}) ORDER BY key",
                excluded_meta,
            ).fetchall()
        else:
            quoted = ",".join(f'"{column}"' for column in columns)
            rows = conn.execute(f'SELECT {quoted} FROM "{name}"').fetchall()
        normalized = sorted(repr(tuple(row)) for row in rows)
        digest = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()
        snapshot[name] = {"rows": len(rows), "sha256": digest}
    return snapshot


def _requested_windows_path(path: str, *, label: str) -> Path:
    requested = Path(os.path.abspath(path))
    if not requested.name or requested.name in {".", ".."}:
        raise MigrationHold("MIGRATION_HOLD", f"{label} must name one file")
    return requested


def _trust_clock_high_water(conn: sqlite3.Connection) -> str:
    highwater_text = "1970-01-01T00:00:00Z"
    highwater_dt = parse_iso(highwater_text, required=True)
    for (created_at,) in conn.execute("SELECT created_at FROM adapter_records"):
        candidate_text = str(created_at)
        candidate_dt = parse_iso(candidate_text, required=True)
        if candidate_dt > highwater_dt:
            highwater_text = candidate_text
            highwater_dt = candidate_dt
    return highwater_text


def _canonical_backup_target(backup_path: str) -> Path:
    requested = Path(os.path.abspath(backup_path))
    if not requested.name or requested.name in {".", ".."}:
        raise MigrationHold("MIGRATION_HOLD", "backup destination must name one file")
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError as exc:
        raise MigrationHold(
            "MIGRATION_HOLD",
            "backup parent directory must already exist",
        ) from exc
    return parent / requested.name


def _require_secure_backup_primitives() -> None:
    required_flags = ("O_NOFOLLOW", "O_DIRECTORY")
    if os.name != "posix" or not hasattr(os, "geteuid") or any(
        not hasattr(os, flag) for flag in required_flags
    ):
        raise MigrationHold(
            "MIGRATION_HOLD",
            "secure pre-v3 backup publication requires POSIX no-follow directory primitives",
        )
    if (
        os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.unlink not in os.supports_dir_fd
        or os.link not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or os.link not in os.supports_follow_symlinks
    ):
        raise MigrationHold(
            "MIGRATION_HOLD",
            "secure pre-v3 backup publication primitives are unavailable",
        )


def _open_private_backup_parent(backup: Path) -> int:
    _require_secure_backup_primitives()
    euid = os.geteuid()
    parent = backup.parent
    current = parent
    child_stat: os.stat_result | None = None
    while True:
        try:
            current_stat = os.stat(current, follow_symlinks=False)
        except OSError as exc:
            raise MigrationHold(
                "MIGRATION_HOLD",
                "backup path custody could not be verified",
            ) from exc
        if not stat.S_ISDIR(current_stat.st_mode):
            raise MigrationHold("MIGRATION_HOLD", "backup path contains a non-directory ancestor")
        if current == parent:
            if current_stat.st_uid != euid or stat.S_IMODE(current_stat.st_mode) & 0o077:
                raise MigrationHold(
                    "MIGRATION_HOLD",
                    "backup parent must be operator-owned and private",
                )
        else:
            if current_stat.st_uid not in {0, euid}:
                raise MigrationHold(
                    "MIGRATION_HOLD",
                    "backup path ancestor is owned by an untrusted OS identity",
                )
            if stat.S_IMODE(current_stat.st_mode) & 0o022:
                sticky_protects_child = bool(current_stat.st_mode & stat.S_ISVTX) and child_stat is not None and (
                    child_stat.st_uid in {0, euid}
                )
                if not sticky_protects_child:
                    raise MigrationHold(
                        "MIGRATION_HOLD",
                        "backup path ancestor permits replacement by another OS identity",
                    )
        if current.parent == current:
            break
        child_stat = current_stat
        current = current.parent

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        parent_fd = os.open(parent, flags)
    except OSError as exc:
        raise MigrationHold("MIGRATION_HOLD", "backup parent could not be opened securely") from exc
    try:
        path_stat = os.stat(parent, follow_symlinks=False)
        handle_stat = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(handle_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino) != (handle_stat.st_dev, handle_stat.st_ino)
            or handle_stat.st_uid != euid
            or stat.S_IMODE(handle_stat.st_mode) & 0o077
        ):
            raise MigrationHold("MIGRATION_HOLD", "backup parent identity or custody changed")
        return parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


def _entry_stat(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _require_entry_absent(parent_fd: int, name: str, *, label: str) -> None:
    if _entry_stat(parent_fd, name) is not None:
        raise MigrationHold("BACKUP_EXISTS", f"{label} already exists")


def _create_private_stage(parent_fd: int) -> tuple[str, int, tuple[int, int]]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _ in range(8):
        name = f".factlane-{uuid.uuid4().hex}.stage"
        try:
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise MigrationHold("MIGRATION_HOLD", "private backup stage could not be created") from exc
        try:
            os.fchmod(fd, 0o600)
            handle_stat = os.fstat(fd)
            if (
                not stat.S_ISREG(handle_stat.st_mode)
                or handle_stat.st_uid != os.geteuid()
                or stat.S_IMODE(handle_stat.st_mode) != 0o600
            ):
                raise MigrationHold("MIGRATION_HOLD", "private backup stage custody is invalid")
            return name, fd, (int(handle_stat.st_dev), int(handle_stat.st_ino))
        except BaseException:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    raise MigrationHold("MIGRATION_HOLD", "private backup stage name could not be reserved")


def _require_stage_identity(parent_fd: int, name: str, identity: tuple[int, int]) -> None:
    entry = _entry_stat(parent_fd, name)
    if (
        entry is None
        or not stat.S_ISREG(entry.st_mode)
        or (int(entry.st_dev), int(entry.st_ino)) != identity
        or entry.st_uid != os.geteuid()
        or stat.S_IMODE(entry.st_mode) != 0o600
    ):
        raise MigrationHold("MIGRATION_HOLD", "private backup stage identity changed")


def _cleanup_private_stage(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
    *,
    include_sqlite_sidecars: bool,
) -> None:
    for suffix in (("-journal", "-wal", "-shm") if include_sqlite_sidecars else ()):
        sidecar_name = name + suffix
        entry = _entry_stat(parent_fd, sidecar_name)
        if entry is None:
            continue
        if not stat.S_ISREG(entry.st_mode):
            raise MigrationHold("MIGRATION_HOLD", "private SQLite backup sidecar has unexpected type")
        os.unlink(sidecar_name, dir_fd=parent_fd)
    entry = _entry_stat(parent_fd, name)
    if entry is None:
        return
    if (int(entry.st_dev), int(entry.st_ino)) != identity:
        raise MigrationHold("MIGRATION_HOLD", "private backup stage no longer names the reserved inode")
    os.unlink(name, dir_fd=parent_fd)


def _publish_private_stage_noreplace(
    parent_fd: int,
    stage_name: str,
    stage_identity: tuple[int, int],
    final_name: str,
    *,
    label: str,
    include_sqlite_sidecars: bool,
) -> None:
    _require_stage_identity(parent_fd, stage_name, stage_identity)
    try:
        os.link(
            stage_name,
            final_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise MigrationHold("BACKUP_EXISTS", f"{label} already exists") from exc
    except (NotImplementedError, OSError) as exc:
        raise MigrationHold(
            "MIGRATION_HOLD",
            f"{label} could not be published with atomic no-replace semantics",
        ) from exc

    final_stat = _entry_stat(parent_fd, final_name)
    if (
        final_stat is None
        or not stat.S_ISREG(final_stat.st_mode)
        or (int(final_stat.st_dev), int(final_stat.st_ino)) != stage_identity
        or final_stat.st_uid != os.geteuid()
        or stat.S_IMODE(final_stat.st_mode) != 0o600
    ):
        raise MigrationHold("MIGRATION_HOLD", f"{label} publication identity is invalid")
    os.fsync(parent_fd)
    _cleanup_private_stage(
        parent_fd,
        stage_name,
        stage_identity,
        include_sqlite_sidecars=include_sqlite_sidecars,
    )
    os.fsync(parent_fd)


def _require_migration_capacity_and_permissions(db: Path, backup: Path, parent_fd: int) -> None:
    if not db.is_file() or not os.access(db, os.R_OK | os.W_OK):
        raise MigrationHold("MIGRATION_HOLD", "storage v2 database is not readable and writable by the migration operator")
    parent_stat = os.fstat(parent_fd)
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or stat.S_IMODE(parent_stat.st_mode) & 0o077
    ):
        raise MigrationHold("MIGRATION_HOLD", "backup parent custody is not private")
    if not os.access(db.parent, os.W_OK) or not os.access(backup.parent, os.R_OK | os.W_OK | os.X_OK):
        raise MigrationHold("MIGRATION_HOLD", "migration and backup directories must be writable by the operator")
    source_bytes = db.stat().st_size + sum(
        sidecar.stat().st_size for sidecar in (Path(str(db) + "-wal"), Path(str(db) + "-shm")) if sidecar.exists()
    )
    required_free = max(source_bytes * 2, 16 * 1024 * 1024)
    if shutil.disk_usage(db.parent).free < required_free or shutil.disk_usage(backup.parent).free < source_bytes:
        raise MigrationHold("MIGRATION_HOLD", "insufficient free capacity for verified migration and backup")


def _backup_manifest_path(backup_path: Path) -> Path:
    return Path(str(backup_path) + ".factlane-v3-backup.json")


def _write_backup_manifest(
    backup_path: Path,
    *,
    parent_fd: int,
    source_db: Path,
    backup_sha256: str,
    snapshot: dict[str, Any],
) -> None:
    manifest_path = _backup_manifest_path(backup_path)
    _require_entry_absent(parent_fd, manifest_path.name, label="backup verification manifest")
    payload = {
        "schema_version": 1,
        "kind": "FACTLANE_PRE_V3_BACKUP",
        "source_path_sha256": hashlib.sha256(str(source_db).encode("utf-8")).hexdigest(),
        "backup_sha256": backup_sha256,
        "logical_snapshot_sha256": hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
        "created_at": iso_now(),
    }
    data = (canonical_json(payload) + "\n").encode("utf-8")
    stage_name, stage_fd, stage_identity = _create_private_stage(parent_fd)
    published = False
    try:
        view = memoryview(data)
        while view:
            written = os.write(stage_fd, view)
            if written <= 0:
                raise MigrationHold("MIGRATION_HOLD", "backup verification manifest write failed")
            view = view[written:]
        os.fsync(stage_fd)
        _require_stage_identity(parent_fd, stage_name, stage_identity)
        _publish_private_stage_noreplace(
            parent_fd,
            stage_name,
            stage_identity,
            manifest_path.name,
            label="backup verification manifest",
            include_sqlite_sidecars=False,
        )
        published = True
    finally:
        os.close(stage_fd)
        if not published:
            _cleanup_private_stage(
                parent_fd,
                stage_name,
                stage_identity,
                include_sqlite_sidecars=False,
            )


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


def _windows_migration_classes() -> tuple[type[Any], type[Any], type[BaseException]]:
    try:
        from .windows_maintenance import (
            WindowsMaintenanceError,
            WindowsMigrationArtifactCustody,
            WindowsStagedMigrationLease,
        )
    except Exception as exc:
        raise MigrationHold("MIGRATION_HOLD", "native Windows migration custody is unavailable") from exc
    return WindowsStagedMigrationLease, WindowsMigrationArtifactCustody, WindowsMaintenanceError


def _windows_existing_sqlite_uri(path: Path) -> str:
    normalized = str(path).replace("\\", "/")
    return "file:" + urllib.parse.quote(normalized, safe="/:") + "?mode=rw"


def _windows_readonly_sqlite_uri(path: Path) -> str:
    normalized = str(path).replace("\\", "/")
    return "file:" + urllib.parse.quote(normalized, safe="/:") + "?mode=ro"


def _require_windows_migration_capacity(db: Path, backup: Path) -> None:
    if not db.is_file() or not os.access(db, os.R_OK | os.W_OK):
        raise MigrationHold("MIGRATION_HOLD", "storage database is not readable and writable by the migration operator")
    if not backup.parent.is_dir() or not os.access(backup.parent, os.R_OK | os.W_OK):
        raise MigrationHold("MIGRATION_HOLD", "backup parent must already exist and be writable by the migration operator")
    source_bytes = db.stat().st_size + sum(
        sidecar.stat().st_size for sidecar in (Path(str(db) + "-wal"), Path(str(db) + "-shm")) if sidecar.exists()
    )
    required_free = max(source_bytes * 2, 16 * 1024 * 1024)
    if shutil.disk_usage(db.parent).free < required_free or shutil.disk_usage(backup.parent).free < source_bytes:
        raise MigrationHold("MIGRATION_HOLD", "insufficient free capacity for verified migration and backup")


def _windows_manifest_payload(
    *,
    operation_id: str,
    source_db: Path,
    source_identity: Any,
    backup_receipt: dict[str, Any],
    source_snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": _WINDOWS_MANIFEST_SCHEMA_VERSION,
        "kind": "FACTLANE_PRE_V3_BACKUP",
        "platform": "windows",
        "operation_id": operation_id,
        "source_path_sha256": hashlib.sha256(str(source_db).encode("utf-8")).hexdigest(),
        "source_volume_serial_number": int(source_identity.volume_serial_number),
        "source_file_id": str(source_identity.file_id_hex),
        "backup_sha256": str(backup_receipt["sha256"]),
        "backup_size": int(backup_receipt["size"]),
        "backup_volume_serial_number": int(backup_receipt["volume_serial_number"]),
        "backup_file_id": str(backup_receipt["file_id"]),
        "logical_snapshot_sha256": hashlib.sha256(canonical_json(source_snapshot).encode("utf-8")).hexdigest(),
        "created_at": iso_now(),
    }


def _windows_binding_from_manifest(manifest: dict[str, Any], *, manifest_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "FACTLANE_WINDOWS_V2_V3_MIGRATION_BINDING",
        "operation_id": manifest["operation_id"],
        "source_path_sha256": manifest["source_path_sha256"],
        "source_volume_serial_number": manifest["source_volume_serial_number"],
        "source_file_id": manifest["source_file_id"],
        "backup_sha256": manifest["backup_sha256"],
        "backup_size": manifest["backup_size"],
        "backup_volume_serial_number": manifest["backup_volume_serial_number"],
        "backup_file_id": manifest["backup_file_id"],
        "logical_snapshot_sha256": manifest["logical_snapshot_sha256"],
        "manifest_sha256": manifest_sha256,
    }


def _windows_backup_database(
    conn: sqlite3.Connection,
    lease: Any,
    backup_path: Path,
    *,
    operation_id: str,
    source_snapshot: dict[str, Any],
) -> tuple[str, int, dict[str, Any], str, Any, Any]:
    _, ArtifactCustody, WindowsMaintenanceError = _windows_migration_classes()
    manifest_path = _backup_manifest_path(backup_path)
    if backup_path.exists() or manifest_path.exists():
        raise MigrationHold("BACKUP_EXISTS", "backup destination or verification manifest already exists")

    backup_artifact: Any = None
    manifest_artifact: Any = None
    backup_guard: Any = None
    manifest_guard: Any = None
    backup_conn: sqlite3.Connection | None = None
    try:
        try:
            backup_artifact = ArtifactCustody.create(backup_path)
        except WindowsMaintenanceError as exc:
            code = "BACKUP_EXISTS" if getattr(exc, "code", "") == "MIGRATION_ARTIFACT_EXISTS" else "MIGRATION_HOLD"
            raise MigrationHold(code, exc.safe_message) from exc

        try:
            backup_conn = sqlite3.connect(_windows_existing_sqlite_uri(backup_path), uri=True, timeout=5.0)
            conn.backup(backup_conn)
            backup_conn.commit()
            _quick_integrity(backup_conn)
            _require_v2_schema(backup_conn)
            _require_lineage_compatibility(backup_conn)
            _require_v2_native_profile_parity(backup_conn)
            if _logical_parity_snapshot(backup_conn) != source_snapshot:
                raise MigrationHold("BACKUP_INVALID", "pre-v3 backup does not preserve the source logical state")
        finally:
            if backup_conn is not None:
                backup_conn.close()
                backup_conn = None

        backup_sha256 = _sha256(backup_path)
        backup_size = int(backup_path.stat().st_size)
        try:
            backup_receipt = backup_artifact.verify(expected_sha256=backup_sha256, expected_size=backup_size)
        except WindowsMaintenanceError as exc:
            raise MigrationHold("BACKUP_INVALID", exc.safe_message) from exc
        _windows_migration_checkpoint("after_backup_publication")

        manifest = _windows_manifest_payload(
            operation_id=operation_id,
            source_db=Path(lease.db_path),
            source_identity=lease.identity,
            backup_receipt=backup_receipt,
            source_snapshot=source_snapshot,
        )
        manifest_bytes = (canonical_json(manifest) + "\n").encode("utf-8")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        try:
            manifest_artifact = ArtifactCustody.create(manifest_path)
            manifest_artifact.write_bytes(manifest_bytes)
            manifest_artifact.verify(expected_sha256=manifest_sha256, expected_size=len(manifest_bytes))
        except WindowsMaintenanceError as exc:
            code = "BACKUP_EXISTS" if getattr(exc, "code", "") == "MIGRATION_ARTIFACT_EXISTS" else "BACKUP_INVALID"
            raise MigrationHold(code, exc.safe_message) from exc
        _windows_migration_checkpoint("after_manifest_publication")

        # Writable publication custody is intentionally replaced with
        # restrictive read-only custody before the source can mutate.  The
        # second verification closes the publish->mutation substitution gap.
        manifest_artifact.close()
        manifest_artifact = None
        backup_artifact.close()
        backup_artifact = None
        try:
            backup_guard = ArtifactCustody.open_existing(backup_path)
            guarded_backup = backup_guard.verify(
                expected_sha256=backup_sha256,
                expected_size=backup_size,
            )
            manifest_guard = ArtifactCustody.open_existing(manifest_path)
            guarded_manifest = manifest_guard.verify(
                expected_sha256=manifest_sha256,
                expected_size=len(manifest_bytes),
            )
        except WindowsMaintenanceError as exc:
            raise MigrationHold("BACKUP_INVALID", exc.safe_message) from exc
        if (
            int(guarded_backup["volume_serial_number"]) != int(backup_receipt["volume_serial_number"])
            or str(guarded_backup["file_id"]).casefold() != str(backup_receipt["file_id"]).casefold()
            or str(guarded_manifest["sha256"]).casefold() != manifest_sha256.casefold()
        ):
            raise MigrationHold("BACKUP_INVALID", "migration artifacts changed before restrictive custody was established")
        lease.revalidate()
        returned_backup_guard, backup_guard = backup_guard, None
        returned_manifest_guard, manifest_guard = manifest_guard, None
        return (
            backup_sha256,
            backup_size,
            manifest,
            manifest_sha256,
            returned_backup_guard,
            returned_manifest_guard,
        )
    finally:
        if backup_conn is not None:
            backup_conn.close()
        if manifest_guard is not None:
            manifest_guard.close()
        if backup_guard is not None:
            backup_guard.close()
        if manifest_artifact is not None:
            manifest_artifact.close()
        if backup_artifact is not None:
            backup_artifact.close()


def _require_windows_migration_binding_source_absent(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM adapter_meta WHERE key=?", (_WINDOWS_MIGRATION_BINDING_KEY,)).fetchone() is not None:
        raise MigrationHold("MIGRATION_HOLD", "storage v2 source already contains a Windows migration binding")


def _require_windows_immediate_migrated_v3(
    conn: sqlite3.Connection,
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
    backup_snapshot: dict[str, Any],
) -> None:
    try:
        _validate_existing_v3_schema(conn)
    except AdapterError as exc:
        raise MigrationHold("ROLLBACK_HOLD", "rollback target is not an exact storage-v3 database") from exc
    row = conn.execute("SELECT value FROM adapter_meta WHERE key='contract_version'").fetchone()
    if row is None or str(row[0]) != str(STORAGE_CONTRACT_VERSION):
        raise MigrationHold("ROLLBACK_HOLD", "rollback target is not storage contract v3")
    expected_binding = canonical_json(_windows_binding_from_manifest(manifest, manifest_sha256=manifest_sha256))
    binding_rows = conn.execute(
        "SELECT value FROM adapter_meta WHERE key=?",
        (_WINDOWS_MIGRATION_BINDING_KEY,),
    ).fetchall()
    if binding_rows != [(expected_binding,)]:
        raise MigrationHold("ROLLBACK_HOLD", "rollback target is not bound to the requested pre-v3 backup")
    if _logical_parity_snapshot(conn, excluded_meta_keys=(_WINDOWS_MIGRATION_BINDING_KEY,)) != backup_snapshot:
        raise MigrationHold("ROLLBACK_HOLD", "rollback target has accepted changes after migration")
    for table in ("lifecycle_events", "lifecycle_operations"):
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            raise MigrationHold("ROLLBACK_HOLD", "rollback target has lifecycle activity after migration")
    highwater = conn.execute(
        "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
    ).fetchone()
    if highwater is None or str(highwater[0]) != _trust_clock_high_water(conn):
        raise MigrationHold("ROLLBACK_HOLD", "rollback target trust-clock state is not the immediate migration result")
    _quick_integrity(conn)
    try:
        SensitiveMemoryRecoveryOperator._require_known_recovery_schema(conn)
    except RecoveryHold as exc:
        raise MigrationHold("ROLLBACK_HOLD", "rollback target Recovery schema is not exact") from exc


def _parse_windows_backup_manifest(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationHold("BACKUP_INVALID", "pre-v3 backup manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise MigrationHold("BACKUP_INVALID", "pre-v3 backup manifest is invalid")
    exact_keys = {
        "schema_version",
        "kind",
        "platform",
        "operation_id",
        "source_path_sha256",
        "source_volume_serial_number",
        "source_file_id",
        "backup_sha256",
        "backup_size",
        "backup_volume_serial_number",
        "backup_file_id",
        "logical_snapshot_sha256",
        "created_at",
    }
    if set(payload) != exact_keys:
        raise MigrationHold("BACKUP_INVALID", "pre-v3 backup manifest field set is not exact")
    if (
        payload.get("schema_version") != _WINDOWS_MANIFEST_SCHEMA_VERSION
        or payload.get("kind") != "FACTLANE_PRE_V3_BACKUP"
        or payload.get("platform") != "windows"
    ):
        raise MigrationHold("BACKUP_INVALID", "pre-v3 backup manifest contract is not supported")
    try:
        uuid.UUID(str(payload["operation_id"]))
        parse_iso(str(payload["created_at"]), required=True)
        source_volume = int(payload["source_volume_serial_number"])
        backup_volume = int(payload["backup_volume_serial_number"])
        backup_size = int(payload["backup_size"])
    except (ValueError, TypeError, AdapterError) as exc:
        raise MigrationHold("BACKUP_INVALID", "pre-v3 backup manifest identity fields are invalid") from exc
    if source_volume < 0 or backup_volume < 0 or backup_size <= 0:
        raise MigrationHold("BACKUP_INVALID", "pre-v3 backup manifest numeric identity fields are invalid")
    for key in (
        "source_path_sha256",
        "backup_sha256",
        "logical_snapshot_sha256",
    ):
        value = str(payload[key])
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.casefold()):
            raise MigrationHold("BACKUP_INVALID", f"pre-v3 backup manifest {key} is invalid")
    for key in ("source_file_id", "backup_file_id"):
        value = str(payload[key])
        if not value or any(character not in "0123456789abcdef" for character in value.casefold()):
            raise MigrationHold("BACKUP_INVALID", f"pre-v3 backup manifest {key} is invalid")
    return payload


def _open_verified_windows_backup(
    backup: Path,
) -> tuple[Any, Any, dict[str, Any], str, dict[str, Any]]:
    _, ArtifactCustody, WindowsMaintenanceError = _windows_migration_classes()
    manifest_path = _backup_manifest_path(backup)
    manifest_artifact: Any = None
    backup_artifact: Any = None
    try:
        try:
            manifest_artifact = ArtifactCustody.open_existing(manifest_path)
            manifest_receipt = manifest_artifact.read_exact()
        except WindowsMaintenanceError as exc:
            raise MigrationHold("BACKUP_INVALID", exc.safe_message) from exc
        manifest = _parse_windows_backup_manifest(bytes(manifest_receipt["data"]))
        manifest_sha256 = str(manifest_receipt["sha256"])

        try:
            backup_artifact = ArtifactCustody.open_existing(backup)
            backup_receipt = backup_artifact.verify(
                expected_sha256=str(manifest["backup_sha256"]),
                expected_size=int(manifest["backup_size"]),
            )
        except WindowsMaintenanceError as exc:
            raise MigrationHold("BACKUP_INVALID", exc.safe_message) from exc
        if (
            int(backup_receipt["volume_serial_number"]) != int(manifest["backup_volume_serial_number"])
            or str(backup_receipt["file_id"]).casefold() != str(manifest["backup_file_id"]).casefold()
        ):
            raise MigrationHold("BACKUP_INVALID", "pre-v3 backup file identity does not match its manifest")

        backup_conn = sqlite3.connect(_windows_readonly_sqlite_uri(backup), uri=True, timeout=5.0)
        try:
            _quick_integrity(backup_conn)
            _require_v2_schema(backup_conn)
            _require_lineage_compatibility(backup_conn)
            _require_v2_native_profile_parity(backup_conn)
            backup_snapshot = _logical_parity_snapshot(backup_conn)
        finally:
            backup_conn.close()
        snapshot_sha256 = hashlib.sha256(canonical_json(backup_snapshot).encode("utf-8")).hexdigest()
        if snapshot_sha256 != str(manifest["logical_snapshot_sha256"]):
            raise MigrationHold("BACKUP_INVALID", "pre-v3 backup logical state does not match its manifest")
        return manifest_artifact, backup_artifact, manifest, manifest_sha256, backup_snapshot
    except BaseException:
        if backup_artifact is not None:
            backup_artifact.close()
        if manifest_artifact is not None:
            manifest_artifact.close()
        raise


def _require_windows_manifest_target_identity(lease: Any, manifest: dict[str, Any]) -> None:
    expected_path = hashlib.sha256(str(lease.db_path).encode("utf-8")).hexdigest()
    if expected_path != str(manifest["source_path_sha256"]):
        raise MigrationHold("ROLLBACK_HOLD", "pre-v3 backup manifest is bound to a different database path")
    if (
        int(lease.identity.volume_serial_number) != int(manifest["source_volume_serial_number"])
        or str(lease.identity.file_id_hex).casefold() != str(manifest["source_file_id"]).casefold()
    ):
        raise MigrationHold("ROLLBACK_HOLD", "pre-v3 backup manifest is bound to a different database file identity")


def _active_file_matches_verified_backup(lease: Any, manifest: dict[str, Any]) -> bool:
    lease.revalidate()
    path = Path(lease.db_path)
    if path.stat().st_size != int(manifest["backup_size"]):
        lease.revalidate()
        return False
    matches = _sha256(path).casefold() == str(manifest["backup_sha256"]).casefold()
    lease.revalidate()
    return matches


def _windows_rollback_intent_path(db: Path) -> Path:
    try:
        from .windows_maintenance import windows_migration_rollback_intent_path
    except Exception as exc:
        raise MigrationHold("ROLLBACK_HOLD", "native Windows rollback-intent custody is unavailable") from exc
    return Path(windows_migration_rollback_intent_path(db))


def _windows_rollback_intent_payload(
    *,
    operation_id: str,
    lease: Any,
    manifest: dict[str, Any],
    manifest_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": _WINDOWS_ROLLBACK_INTENT_SCHEMA_VERSION,
        "kind": "FACTLANE_PRE_V3_ROLLBACK_INTENT",
        "platform": "windows",
        "operation_id": operation_id,
        "migration_operation_id": str(manifest["operation_id"]),
        "target_path_sha256": hashlib.sha256(str(lease.db_path).encode("utf-8")).hexdigest(),
        "target_volume_serial_number": int(lease.identity.volume_serial_number),
        "target_file_id": str(lease.identity.file_id_hex),
        "backup_sha256": str(manifest["backup_sha256"]),
        "backup_size": int(manifest["backup_size"]),
        "backup_volume_serial_number": int(manifest["backup_volume_serial_number"]),
        "backup_file_id": str(manifest["backup_file_id"]),
        "manifest_sha256": manifest_sha256,
        "restored_contract": 2,
        "created_at": iso_now(),
    }


def _parse_windows_rollback_intent(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationHold("ROLLBACK_HOLD", "rollback intent is invalid") from exc
    if not isinstance(payload, dict):
        raise MigrationHold("ROLLBACK_HOLD", "rollback intent is invalid")
    exact_keys = {
        "schema_version",
        "kind",
        "platform",
        "operation_id",
        "migration_operation_id",
        "target_path_sha256",
        "target_volume_serial_number",
        "target_file_id",
        "backup_sha256",
        "backup_size",
        "backup_volume_serial_number",
        "backup_file_id",
        "manifest_sha256",
        "restored_contract",
        "created_at",
    }
    if set(payload) != exact_keys:
        raise MigrationHold("ROLLBACK_HOLD", "rollback intent field set is not exact")
    if (
        payload.get("schema_version") != _WINDOWS_ROLLBACK_INTENT_SCHEMA_VERSION
        or payload.get("kind") != "FACTLANE_PRE_V3_ROLLBACK_INTENT"
        or payload.get("platform") != "windows"
        or payload.get("restored_contract") != 2
    ):
        raise MigrationHold("ROLLBACK_HOLD", "rollback intent contract is not supported")
    try:
        uuid.UUID(str(payload["operation_id"]))
        uuid.UUID(str(payload["migration_operation_id"]))
        parse_iso(str(payload["created_at"]), required=True)
        target_volume = int(payload["target_volume_serial_number"])
        backup_volume = int(payload["backup_volume_serial_number"])
        backup_size = int(payload["backup_size"])
    except (ValueError, TypeError, AdapterError) as exc:
        raise MigrationHold("ROLLBACK_HOLD", "rollback intent identity fields are invalid") from exc
    if target_volume < 0 or backup_volume < 0 or backup_size <= 0:
        raise MigrationHold("ROLLBACK_HOLD", "rollback intent numeric identity fields are invalid")
    for key in ("target_path_sha256", "backup_sha256", "manifest_sha256"):
        value = str(payload[key])
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.casefold()):
            raise MigrationHold("ROLLBACK_HOLD", f"rollback intent {key} is invalid")
    for key in ("target_file_id", "backup_file_id"):
        value = str(payload[key])
        if not value or any(character not in "0123456789abcdef" for character in value.casefold()):
            raise MigrationHold("ROLLBACK_HOLD", f"rollback intent {key} is invalid")
    return payload


def _require_windows_rollback_intent_binding(
    payload: dict[str, Any],
    *,
    lease: Any,
    manifest: dict[str, Any],
    manifest_sha256: str,
) -> None:
    expected_path_sha256 = hashlib.sha256(str(lease.db_path).encode("utf-8")).hexdigest()
    if (
        str(payload["migration_operation_id"]) != str(manifest["operation_id"])
        or str(payload["target_path_sha256"]) != expected_path_sha256
        or int(payload["target_volume_serial_number"]) != int(lease.identity.volume_serial_number)
        or str(payload["target_file_id"]).casefold() != str(lease.identity.file_id_hex).casefold()
        or str(payload["backup_sha256"]).casefold() != str(manifest["backup_sha256"]).casefold()
        or int(payload["backup_size"]) != int(manifest["backup_size"])
        or int(payload["backup_volume_serial_number"]) != int(manifest["backup_volume_serial_number"])
        or str(payload["backup_file_id"]).casefold() != str(manifest["backup_file_id"]).casefold()
        or str(payload["manifest_sha256"]).casefold() != manifest_sha256.casefold()
    ):
        raise MigrationHold("ROLLBACK_HOLD", "rollback intent is not bound to this verified backup and target")


def _publish_windows_rollback_intent(
    backup: Path,
    *,
    operation_id: str,
    lease: Any,
    manifest: dict[str, Any],
    manifest_sha256: str,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    _, ArtifactCustody, WindowsMaintenanceError = _windows_migration_classes()
    intent_path = _windows_rollback_intent_path(Path(lease.db_path))
    payload = _windows_rollback_intent_payload(
        operation_id=operation_id,
        lease=lease,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
    )
    intent_bytes = (canonical_json(payload) + "\n").encode("utf-8")
    intent_sha256 = hashlib.sha256(intent_bytes).hexdigest()
    writable: Any = None
    guard: Any = None
    try:
        try:
            writable = ArtifactCustody.create(intent_path)
            writable.write_bytes(intent_bytes)
            first_receipt = writable.verify(expected_sha256=intent_sha256, expected_size=len(intent_bytes))
        except WindowsMaintenanceError as exc:
            code = "ROLLBACK_HOLD"
            if getattr(exc, "code", "") == "MIGRATION_ARTIFACT_EXISTS":
                code = "ROLLBACK_HOLD"
            raise MigrationHold(code, exc.safe_message) from exc
        writable.close()
        writable = None
        try:
            guard = ArtifactCustody.open_existing(intent_path)
            guarded_receipt = guard.verify(expected_sha256=intent_sha256, expected_size=len(intent_bytes))
        except WindowsMaintenanceError as exc:
            raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc
        if (
            int(guarded_receipt["volume_serial_number"]) != int(first_receipt["volume_serial_number"])
            or str(guarded_receipt["file_id"]).casefold() != str(first_receipt["file_id"]).casefold()
        ):
            raise MigrationHold("ROLLBACK_HOLD", "rollback intent changed before restrictive custody was established")
        _require_windows_rollback_intent_binding(
            payload,
            lease=lease,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
        )
        returned_guard, guard = guard, None
        return returned_guard, payload, guarded_receipt
    finally:
        if guard is not None:
            guard.close()
        if writable is not None:
            writable.close()


def _open_verified_windows_rollback_intent(
    backup: Path,
    *,
    lease: Any,
    manifest: dict[str, Any],
    manifest_sha256: str,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    _, ArtifactCustody, WindowsMaintenanceError = _windows_migration_classes()
    intent_path = _windows_rollback_intent_path(Path(lease.db_path))
    guard: Any = None
    try:
        try:
            guard = ArtifactCustody.open_existing(intent_path)
            receipt = guard.read_exact()
        except WindowsMaintenanceError as exc:
            raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc
        payload = _parse_windows_rollback_intent(bytes(receipt["data"]))
        _require_windows_rollback_intent_binding(
            payload,
            lease=lease,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
        )
        returned_guard, guard = guard, None
        return returned_guard, payload, receipt
    finally:
        if guard is not None:
            guard.close()


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


def _backup_database(
    conn: sqlite3.Connection,
    backup_path: Path,
    *,
    parent_fd: int,
    source_db: Path,
    source_snapshot: dict[str, Any],
) -> str:
    _require_entry_absent(parent_fd, backup_path.name, label="backup destination")
    _require_entry_absent(
        parent_fd,
        _backup_manifest_path(backup_path).name,
        label="backup verification manifest",
    )
    stage_name, stage_fd, stage_identity = _create_private_stage(parent_fd)
    stage_path = backup_path.parent / stage_name
    backup: sqlite3.Connection | None = None
    published = False
    try:
        try:
            _require_stage_identity(parent_fd, stage_name, stage_identity)
            stage_uri = "file:" + urllib.parse.quote(str(stage_path), safe="/") + "?mode=rw"
            try:
                backup = sqlite3.connect(stage_uri, uri=True, timeout=5.0)
            except sqlite3.Error as exc:
                raise MigrationHold(
                    "MIGRATION_HOLD",
                    "private backup stage could not be reopened existing-only",
                ) from exc
            _require_stage_identity(parent_fd, stage_name, stage_identity)
            conn.backup(backup)
            backup.commit()
            _integrity(backup)
            _require_v2_schema(backup)
            _require_lineage_compatibility(backup)
            _require_v2_native_profile_parity(backup)
            if _logical_parity_snapshot(backup) != source_snapshot:
                raise MigrationHold("BACKUP_INVALID", "pre-v3 backup does not preserve the source logical state")
        finally:
            if backup is not None:
                backup.close()
        os.fchmod(stage_fd, 0o600)
        os.fsync(stage_fd)
        _require_stage_identity(parent_fd, stage_name, stage_identity)
        _publish_private_stage_noreplace(
            parent_fd,
            stage_name,
            stage_identity,
            backup_path.name,
            label="backup destination",
            include_sqlite_sidecars=True,
        )
        published = True
    finally:
        os.close(stage_fd)
        if not published:
            _cleanup_private_stage(
                parent_fd,
                stage_name,
                stage_identity,
                include_sqlite_sidecars=True,
            )
    backup_sha256 = _sha256(backup_path)
    _write_backup_manifest(
        backup_path,
        parent_fd=parent_fd,
        source_db=source_db,
        backup_sha256=backup_sha256,
        snapshot=source_snapshot,
    )
    return backup_sha256


def _require_windows_frozen_v3_stage(
    stage_path: str,
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
    backup_snapshot: dict[str, Any],
) -> None:
    """Re-prove the exact frozen private v3 image without opening the active DB."""

    stage = Path(stage_path)
    conn = sqlite3.connect(_windows_readonly_sqlite_uri(stage), uri=True, timeout=5.0)
    try:
        _require_windows_immediate_migrated_v3(
            conn,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            backup_snapshot=backup_snapshot,
        )
    finally:
        conn.close()


def _publish_and_promote_windows_v3_stage(
    lease: Any,
    backup: Path,
    *,
    operation_id: str,
    manifest: dict[str, Any],
    manifest_sha256: str,
    backup_snapshot: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    """Freeze/verify stage, publish rollback intent, then raw-promote in place."""

    _StagedLease, ArtifactCustody, WindowsMaintenanceError = _windows_migration_classes()
    try:
        stage_receipt = lease.freeze_staged_image()
    except WindowsMaintenanceError as exc:
        raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc
    _require_windows_frozen_v3_stage(
        str(stage_receipt["path"]),
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        backup_snapshot=backup_snapshot,
    )
    _windows_migration_checkpoint("after_migration_post_verification")

    intent_guard, _intent, intent_receipt = _publish_windows_rollback_intent(
        backup,
        operation_id=operation_id,
        lease=lease,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
    )
    _windows_migration_checkpoint("after_rollback_intent_publication")
    try:
        lease.promote_frozen_stage(
            rollback_intent_verified=True,
            expected_sha256=str(stage_receipt["sha256"]),
            expected_size=int(stage_receipt["size"]),
        )
        _windows_migration_checkpoint("after_sidecar_cleanup")
        lease.prepare_runtime_activation()
    except WindowsMaintenanceError as exc:
        raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc

    return intent_guard, intent_receipt


def _consume_windows_rollback_intent(
    intent_guard: Any,
    intent_receipt: dict[str, Any],
    intent_path: Path,
) -> None:
    _StagedLease, ArtifactCustody, WindowsMaintenanceError = _windows_migration_classes()
    intent_guard.close()
    try:
        ArtifactCustody.delete_existing_verified(
            intent_path,
            expected_sha256=str(intent_receipt["sha256"]),
            expected_size=int(intent_receipt["size"]),
            expected_volume_serial_number=int(intent_receipt["volume_serial_number"]),
            expected_file_id=str(intent_receipt["file_id"]),
        )
    except WindowsMaintenanceError as exc:
        raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc


def _resume_storage_v3_windows(db: Path, backup: Path) -> dict[str, Any]:
    StagedLease, _, WindowsMaintenanceError = _windows_migration_classes()
    (
        manifest_artifact,
        backup_artifact,
        manifest,
        manifest_sha256,
        backup_snapshot,
    ) = _open_verified_windows_backup(backup)
    operation_id = str(manifest["operation_id"])
    lease: Any = None
    intent_guard: Any = None
    intent_receipt: dict[str, Any] | None = None
    intent_path = _windows_rollback_intent_path(db)
    try:
        try:
            lease = StagedLease.acquire(db)
            _require_windows_manifest_target_identity(lease, manifest)
            recovery_conn = lease.open_verified_recovery_source(recovery_proof_verified=True)
        except WindowsMaintenanceError as exc:
            raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc

        contract_row = recovery_conn.execute("SELECT value FROM adapter_meta WHERE key='contract_version'").fetchone()
        contract = str(contract_row[0]) if contract_row is not None else ""
        if contract == str(STORAGE_CONTRACT_VERSION):
            _require_windows_immediate_migrated_v3(
                recovery_conn,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                backup_snapshot=backup_snapshot,
            )
            try:
                lease.release_verified_recovery_guard_unmutated()
                lease = None
            except WindowsMaintenanceError as exc:
                raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc
        elif contract == "2":
            _quick_integrity(recovery_conn)
            _require_v2_schema(recovery_conn)
            _require_lineage_compatibility(recovery_conn)
            _require_v2_native_profile_parity(recovery_conn)
            _require_windows_migration_binding_source_absent(recovery_conn)
            if _logical_parity_snapshot(recovery_conn) != backup_snapshot:
                raise MigrationHold("MIGRATION_HOLD", "interrupted migration v2 state differs from its verified backup")
        else:
            raise MigrationHold("MIGRATION_HOLD", "interrupted migration target is neither verified v2 nor bound v3")

        if contract == "2":
            assert lease is not None
            try:
                conn = lease.transition_verified_recovery_guard_to_mutation(recovery_target_verified=True)
            except WindowsMaintenanceError as exc:
                raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc
            register_storage_legacy_maintenance_writer(conn)
            binding = _windows_binding_from_manifest(manifest, manifest_sha256=manifest_sha256)
            committed = False
            try:
                conn.execute("BEGIN IMMEDIATE")
                _create_v3_governance_schema(conn)
                _windows_migration_checkpoint("after_schema_transaction")
                if _logical_parity_snapshot(conn) != backup_snapshot:
                    raise MigrationHold("MIGRATION_HOLD", "resumed migration changed historical native/vector/adapter rows")
                conn.execute(
                    "INSERT INTO adapter_meta(key,value) VALUES (?,?)",
                    (_WINDOWS_MIGRATION_BINDING_KEY, canonical_json(binding)),
                )
                # Contract version is the final database write in the schema transaction.
                conn.execute(
                    "UPDATE adapter_meta SET value=? WHERE key='contract_version'",
                    (str(STORAGE_CONTRACT_VERSION),),
                )
                _windows_migration_checkpoint("after_contract_version_write")
                _validate_existing_v3_schema(conn)
                _quick_integrity(conn)
                if _logical_parity_snapshot(
                    conn,
                    excluded_meta_keys=(_WINDOWS_MIGRATION_BINDING_KEY,),
                ) != backup_snapshot:
                    raise MigrationHold("MIGRATION_HOLD", "resumed pre-commit migration parity verification failed")
                try:
                    SensitiveMemoryRecoveryOperator._require_known_recovery_schema(conn)
                except RecoveryHold as exc:
                    raise MigrationHold("MIGRATION_HOLD", "resumed pre-commit Recovery schema verification failed") from exc
                conn.commit()
                committed = True
                _windows_migration_checkpoint("after_v3_commit")
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                if not committed:
                    try:
                        _quick_integrity(conn)
                        _require_v2_schema(conn)
                        _require_lineage_compatibility(conn)
                        _require_v2_native_profile_parity(conn)
                        if _logical_parity_snapshot(conn) != backup_snapshot:
                            raise MigrationHold("MIGRATION_HOLD", "resumed failed migration did not roll back to verified v2 state")
                        lease.release_staged_mutation_unmutated()
                        lease = None
                    except BaseException:
                        if lease is not None:
                            lease.retain_fail_closed()
                        raise
                raise
            _require_windows_immediate_migrated_v3(
                conn,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                backup_snapshot=backup_snapshot,
            )
            intent_guard, intent_receipt = _publish_and_promote_windows_v3_stage(
                lease,
                backup,
                operation_id=operation_id,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                backup_snapshot=backup_snapshot,
            )
            try:
                backup_artifact.verify(
                    expected_sha256=str(manifest["backup_sha256"]),
                    expected_size=int(manifest["backup_size"]),
                )
                manifest_artifact.revalidate()
            except WindowsMaintenanceError as exc:
                raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc
            assert intent_receipt is not None
            _consume_windows_rollback_intent(intent_guard, intent_receipt, intent_path)
            intent_guard = None
            try:
                lease.close()
                lease = None
            except WindowsMaintenanceError as exc:
                raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc
    except BaseException:
        if lease is not None:
            try:
                phase = getattr(lease, "phase", None)
                if phase in {
                    getattr(lease, "PHASE_CUSTODY", object()),
                    getattr(lease, "PHASE_BACKUP_GUARD", object()),
                    getattr(lease, "PHASE_RECOVERY_GUARD", object()),
                }:
                    lease.release_unmutated()
                elif phase == getattr(lease, "PHASE_MUTATION", object()) and not getattr(
                    lease, "_active_overwrite_started", True
                ):
                    lease.release_staged_mutation_unmutated()
                else:
                    lease.retain_fail_closed()
            except Exception:
                try:
                    lease.retain_fail_closed()
                except Exception:
                    pass
        raise
    finally:
        if intent_guard is not None:
            intent_guard.close()
        backup_artifact.close()
        manifest_artifact.close()
    return {
        "status": "OK",
        "operation": "MIGRATE_STORAGE_V3",
        "operation_id": operation_id,
        "database": str(db),
        "from_contract": 2,
        "to_contract": STORAGE_CONTRACT_VERSION,
        "backup": str(backup),
        "backup_sha256": str(manifest["backup_sha256"]),
        "backup_size": int(manifest["backup_size"]),
        "backup_manifest_sha256": manifest_sha256,
        "resumed": True,
        "completed_at": iso_now(),
    }


def _migrate_storage_v3_windows(db_path: str, backup_path: str) -> dict[str, Any]:
    db = _requested_windows_path(db_path, label="database")
    backup = _requested_windows_path(backup_path, label="backup destination")
    manifest_path = _backup_manifest_path(backup)
    intent_path = _windows_rollback_intent_path(db)
    if intent_path.exists():
        if backup.is_file() and manifest_path.is_file():
            # A raw staged promotion may have crashed with a partially written
            # target that is not SQLite-openable.  The durable intent is the
            # authority to restore exact verified v2 bytes first; only then is
            # migration resumed on a new private stage.
            _restore_pre_v3_backup_windows(str(db), str(backup))
            return _resume_storage_v3_windows(db, backup)
        raise MigrationHold(
            "MIGRATION_HOLD",
            "storage migration is blocked by an unresolved exact-rollback intent with incomplete artifacts",
        )
    if backup.exists() or manifest_path.exists():
        if backup.is_file() and manifest_path.is_file():
            return _resume_storage_v3_windows(db, backup)
        raise MigrationHold("BACKUP_INVALID", "interrupted migration artifacts are incomplete")
    operation_id = str(uuid.uuid4())
    StagedLease, _, WindowsMaintenanceError = _windows_migration_classes()
    _require_windows_migration_capacity(db, backup)

    lease: Any = None
    backup_sha256 = ""
    backup_size = 0
    manifest: dict[str, Any] | None = None
    manifest_sha256 = ""
    source_snapshot: dict[str, Any] | None = None
    backup_guard: Any = None
    manifest_guard: Any = None
    intent_guard: Any = None
    intent_receipt: dict[str, Any] | None = None
    committed = False
    try:
        try:
            lease = StagedLease.acquire(db)
            source = lease.open_backup_source()
        except WindowsMaintenanceError as exc:
            raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc

        register_storage_legacy_maintenance_writer(source)
        _quick_integrity(source)
        _require_v2_schema(source)
        _require_lineage_compatibility(source)
        _require_v2_native_profile_parity(source)
        _require_windows_migration_binding_source_absent(source)
        source_snapshot = _logical_parity_snapshot(source)
        (
            backup_sha256,
            backup_size,
            manifest,
            manifest_sha256,
            backup_guard,
            manifest_guard,
        ) = _windows_backup_database(
            source,
            lease,
            backup,
            operation_id=operation_id,
            source_snapshot=source_snapshot,
        )
        _windows_migration_checkpoint("after_backup_manifest")

        try:
            conn = lease.transition_to_mutation(backup_manifest_verified=True)
        except WindowsMaintenanceError as exc:
            raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc
        _windows_migration_checkpoint("after_transition_to_mutation")
        _windows_migration_checkpoint("after_sidecar_cleanup")
        register_storage_legacy_maintenance_writer(conn)
        binding = _windows_binding_from_manifest(manifest, manifest_sha256=manifest_sha256)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _create_v3_governance_schema(conn)
            _windows_migration_checkpoint("after_schema_transaction")
            if _logical_parity_snapshot(conn) != source_snapshot:
                raise MigrationHold("MIGRATION_HOLD", "migration changed historical native/vector/adapter rows")
            conn.execute(
                "INSERT INTO adapter_meta(key,value) VALUES (?,?)",
                (_WINDOWS_MIGRATION_BINDING_KEY, canonical_json(binding)),
            )
            # Contract version is the final database write in the schema transaction.
            conn.execute(
                "UPDATE adapter_meta SET value=? WHERE key='contract_version'",
                (str(STORAGE_CONTRACT_VERSION),),
            )
            _windows_migration_checkpoint("after_contract_version_write")
            _validate_existing_v3_schema(conn)
            _quick_integrity(conn)
            if _logical_parity_snapshot(
                conn,
                excluded_meta_keys=(_WINDOWS_MIGRATION_BINDING_KEY,),
            ) != source_snapshot:
                raise MigrationHold("MIGRATION_HOLD", "pre-commit migration parity verification failed")
            try:
                SensitiveMemoryRecoveryOperator._require_known_recovery_schema(conn)
            except RecoveryHold as exc:
                raise MigrationHold("MIGRATION_HOLD", "pre-commit Recovery schema verification failed") from exc
            conn.commit()
            committed = True
            _windows_migration_checkpoint("after_v3_commit")
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            if not committed:
                try:
                    _quick_integrity(conn)
                    _require_v2_schema(conn)
                    _require_lineage_compatibility(conn)
                    _require_v2_native_profile_parity(conn)
                    if _logical_parity_snapshot(conn) != source_snapshot:
                        raise MigrationHold("MIGRATION_HOLD", "failed migration did not roll back to the exact v2 logical state")
                    lease.release_staged_mutation_unmutated()
                    lease = None
                except BaseException:
                    if lease is not None:
                        lease.retain_fail_closed()
                    raise
            raise

        _validate_existing_v3_schema(conn)
        _quick_integrity(conn)
        if _logical_parity_snapshot(
            conn,
            excluded_meta_keys=(_WINDOWS_MIGRATION_BINDING_KEY,),
        ) != source_snapshot:
            raise MigrationHold("MIGRATION_HOLD", "post-migration native/vector/adapter parity check failed")
        expected_binding = canonical_json(binding)
        if conn.execute(
            "SELECT value FROM adapter_meta WHERE key=?",
            (_WINDOWS_MIGRATION_BINDING_KEY,),
        ).fetchall() != [(expected_binding,)]:
            raise MigrationHold("MIGRATION_HOLD", "post-migration backup binding verification failed")
        try:
            SensitiveMemoryRecoveryOperator._require_known_recovery_schema(conn)
        except RecoveryHold as exc:
            raise MigrationHold("MIGRATION_HOLD", "post-migration Recovery schema preflight failed") from exc
        assert manifest is not None
        assert source_snapshot is not None
        intent_guard, intent_receipt = _publish_and_promote_windows_v3_stage(
            lease,
            backup,
            operation_id=operation_id,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            backup_snapshot=source_snapshot,
        )
        try:
            assert backup_guard is not None and manifest_guard is not None
            backup_guard.verify(expected_sha256=backup_sha256, expected_size=backup_size)
            manifest_guard.revalidate()
        except WindowsMaintenanceError as exc:
            raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc
        assert intent_receipt is not None
        _consume_windows_rollback_intent(intent_guard, intent_receipt, intent_path)
        intent_guard = None
        try:
            lease.close()
            lease = None
        except WindowsMaintenanceError as exc:
            raise MigrationHold("MIGRATION_HOLD", exc.safe_message) from exc
    except BaseException:
        if lease is not None:
            phase = getattr(lease, "phase", None)
            try:
                if phase in {
                    getattr(lease, "PHASE_CUSTODY", object()),
                    getattr(lease, "PHASE_BACKUP_GUARD", object()),
                }:
                    lease.release_unmutated()
                elif phase == getattr(lease, "PHASE_MUTATION", object()) and not getattr(
                    lease, "_active_overwrite_started", True
                ):
                    lease.release_staged_mutation_unmutated()
                else:
                    lease.retain_fail_closed()
            except Exception:
                try:
                    lease.retain_fail_closed()
                except Exception:
                    pass
        if manifest_guard is not None:
            manifest_guard.close()
            manifest_guard = None
        if backup_guard is not None:
            backup_guard.close()
            backup_guard = None
        if intent_guard is not None:
            intent_guard.close()
            intent_guard = None
        raise

    if manifest_guard is not None:
        manifest_guard.close()
        manifest_guard = None
    if backup_guard is not None:
        backup_guard.close()
        backup_guard = None
    return {
        "status": "OK",
        "operation": "MIGRATE_STORAGE_V3",
        "operation_id": operation_id,
        "database": str(db),
        "from_contract": 2,
        "to_contract": STORAGE_CONTRACT_VERSION,
        "backup": str(backup),
        "backup_sha256": backup_sha256,
        "backup_size": backup_size,
        "backup_manifest_sha256": manifest_sha256,
        "completed_at": iso_now(),
    }


def migrate_storage_v3(db_path: str, backup_path: str) -> dict[str, Any]:
    if os.name == "nt":
        return _migrate_storage_v3_windows(db_path, backup_path)
    db = Path(db_path).resolve()
    backup = _canonical_backup_target(backup_path)
    if inspect_storage_contract_version(str(db)) != 2:
        raise MigrationHold("SCHEMA_MISMATCH", "migrate-storage-v3 requires an exact storage-v2 database")
    operation_id = str(uuid.uuid4())
    try:
        lease_context = MaintenanceLease(str(db), f"migration:{operation_id}")
        with lease_context:
            parent_fd = _open_private_backup_parent(backup)
            try:
                _require_migration_capacity_and_permissions(db, backup, parent_fd)
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
                        parent_fd=parent_fd,
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
            finally:
                os.close(parent_fd)
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


def _restore_pre_v3_backup_windows(db_path: str, backup_path: str) -> dict[str, Any]:
    db = _requested_windows_path(db_path, label="database")
    backup = _requested_windows_path(backup_path, label="backup")
    StagedLease, ArtifactCustody, WindowsMaintenanceError = _windows_migration_classes()
    (
        manifest_artifact,
        backup_artifact,
        manifest,
        manifest_sha256,
        backup_snapshot,
    ) = _open_verified_windows_backup(backup)
    operation_id = str(uuid.uuid4())
    lease: Any = None
    intent_guard: Any = None
    intent_receipt: dict[str, Any] | None = None
    intent_path = _windows_rollback_intent_path(db)
    try:
        try:
            lease = StagedLease.acquire(db)
            _require_windows_manifest_target_identity(lease, manifest)
        except WindowsMaintenanceError as exc:
            raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc

        if intent_path.exists():
            intent_guard, _intent, intent_receipt = _open_verified_windows_rollback_intent(
                backup,
                lease=lease,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
            )

        exact_already_restored = _active_file_matches_verified_backup(lease, manifest)
        if exact_already_restored and (
            Path(str(db) + "-wal").exists() or Path(str(db) + "-shm").exists()
        ):
            # Without durable intent, unexpected sidecars are a no-mutation
            # HOLD.  With intent, route through raw retry so retained native
            # custody invalidates them before exact v2 is released.
            if intent_guard is not None:
                exact_already_restored = False
        if exact_already_restored:
            try:
                lease.prepare_verified_exact_restore_release(
                    recovery_proof_verified=True,
                    expected_sha256=str(manifest["backup_sha256"]),
                    expected_size=int(manifest["backup_size"]),
                )
            except WindowsMaintenanceError as exc:
                raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc

        if lease is not None and not exact_already_restored:
            raw_retry_from_intent = False
            recovery_conn: sqlite3.Connection | None = None
            try:
                recovery_conn = lease.open_verified_recovery_source(recovery_proof_verified=True)
            except WindowsMaintenanceError as exc:
                if intent_guard is not None and getattr(exc, "code", "") == "MIGRATION_RECOVERY_STAGE_UNPROVEN":
                    raw_retry_from_intent = True
                else:
                    raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc

            if not raw_retry_from_intent:
                try:
                    assert recovery_conn is not None
                    contract_row = recovery_conn.execute(
                        "SELECT value FROM adapter_meta WHERE key='contract_version'"
                    ).fetchone()
                    contract = str(contract_row[0]) if contract_row is not None else ""
                    if contract == str(STORAGE_CONTRACT_VERSION):
                        _require_windows_immediate_migrated_v3(
                            recovery_conn,
                            manifest=manifest,
                            manifest_sha256=manifest_sha256,
                            backup_snapshot=backup_snapshot,
                        )
                    elif contract == "2":
                        _quick_integrity(recovery_conn)
                        _require_v2_schema(recovery_conn)
                        _require_lineage_compatibility(recovery_conn)
                        _require_v2_native_profile_parity(recovery_conn)
                        _require_windows_migration_binding_source_absent(recovery_conn)
                        if _logical_parity_snapshot(recovery_conn) != backup_snapshot:
                            if intent_guard is not None:
                                raw_retry_from_intent = True
                            else:
                                raise MigrationHold(
                                    "ROLLBACK_HOLD",
                                    "rollback target v2 state differs from the verified pre-v3 backup",
                                )
                    else:
                        raise MigrationHold(
                            "ROLLBACK_HOLD",
                            "rollback target is neither the verified v2 source nor its immediate v3 migration",
                        )
                except sqlite3.DatabaseError:
                    if intent_guard is None:
                        raise MigrationHold("ROLLBACK_HOLD", "rollback target cannot be proven recovery-eligible")
                    raw_retry_from_intent = True
                except MigrationHold as exc:
                    structural_interrupted_restore = (
                        exc.code == "ROLLBACK_HOLD"
                        and exc.safe_message == "rollback target is not an exact storage-v3 database"
                    )
                    if intent_guard is None or (exc.code == "ROLLBACK_HOLD" and not structural_interrupted_restore):
                        raise
                    raw_retry_from_intent = True

            if raw_retry_from_intent:
                if getattr(lease, "phase", None) == getattr(lease, "PHASE_RECOVERY_GUARD", object()):
                    try:
                        lease.release_verified_recovery_guard_unmutated()
                    except WindowsMaintenanceError as exc:
                        raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc
                    lease = None
                if lease is None or getattr(lease, "_closed", False):
                    try:
                        lease = StagedLease.acquire(db)
                        _require_windows_manifest_target_identity(lease, manifest)
                    except WindowsMaintenanceError as exc:
                        raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc
                assert intent_guard is not None
                _require_windows_rollback_intent_binding(
                    _parse_windows_rollback_intent(bytes(intent_guard.read_exact()["data"])),
                    lease=lease,
                    manifest=manifest,
                    manifest_sha256=manifest_sha256,
                )
                try:
                    lease.restore_exact_from_backup_from_custody(
                        backup,
                        recovery_proof_verified=True,
                        expected_sha256=str(manifest["backup_sha256"]),
                        expected_size=int(manifest["backup_size"]),
                    )
                except WindowsMaintenanceError as exc:
                    raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc
            else:
                try:
                    lease.transition_verified_recovery_guard_to_mutation(recovery_target_verified=True)
                except WindowsMaintenanceError as exc:
                    raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc

                if intent_guard is None:
                    intent_guard, _intent, intent_receipt = _publish_windows_rollback_intent(
                        backup,
                        operation_id=operation_id,
                        lease=lease,
                        manifest=manifest,
                        manifest_sha256=manifest_sha256,
                    )
                    _windows_migration_checkpoint("after_rollback_intent_publication")
                try:
                    lease.restore_exact_from_backup(
                        backup,
                        rollback_intent_verified=True,
                        expected_sha256=str(manifest["backup_sha256"]),
                        expected_size=int(manifest["backup_size"]),
                    )
                except WindowsMaintenanceError as exc:
                    raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc
                _windows_migration_checkpoint("after_sidecar_cleanup")
            _windows_migration_checkpoint("after_exact_restore")
            try:
                lease.prepare_exact_restore_release()
            except WindowsMaintenanceError as exc:
                raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc

        assert lease is not None
        if not _active_file_matches_verified_backup(lease, manifest):
            raise MigrationHold("ROLLBACK_HOLD", "restored database bytes do not exactly match the verified pre-v3 backup")
        try:
            backup_artifact.verify(
                expected_sha256=str(manifest["backup_sha256"]),
                expected_size=int(manifest["backup_size"]),
            )
            manifest_artifact.revalidate()
        except WindowsMaintenanceError as exc:
            raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc
        _windows_migration_checkpoint("after_restore_post_verification")

        if intent_guard is not None:
            assert intent_receipt is not None
            intent_guard.close()
            intent_guard = None
            try:
                ArtifactCustody.delete_existing_verified(
                    intent_path,
                    expected_sha256=str(intent_receipt["sha256"]),
                    expected_size=int(intent_receipt["size"]),
                    expected_volume_serial_number=int(intent_receipt["volume_serial_number"]),
                    expected_file_id=str(intent_receipt["file_id"]),
                )
            except WindowsMaintenanceError as exc:
                raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc
        try:
            lease.close()
            lease = None
        except WindowsMaintenanceError as exc:
            raise MigrationHold("ROLLBACK_HOLD", exc.safe_message) from exc
    except BaseException:
        if lease is not None:
            try:
                phase = getattr(lease, "phase", None)
                if phase in {
                    getattr(lease, "PHASE_CUSTODY", object()),
                    getattr(lease, "PHASE_BACKUP_GUARD", object()),
                    getattr(lease, "PHASE_RECOVERY_GUARD", object()),
                }:
                    lease.release_unmutated()
                else:
                    lease.retain_fail_closed()
            except Exception:
                try:
                    lease.retain_fail_closed()
                except Exception:
                    pass
        raise
    finally:
        if intent_guard is not None:
            intent_guard.close()
        backup_artifact.close()
        manifest_artifact.close()
    return {
        "status": "OK",
        "operation": "RESTORE_PRE_V3_BACKUP",
        "operation_id": operation_id,
        "database": str(db),
        "restored_contract": 2,
        "backup": str(backup),
        "backup_sha256": str(manifest["backup_sha256"]),
        "backup_manifest_sha256": manifest_sha256,
        "completed_at": iso_now(),
    }


def restore_pre_v3_backup(db_path: str, backup_path: str) -> dict[str, Any]:
    if os.name == "nt":
        return _restore_pre_v3_backup_windows(db_path, backup_path)
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
