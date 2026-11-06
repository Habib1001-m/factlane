from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .backend_compat import (
    bind_deferred_embedding_initializer,
    execute_with_backend_retry,
    load_pinned_sqlite_vec_storage,
)
from .contract import (
    AdapterError,
    ScopeContext,
    canonical_json,
    digest,
    finite_vector,
    is_fresh,
    iso_now,
    native_hash,
    parse_iso,
    validate_identifier,
    validate_freshness,
    validate_scope,
)
from .embeddings import EmbeddingProfile
from .embedding_compatibility import parse_embedding_profile_metadata

STORAGE_CONTRACT_VERSION = 3
MIN_SQLITE_VERSION = (3, 42, 0)
_WRITER_FUNCTION = "factlane_contract_v3_writer"
_LEGACY_WRITER_FUNCTION = "factlane_contract_v2_writer"
_RECOVERY_WRITER_FUNCTION = "factlane_recovery_v3_writer"
_CASEFOLD_FUNCTION = "factlane_unicode_casefold"
_LEGACY_ORIGIN_JSON = canonical_json({"contributor_class": "LEGACY_UNKNOWN", "contributor_ref": None})
_MAINTENANCE_CAPABILITY_SECRET = object()
_SENSITIVE_RECOVERY_INTERLOCK_KEY = "sensitive_recovery_interlock"
_CURRENT_AUTHORITY_BY_SCOPE = {
    "GLOBAL_USER": "OWNER_CURRENT",
    "PROJECT": "PROJECT_CURRENT",
    "WORKFLOW": "WORKFLOW_CURRENT",
    "CROSS_PROJECT_WORKFLOW": "WORKFLOW_CURRENT",
    "TOOL_ENVIRONMENT": "TOOL_ENV_CURRENT",
}
_CURRENT_VERIFICATIONS = frozenset({"OWNER", "CURRENT_REPO_CHECK", "AUTOMATED_CHECK"})
_CROSS_PROJECT_MEMORY_TYPES = frozenset({"WORKFLOW_RULE", "DECISION_RATIONALE"})
_MIN_BUSY_TIMEOUT_MS = 5000
_SQLITE_FULL_CODE = getattr(sqlite3, "SQLITE_FULL", 13)
MAX_CONTRADICTION_GROUP_MEMBERS = 32
MAX_GROUP_CAS_BYTES = 12_288
_GOVERNANCE_ACTIVE_STATES = frozenset({"VALIDATED_CURRENT", "CANDIDATE", "STALE", "QUARANTINED"})
_ADVERSE_FRESHNESS_GATES = {
    "CHANGED": "SOURCE_CHANGE_REVIEW_REQUIRED",
    "UNAVAILABLE": "RECHECK_UNAVAILABLE",
    "INDETERMINATE": "RECHECK_INDETERMINATE",
}
MAX_TRUST_RENEWAL_EVIDENCE_AGE_SECONDS = 86_400
MAX_FUTURE_EVIDENCE_SKEW_SECONDS = 300
MAX_LOGICAL_EXPORT_RECORDS = 1000
MAX_PERMANENT_PURGE_TARGETS = 50
LOGICAL_EXPORT_LIFECYCLE_STATES = frozenset(
    {
        "CANDIDATE",
        "VALIDATED_CURRENT",
        "SUPERSEDED",
        "STALE",
        "QUARANTINED",
        "HISTORICAL",
        "REVOKED",
        "REJECTED",
    }
)
_PURGE_PM0 = "PM0_ADAPTER_ONLY_EXPECTED"
_PURGE_PM1 = "PM1_FULL_NATIVE_MATERIALIZATION"

_LIFECYCLE_EVENT_COLUMNS = (
    "event_id", "operation_id", "memory_id", "record_id", "target_record_id", "target_revision",
    "action", "prior_lifecycle_state", "result_lifecycle_state", "prior_contradiction_state",
    "result_contradiction_state", "reason_code", "actor_class", "actor_ref_digest", "grant_ref_digest",
    "occurred_at", "expected_revision", "result_revision", "related_record_id", "related_memory_id",
    "freshness_outcome", "observation_origin", "observed_at", "accepted_at", "evidence_ref_digest",
    "freshness_generation", "clock_anomaly",
)

_LIFECYCLE_OPERATION_COLUMNS = (
    "idempotency_key", "request_fingerprint", "operation_id", "request_id", "operation_kind", "actor_class",
    "actor_ref_digest", "grant_ref_digest", "scope_digest", "target_memory_id", "target_record_id",
    "winner_memory_id", "winner_record_id", "winner_parent_record_id", "result_record_id", "result_revision",
    "result_lifecycle_state", "result_contradiction_state", "result_outcome", "observed_at",
    "affected_members_json", "event_ids_json",
    "result_group_token", "result_group_count", "replay_status", "commit_timestamp", "decision_error",
    "decision_at", "accepted_event_freshness_generation", "resulting_member_freshness_generation",
    "recovery_event_id",
)


def _sqlite_pragma_compatibility_error() -> AdapterError:
    return AdapterError(
        "BACKEND_COMPATIBILITY_MISMATCH",
        "backend SQLite pragma configuration conflicts with FactLane requirements",
    )


def _assert_required_sqlite_pragma_environment() -> None:
    """Reject backend env overrides that weaken FactLane's SQLite posture."""
    raw_pragmas = os.environ.get("MCP_MEMORY_SQLITE_PRAGMAS", "")
    if not raw_pragmas:
        return

    for pragma_pair in raw_pragmas.split(","):
        if "=" not in pragma_pair:
            continue
        raw_name, raw_value = pragma_pair.split("=", 1)
        name = raw_name.strip().lower().rsplit(".", 1)[-1]
        value = raw_value.strip().lower()
        if name == "journal_mode" and value != "wal":
            raise _sqlite_pragma_compatibility_error()
        if name == "synchronous":
            synchronous_levels = {
                "off": 0,
                "0": 0,
                "normal": 1,
                "1": 1,
                "full": 2,
                "2": 2,
                "extra": 3,
                "3": 3,
            }
            if synchronous_levels.get(value, -1) < 1:
                raise _sqlite_pragma_compatibility_error()
        if name == "busy_timeout":
            try:
                busy_timeout_ms = int(value)
            except ValueError as exc:
                raise _sqlite_pragma_compatibility_error() from exc
            if busy_timeout_ms < _MIN_BUSY_TIMEOUT_MS:
                raise _sqlite_pragma_compatibility_error()


def _assert_required_sqlite_pragmas(conn: sqlite3.Connection) -> None:
    """Verify the pinned backend actually established FactLane's required pragmas."""
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    synchronous = int(conn.execute("PRAGMA synchronous").fetchone()[0])
    busy_timeout_ms = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    if journal_mode != "wal" or synchronous < 1 or busy_timeout_ms < _MIN_BUSY_TIMEOUT_MS:
        raise _sqlite_pragma_compatibility_error()


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _normalize_schema_sql(value: str | None) -> str:
    if value is None:
        return ""
    compact = "".join(str(value).lower().split())
    return compact.replace("ifnotexists", "")


def _index_signature(conn: sqlite3.Connection, table: str, name: str) -> tuple[bool, bool, tuple[tuple[str, bool], ...]] | None:
    row = next((item for item in conn.execute(f"PRAGMA index_list({table})") if str(item[1]) == name), None)
    if row is None:
        return None
    unique = bool(row[2])
    partial = bool(row[4]) if len(row) > 4 else False
    columns = tuple(
        (str(item[2]), bool(item[3]))
        for item in conn.execute(f"PRAGMA index_xinfo({name})")
        if int(item[5]) == 1 and item[2] is not None
    )
    return unique, partial, columns


def _expected_v3_trigger_sql() -> dict[str, str]:
    expected: dict[str, str] = {}
    for name, action in (
        ("factlane_v3_adapter_insert_fence", "INSERT"),
        ("factlane_v3_adapter_update_fence", "UPDATE"),
        ("factlane_v3_adapter_delete_fence", "DELETE"),
    ):
        expected[name] = (
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
            expected[name] = (
                f"CREATE TRIGGER {name} BEFORE {action} ON {table} WHEN {predicate} "
                f"BEGIN SELECT RAISE(ABORT, '{error}'); END"
            )
    expected["factlane_v3_adapter_global_idempotency"] = (
        "CREATE TRIGGER factlane_v3_adapter_global_idempotency BEFORE INSERT ON adapter_records "
        "WHEN EXISTS (SELECT 1 FROM lifecycle_operations WHERE idempotency_key = NEW.idempotency_key) "
        "BEGIN SELECT RAISE(ABORT, 'FACTLANE_GLOBAL_IDEMPOTENCY_CONFLICT'); END"
    )
    expected["factlane_v3_operation_global_idempotency"] = (
        "CREATE TRIGGER factlane_v3_operation_global_idempotency BEFORE INSERT ON lifecycle_operations "
        "WHEN EXISTS (SELECT 1 FROM adapter_records WHERE idempotency_key = NEW.idempotency_key) "
        "BEGIN SELECT RAISE(ABORT, 'FACTLANE_GLOBAL_IDEMPOTENCY_CONFLICT'); END"
    )
    return expected


def _expected_v3_table_sql() -> dict[str, str]:
    return {
        "adapter_records": """
            CREATE TABLE adapter_records (
                record_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                parent_record_id TEXT,
                scope TEXT NOT NULL,
                project_id TEXT,
                worktree_id TEXT,
                workflow_id TEXT,
                agent_id TEXT,
                memory_type TEXT NOT NULL,
                fact TEXT NOT NULL,
                source_provenance TEXT NOT NULL,
                source_timestamp TEXT,
                created_at TEXT NOT NULL,
                last_verified_at TEXT,
                verified_by TEXT NOT NULL,
                authority_role TEXT NOT NULL,
                contribution_origin TEXT,
                freshness_policy TEXT NOT NULL,
                supersedes TEXT NOT NULL,
                contradiction_key TEXT NOT NULL,
                contradiction_state TEXT NOT NULL,
                confidence REAL NOT NULL,
                tags TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                native_content_hash TEXT NOT NULL UNIQUE,
                payload_fingerprint TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                embedding_profile_id TEXT NOT NULL,
                embedding_model_digest TEXT NOT NULL,
                embedding_output_dimension INTEGER NOT NULL,
                CHECK (confidence >= 0.0 AND confidence <= 1.0)
            )
        """,
        "lifecycle_events": """
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
        """,
        "lifecycle_operations": """
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
        """,
    }


def _validate_existing_v3_schema(conn: sqlite3.Connection) -> None:
    """Validate FactLane-owned v3 schema without repairing it during ordinary runtime open."""
    required_tables = {"adapter_meta", "adapter_records", "lifecycle_events", "lifecycle_operations"}
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)",
            tuple(sorted(required_tables)),
        ).fetchall()
    }
    if present != required_tables:
        raise AdapterError("SCHEMA_MISMATCH", "storage v3 is incomplete; migration/operator reconciliation is required")
    adapter_columns = set(_table_columns(conn, "adapter_records"))
    required_adapter = set(_RECORD_COLUMNS) if "_RECORD_COLUMNS" in globals() else {
        "record_id", "memory_id", "revision", "parent_record_id", "scope", "project_id", "worktree_id",
        "workflow_id", "agent_id", "memory_type", "fact", "source_provenance", "source_timestamp",
        "created_at", "last_verified_at", "verified_by", "authority_role", "contribution_origin",
        "freshness_policy", "supersedes", "contradiction_key", "contradiction_state", "confidence", "tags",
        "lifecycle_state", "native_content_hash", "payload_fingerprint", "idempotency_key", "embedding_profile_id",
        "embedding_model_digest", "embedding_output_dimension",
    }
    if required_adapter != adapter_columns:
        raise AdapterError("SCHEMA_MISMATCH", "storage v3 adapter_records schema is not exact")
    if _table_columns(conn, "lifecycle_events") != _LIFECYCLE_EVENT_COLUMNS:
        raise AdapterError("SCHEMA_MISMATCH", "storage v3 lifecycle_events schema is not exact")
    if _table_columns(conn, "lifecycle_operations") != _LIFECYCLE_OPERATION_COLUMNS:
        raise AdapterError("SCHEMA_MISMATCH", "storage v3 lifecycle_operations schema is not exact")
    expected_tables = _expected_v3_table_sql()
    for table, expected_sql in expected_tables.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row is None or _normalize_schema_sql(row[0]) != _normalize_schema_sql(expected_sql):
            raise AdapterError("SCHEMA_MISMATCH", f"storage v3 table {table} definition is not exact")
    required_indexes = {
        ("adapter_records", "idx_adapter_scope_current"): (False, False, (("scope", False), ("project_id", False), ("worktree_id", False), ("workflow_id", False), ("agent_id", False), ("lifecycle_state", False))),
        ("adapter_records", "idx_adapter_memory_id"): (False, False, (("memory_id", False), ("revision", False))),
        ("adapter_records", "idx_adapter_contradiction"): (False, False, (("contradiction_key", False), ("lifecycle_state", False))),
        ("adapter_records", "uq_adapter_memory_revision"): (True, False, (("memory_id", False), ("revision", False))),
        ("adapter_records", "uq_adapter_parent_record"): (True, True, (("parent_record_id", False),)),
        ("lifecycle_events", "uq_lifecycle_event_target_generation"): (True, True, (("target_record_id", False), ("target_revision", False), ("freshness_generation", False))),
        ("lifecycle_events", "idx_lifecycle_event_target_generation"): (False, True, (("target_record_id", False), ("target_revision", False), ("freshness_generation", True))),
        ("lifecycle_events", "idx_lifecycle_event_memory"): (False, False, (("memory_id", False), ("occurred_at", False), ("event_id", False))),
        ("lifecycle_operations", "idx_lifecycle_operation_target"): (False, False, (("target_memory_id", False), ("target_record_id", False))),
    }
    for (table, name), expected in required_indexes.items():
        if _index_signature(conn, table, name) != expected:
            raise AdapterError("SCHEMA_MISMATCH", f"storage v3 index {name} is not exact")
    parent_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_adapter_parent_record'").fetchone()
    generation_unique_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_lifecycle_event_target_generation'").fetchone()
    generation_lookup_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_lifecycle_event_target_generation'").fetchone()
    if not parent_sql or "whereparent_record_idisnotnull" not in _normalize_schema_sql(parent_sql[0]):
        raise AdapterError("SCHEMA_MISMATCH", "storage v3 parent lineage guard predicate is not exact")
    for row, name in (
        (generation_unique_sql, "uq_lifecycle_event_target_generation"),
        (generation_lookup_sql, "idx_lifecycle_event_target_generation"),
    ):
        if not row or "wherefreshness_generationisnotnull" not in _normalize_schema_sql(row[0]):
            raise AdapterError("SCHEMA_MISMATCH", f"storage v3 index {name} predicate is not exact")

    expected_triggers = _expected_v3_trigger_sql()
    actual_triggers = {
        str(name): str(sql)
        for name, sql in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'factlane_v3_%'"
        ).fetchall()
    }
    if set(actual_triggers) != set(expected_triggers):
        raise AdapterError("SCHEMA_MISMATCH", "storage v3 governance trigger set is not exact")
    for name, expected_sql in expected_triggers.items():
        if _normalize_schema_sql(actual_triggers[name]) != _normalize_schema_sql(expected_sql):
            raise AdapterError("SCHEMA_MISMATCH", f"storage v3 trigger {name} is not exact")
    clock_rows = conn.execute(
        "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
    ).fetchall()
    if len(clock_rows) != 1:
        raise AdapterError("SCHEMA_MISMATCH", "storage v3 trust-clock high-water is missing")
    try:
        parse_iso(clock_rows[0][0], required=True)
    except (ValueError, TypeError, AdapterError) as exc:
        raise AdapterError("SCHEMA_MISMATCH", "storage v3 trust-clock high-water is malformed") from exc


def inspect_storage_contract_version(db_path: str) -> int | None:
    """Read FactLane contract metadata from a disposable byte copy, never the live DB.

    Copying the main/WAL/SHM trio before SQLite inspection keeps ordinary v0.1.7
    launch from creating/checkpointing sidecars or otherwise mutating a v2 database.
    """
    canonical = os.path.realpath(os.path.abspath(db_path))
    if not os.path.exists(canonical) or os.path.getsize(canonical) == 0:
        return None
    with tempfile.TemporaryDirectory(prefix="factlane-contract-preflight-") as td:
        copied = os.path.join(td, "factlane.db")
        shutil.copy2(canonical, copied)
        for suffix in ("-wal", "-shm"):
            source = canonical + suffix
            if os.path.exists(source):
                shutil.copy2(source, copied + suffix)
        conn: sqlite3.Connection | None = None
        try:
            uri = "file:" + urllib.parse.quote(copied, safe="/") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            has_meta = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adapter_meta'"
            ).fetchone()
            if has_meta is None:
                raise AdapterError("SCHEMA_MISMATCH", "existing database has no FactLane storage contract metadata")
            row = conn.execute("SELECT value FROM adapter_meta WHERE key='contract_version'").fetchone()
            if row is None:
                raise AdapterError("SCHEMA_MISMATCH", "existing database has no FactLane storage contract version")
            try:
                version = int(row[0])
            except (TypeError, ValueError) as exc:
                raise AdapterError("SCHEMA_MISMATCH", "storage contract version is invalid") from exc
            if version == STORAGE_CONTRACT_VERSION:
                _validate_existing_v3_schema(conn)
            return version
        except sqlite3.Error as exc:
            raise AdapterError("SCHEMA_MISMATCH", "existing database contract metadata could not be inspected") from exc
        finally:
            if conn is not None:
                conn.close()


def _validate_current_authority_metadata(record: dict[str, Any]) -> None:
    """Fail closed if a direct storage caller fabricates current authority."""
    if record.get("lifecycle_state") != "VALIDATED_CURRENT":
        return
    scope = validate_scope(
        record.get("scope"),
        record.get("project_id"),
        record.get("worktree_id"),
        record.get("workflow_id"),
        record.get("agent_id"),
    )
    expected_authority = _CURRENT_AUTHORITY_BY_SCOPE[scope.scope]
    if record.get("authority_role") != expected_authority:
        raise AdapterError(
            "INVALID_ENVELOPE",
            "validated current authority_role does not match exact scope",
        )
    if record.get("verified_by") not in _CURRENT_VERIFICATIONS:
        raise AdapterError(
            "INVALID_ENVELOPE",
            "validated current requires an admissible verification basis",
        )
    parse_iso(record.get("source_timestamp"), required=True)
    parse_iso(record.get("last_verified_at"), required=True)


def _validate_current_contradiction_state(record: dict[str, Any]) -> None:
    """Fail closed on impossible validated-current contradiction state."""
    if record.get("lifecycle_state") != "VALIDATED_CURRENT":
        return
    if record.get("contradiction_state") not in {"NONE", "RESOLVED"}:
        raise AdapterError(
            "INVALID_ENVELOPE",
            "validated current contradiction_state must be NONE or RESOLVED",
        )


def _validate_cross_project_storage_semantics(record: dict[str, Any]) -> None:
    """Keep direct storage admission aligned with CROSS_PROJECT_WORKFLOW semantics."""
    if record.get("scope") != "CROSS_PROJECT_WORKFLOW":
        return
    if record.get("memory_type") not in _CROSS_PROJECT_MEMORY_TYPES:
        raise AdapterError("SCOPE_TYPE_MISMATCH", "memory_type is incompatible with CROSS_PROJECT_WORKFLOW")

    provenance = record.get("source_provenance")
    if isinstance(provenance, dict) and "source_fingerprint" in provenance:
        raise AdapterError(
            "SCOPE_FRESHNESS_MISMATCH",
            "CROSS_PROJECT_WORKFLOW uses freshness_policy.source_fingerprint as the sole fingerprint authority",
        )
    freshness = validate_freshness(record.get("freshness_policy"))
    kind = freshness["kind"]
    if kind not in {"on_change", "manual"}:
        raise AdapterError("INVALID_FRESHNESS", "CROSS_PROJECT_WORKFLOW freshness must be on_change or manual")
    if kind == "on_change":
        recheck_ref = freshness.get("recheck_ref")
        fingerprint = freshness.get("source_fingerprint")
        source_hash = provenance.get("source_hash") if isinstance(provenance, dict) else None
        if not isinstance(recheck_ref, str) or not recheck_ref.strip() or fingerprint != source_hash:
            raise AdapterError(
                "INVALID_FRESHNESS",
                "on_change freshness_policy requires a recheck_ref and source_fingerprint matching source_provenance.source_hash",
            )
    elif freshness.get("recheck_ref") is not None or freshness.get("source_fingerprint") is not None:
        raise AdapterError(
            "INVALID_FRESHNESS",
            "manual CROSS_PROJECT_WORKFLOW freshness cannot carry recheck_ref or source_fingerprint",
        )


class _MaintenanceCapability:
    __slots__ = ("db_path", "db_identity", "handle", "active", "_secret")

    def __init__(self, db_path: str, handle: Any, secret: object) -> None:
        self.db_path = os.path.realpath(os.path.abspath(db_path))
        stat = os.fstat(handle.fileno())
        self.db_identity = (int(stat.st_dev), int(stat.st_ino))
        self.handle = handle
        self.active = True
        self._secret = secret


def _make_maintenance_capability(db_path: str, handle: Any) -> _MaintenanceCapability:
    """Create the internal capability proving recovery still owns its lease handle."""
    return _MaintenanceCapability(db_path, handle, _MAINTENANCE_CAPABILITY_SECRET)


def _validate_maintenance_capability(db_path: str, capability: _MaintenanceCapability) -> None:
    canonical = os.path.realpath(os.path.abspath(db_path))
    if (
        not isinstance(capability, _MaintenanceCapability)
        or capability._secret is not _MAINTENANCE_CAPABILITY_SECRET
        or not capability.active
        or capability.handle is None
        or capability.handle.closed
    ):
        raise AdapterError(
            "WRITE_AUTHORIZATION_DENIED",
            "maintenance engine requires an active recovery lease for the exact database",
        )
    try:
        path_stat = os.stat(canonical)
        path_identity = (int(path_stat.st_dev), int(path_stat.st_ino))
        handle_stat = os.fstat(capability.handle.fileno())
        handle_identity = (int(handle_stat.st_dev), int(handle_stat.st_ino))
    except (OSError, ValueError):
        path_identity = None
        handle_identity = None
    if (
        capability.db_path != canonical
        or capability.db_identity != path_identity
        or capability.db_identity != handle_identity
    ):
        raise AdapterError(
            "WRITE_AUTHORIZATION_DENIED",
            "maintenance engine requires an active recovery lease for the exact database",
        )


def _same_inode(path: str, handle: Any) -> bool:
    try:
        path_stat = os.stat(path)
        handle_stat = os.fstat(handle.fileno())
    except OSError:
        return False
    return (int(path_stat.st_dev), int(path_stat.st_ino)) == (
        int(handle_stat.st_dev),
        int(handle_stat.st_ino),
    )


def _open_database_lease_handle(db_path: str, *, create: bool) -> tuple[Any, bool]:
    """Open the database inode itself as the maintenance coordination object."""
    canonical = os.path.realpath(os.path.abspath(db_path))
    parent = os.path.dirname(canonical)
    if create:
        os.makedirs(parent, exist_ok=True)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    created = False
    fd: int | None = None
    try:
        if create:
            try:
                fd = os.open(canonical, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                fd = os.open(canonical, flags)
        else:
            fd = os.open(canonical, flags)
        try:
            return os.fdopen(fd, "r+b", buffering=0), created
        except BaseException:
            os.close(fd)
            raise
    except OSError:
        raise


def _cleanup_new_lease_anchor(db_path: str, handle: Any, *, created: bool) -> None:
    if not created:
        return
    canonical = os.path.realpath(os.path.abspath(db_path))
    try:
        if _same_inode(canonical, handle) and os.fstat(handle.fileno()).st_size == 0:
            os.unlink(canonical)
    except OSError:
        pass


def _acquire_runtime_maintenance_lease(db_path: str) -> Any:
    """Hold a shared lease while a normal runtime engine is attached to one DB."""
    try:
        import fcntl
    except ImportError:
        # Sensitive-memory recovery is POSIX-only today as well. Do not make the
        # ordinary storage module unimportable on platforms without fcntl.
        return None

    try:
        handle, created = _open_database_lease_handle(db_path, create=True)
    except OSError as exc:
        raise AdapterError(
            "BACKEND_UNAVAILABLE",
            "database maintenance coordination is unavailable",
        ) from exc

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        _cleanup_new_lease_anchor(db_path, handle, created=created)
        handle.close()
        raise AdapterError(
            "MAINTENANCE_IN_PROGRESS",
            "database maintenance is in progress; retry after maintenance completes",
        ) from exc
    except OSError as exc:
        _cleanup_new_lease_anchor(db_path, handle, created=created)
        handle.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise AdapterError(
                "MAINTENANCE_IN_PROGRESS",
                "database maintenance is in progress; retry after maintenance completes",
            ) from exc
        raise AdapterError(
            "BACKEND_UNAVAILABLE",
            "database maintenance coordination is unavailable",
        ) from exc
    if not _same_inode(db_path, handle):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        raise AdapterError(
            "BACKEND_UNAVAILABLE",
            "database path changed during maintenance coordination",
        )
    return handle


def _release_runtime_maintenance_lease(handle: Any) -> None:
    if handle is None:
        return
    try:
        import fcntl
    except ImportError:
        handle.close()
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _assert_sensitive_recovery_interlock_clear(db_path: str) -> None:
    """Block ordinary service while a committed sensitive recovery is incomplete."""
    if not os.path.isfile(db_path) or os.path.getsize(db_path) == 0:
        return
    conn: sqlite3.Connection | None = None
    try:
        ro_uri = "file:" + urllib.parse.quote(os.path.abspath(db_path), safe="/") + "?mode=ro"
        conn = sqlite3.connect(ro_uri, uri=True, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT value FROM adapter_meta WHERE key = ?",
                (_SENSITIVE_RECOVERY_INTERLOCK_KEY,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return
            raise AdapterError(
                "BACKEND_UNAVAILABLE",
                "database recovery interlock could not be checked",
            ) from exc
        if row is not None:
            raise AdapterError(
                "MAINTENANCE_IN_PROGRESS",
                "sensitive-memory recovery is incomplete; operator reconciliation is required",
            )
    finally:
        if conn is not None:
            conn.close()


class RecordUniquenessConflict(RuntimeError):
    """Internal signal for an adapter-record UNIQUE race that must be reconciled above storage."""

    def __init__(self, constraint: str) -> None:
        super().__init__("adapter record uniqueness conflict")
        self.constraint = constraint


def register_storage_v3_writer(conn: sqlite3.Connection) -> None:
    """Mark one trusted FactLane connection as a governed storage-v3 writer."""
    conn.create_function(_WRITER_FUNCTION, 0, lambda: 1)
    # Legacy v0.1.3 trigger bodies may still name the old writer function while
    # the database is being inspected. Resolve that function fail-closed so a
    # no-op schema normalization can open the DB without granting legacy write
    # authority before compatibility is proven and the fences are replaced.
    conn.create_function(_LEGACY_WRITER_FUNCTION, 0, lambda: 0)
    conn.create_function(_RECOVERY_WRITER_FUNCTION, 0, lambda: 0)
    conn.create_function(_CASEFOLD_FUNCTION, 1, lambda value: str(value).casefold(), deterministic=True)


def register_storage_v2_writer(conn: sqlite3.Connection) -> None:
    """Compatibility alias retained for private recovery/test callers while v3 lands."""
    register_storage_v3_writer(conn)


def register_storage_legacy_maintenance_writer(conn: sqlite3.Connection) -> None:
    """Permit a maintenance-only connection to operate on pre-compatibility v2 trigger fences."""
    conn.create_function(_LEGACY_WRITER_FUNCTION, 0, lambda: 1)
    conn.create_function(_WRITER_FUNCTION, 0, lambda: 0)
    conn.create_function(_RECOVERY_WRITER_FUNCTION, 0, lambda: 0)
    conn.create_function(_CASEFOLD_FUNCTION, 1, lambda value: str(value).casefold(), deterministic=True)


def register_storage_recovery_writer(conn: sqlite3.Connection) -> None:
    """Enable the exact privileged writer functions required by sensitive Recovery v3."""
    conn.create_function(_WRITER_FUNCTION, 0, lambda: 1)
    conn.create_function(_LEGACY_WRITER_FUNCTION, 0, lambda: 1)
    conn.create_function(_RECOVERY_WRITER_FUNCTION, 0, lambda: 1)
    conn.create_function(_CASEFOLD_FUNCTION, 1, lambda value: str(value).casefold(), deterministic=True)


def assert_supported_sqlite_runtime(version_info: tuple[int, int, int] | None = None) -> None:
    """Fail closed when the linked SQLite runtime cannot satisfy FactLane's storage contract."""
    actual = tuple(version_info or sqlite3.sqlite_version_info)
    if actual < MIN_SQLITE_VERSION:
        required = ".".join(str(value) for value in MIN_SQLITE_VERSION)
        detected = ".".join(str(value) for value in actual)
        raise AdapterError(
            "BACKEND_COMPATIBILITY_MISMATCH",
            f"FactLane requires SQLite >= {required}; linked runtime is {detected}",
        )


_RECORD_COLUMNS = (
    "record_id",
    "memory_id",
    "revision",
    "parent_record_id",
    "scope",
    "project_id",
    "worktree_id",
    "workflow_id",
    "agent_id",
    "memory_type",
    "fact",
    "source_provenance",
    "source_timestamp",
    "created_at",
    "last_verified_at",
    "verified_by",
    "authority_role",
    "contribution_origin",
    "freshness_policy",
    "supersedes",
    "contradiction_key",
    "contradiction_state",
    "confidence",
    "tags",
    "lifecycle_state",
    "native_content_hash",
    "payload_fingerprint",
    "idempotency_key",
    "embedding_profile_id",
    "embedding_model_digest",
    "embedding_output_dimension",
)


class SQLiteVecEngine:
    """Small adapter-owned repository over the pinned SQLite-vec schema."""

    def __init__(self, db_path: str, profile: EmbeddingProfile) -> None:
        # Resolve aliases once so the lease and backend always open the same path.
        self.db_path = os.path.realpath(os.path.abspath(db_path))
        self.profile = profile
        self.storage: Any = None
        self.conn: sqlite3.Connection | None = None
        self.native_columns: set[str] = set()
        self.vector_dimension: int | None = None
        self.embedding_store_key = profile.profile_id
        # Fresh, unbound engines are provisionally usable by internal storage tests and
        # maintenance setup. Existing materialized databases are downgraded to UNPROVEN
        # during metadata inspection until the adapter establishes runtime compatibility.
        self.embedding_compatibility_state = "COMPATIBLE"
        self.embedding_compatibility_reason: str | None = None
        self.embedding_profile_metadata: dict[str, object] | None = None
        self.embedding_profile_metadata_invalid = False
        self._closed = False
        self._maintenance_lease_handle: Any = None
        self._maintenance_capability: _MaintenanceCapability | None = None

    @classmethod
    def _for_maintenance(
        cls,
        db_path: str,
        profile: EmbeddingProfile,
        capability: _MaintenanceCapability,
    ) -> "SQLiteVecEngine":
        """Create the operator-owned engine used while recovery holds the exclusive lease."""
        _validate_maintenance_capability(db_path, capability)
        engine = cls(db_path, profile)
        engine._maintenance_capability = capability
        return engine

    async def open(self) -> None:
        if self._closed:
            raise AdapterError("BACKEND_UNAVAILABLE", "backend engine is already closed")
        if self.storage is not None or self.conn is not None or self._maintenance_lease_handle is not None:
            raise AdapterError("BACKEND_UNAVAILABLE", "backend engine is already open")
        if os.environ.get("MCP_EXTERNAL_EMBEDDING_URL", "").strip():
            raise AdapterError("ADMIN_OPERATION_DENIED", "external embedding providers are disabled")
        _assert_required_sqlite_pragma_environment()
        assert_supported_sqlite_runtime()
        existing_contract = inspect_storage_contract_version(self.db_path)
        if existing_contract == 2:
            raise AdapterError(
                "MIGRATION_REQUIRED",
                "storage contract v2 requires the explicit migrate-storage-v3 operator before v0.1.7 runtime can open it",
            )
        if existing_contract is not None and existing_contract != STORAGE_CONTRACT_VERSION:
            raise AdapterError("SCHEMA_MISMATCH", "storage contract version is unsupported by v0.1.7")
        if self._maintenance_capability is None:
            self._maintenance_lease_handle = _acquire_runtime_maintenance_lease(self.db_path)
        else:
            _validate_maintenance_capability(self.db_path, self._maintenance_capability)
        try:
            if self._maintenance_capability is None:
                _assert_sensitive_recovery_interlock_clear(self.db_path)
            # Do not write MCP_* process environment here. The pinned backend
            # caches environment-backed config at import time; FactLane binds
            # the storage class and the behavior it uses explicitly below.
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            SqliteVecMemoryStorage = load_pinned_sqlite_vec_storage()
            storage = SqliteVecMemoryStorage(
                self.db_path,
                embedding_model=self.profile.base_model_identity,
            )
            self.storage = storage
            # Native startup receives the profile dimension; adapter code binds
            # the real provider before any memory operation.
            storage.embedding_dimension = self.profile.output_dimension
            storage.semantic_dedup_enabled = False

            async def defer_native_embedding() -> None:
                # The adapter owns embedding calls; native startup must not create
                # hash vectors or attempt a model download before provider binding.
                storage.embedding_model = None
                storage.embedding_dimension = self.profile.output_dimension
                storage.embedding_backend_degraded = False

            bind_deferred_embedding_initializer(storage, defer_native_embedding)
            await storage.initialize(strict_dimension_check=False)
            self.conn = storage.conn
            if self.conn is None:
                raise AdapterError("BACKEND_UNAVAILABLE", "SQLite-vec backend did not expose a connection")
            _assert_required_sqlite_pragmas(self.conn)
            register_storage_v3_writer(self.conn)
            self.vector_dimension = await self._run(self._read_dimension)
            self.native_columns = await self._run(self._read_native_columns)
            required = {"content_hash", "content", "metadata", "created_at", "updated_at"}
            if not required.issubset(self.native_columns):
                await self.close()
                raise AdapterError("SCHEMA_MISMATCH", "pinned backend schema is missing required columns")
            await self._run(self._create_adapter_schema)
            await self._run(self._inspect_profile_metadata)
        except asyncio.CancelledError:
            await self.close()
            raise
        except AdapterError:
            await self.close()
            raise
        except sqlite3.Error as exc:
            await self.close()
            raise AdapterError("BACKEND_CORRUPT", "SQLite-vec database could not be opened") from exc
        except Exception as exc:
            await self.close()
            raise AdapterError("BACKEND_UNAVAILABLE", "SQLite-vec backend could not be initialized") from exc

    async def _run(self, operation: Callable[..., Any], *args: Any) -> Any:
        if self.conn is None or self.storage is None:
            raise AdapterError("BACKEND_UNAVAILABLE", "backend is not open")

        try:
            return await execute_with_backend_retry(self.storage, lambda: operation(*args))
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise AdapterError("BACKEND_BUSY", "backend remained busy after bounded retry") from exc
            error_code = getattr(exc, "sqlite_errorcode", None)
            base_error_code = error_code & 0xFF if isinstance(error_code, int) else None
            if base_error_code == _SQLITE_FULL_CODE:
                raise AdapterError(
                    "BACKEND_UNAVAILABLE",
                    "backend storage is temporarily unavailable",
                ) from exc
            raise

    def _read_dimension(self) -> int | None:
        assert self.conn is not None
        row = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='memory_embeddings'").fetchone()
        if not row or not row[0]:
            return None
        import re

        match = re.search(r"FLOAT\[(\d+)\]", row[0])
        return int(match.group(1)) if match else None

    def _read_native_columns(self) -> set[str]:
        assert self.conn is not None
        return {row[1] for row in self.conn.execute("PRAGMA table_info(memories)").fetchall()}

    def _migration_checkpoint(self, name: str) -> None:
        """Private no-op seam used to prove transactional migration rollback."""
        return None

    def _create_adapter_schema(self) -> None:
        assert self.conn is not None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS adapter_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            version_row = self.conn.execute("SELECT value FROM adapter_meta WHERE key='contract_version'").fetchone()
            if version_row is not None:
                try:
                    existing_version = int(version_row[0])
                except (TypeError, ValueError) as exc:
                    raise AdapterError("SCHEMA_MISMATCH", "storage contract version is invalid") from exc
                if existing_version != STORAGE_CONTRACT_VERSION:
                    raise AdapterError("SCHEMA_MISMATCH", "ordinary runtime cannot migrate an older storage contract")
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS adapter_records (
                    record_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    parent_record_id TEXT,
                    scope TEXT NOT NULL,
                    project_id TEXT,
                    worktree_id TEXT,
                    workflow_id TEXT,
                    agent_id TEXT,
                    memory_type TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    source_provenance TEXT NOT NULL,
                    source_timestamp TEXT,
                    created_at TEXT NOT NULL,
                    last_verified_at TEXT,
                    verified_by TEXT NOT NULL,
                    authority_role TEXT NOT NULL,
                    contribution_origin TEXT,
                    freshness_policy TEXT NOT NULL,
                    supersedes TEXT NOT NULL,
                    contradiction_key TEXT NOT NULL,
                    contradiction_state TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    tags TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    native_content_hash TEXT NOT NULL UNIQUE,
                    payload_fingerprint TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    embedding_profile_id TEXT NOT NULL,
                    embedding_model_digest TEXT NOT NULL,
                    embedding_output_dimension INTEGER NOT NULL,
                    CHECK (confidence >= 0.0 AND confidence <= 1.0)
                )
                """
            )
            if set(_table_columns(self.conn, "adapter_records")) != set(_RECORD_COLUMNS):
                raise AdapterError("SCHEMA_MISMATCH", "storage v3 adapter_records schema is not exact")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_adapter_scope_current "
                "ON adapter_records(scope, project_id, worktree_id, workflow_id, agent_id, lifecycle_state)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_adapter_memory_id ON adapter_records(memory_id, revision)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_adapter_contradiction ON adapter_records(contradiction_key, lifecycle_state)"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_adapter_memory_revision ON adapter_records(memory_id, revision)"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_adapter_parent_record ON adapter_records(parent_record_id) "
                "WHERE parent_record_id IS NOT NULL"
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_events (
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
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_operations (
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
            if _table_columns(self.conn, "lifecycle_events") != _LIFECYCLE_EVENT_COLUMNS:
                raise AdapterError("SCHEMA_MISMATCH", "storage v3 lifecycle_events schema is not exact")
            if _table_columns(self.conn, "lifecycle_operations") != _LIFECYCLE_OPERATION_COLUMNS:
                raise AdapterError("SCHEMA_MISMATCH", "storage v3 lifecycle_operations schema is not exact")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_lifecycle_event_target_generation "
                "ON lifecycle_events(target_record_id, target_revision, freshness_generation) "
                "WHERE freshness_generation IS NOT NULL"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_event_target_generation "
                "ON lifecycle_events(target_record_id, target_revision, freshness_generation DESC) "
                "WHERE freshness_generation IS NOT NULL"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_event_memory ON lifecycle_events(memory_id, occurred_at, event_id)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_operation_target "
                "ON lifecycle_operations(target_memory_id, target_record_id)"
            )
            for name, action in (
                ("factlane_v3_adapter_insert_fence", "INSERT"),
                ("factlane_v3_adapter_update_fence", "UPDATE"),
                ("factlane_v3_adapter_delete_fence", "DELETE"),
            ):
                self.conn.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} ON adapter_records "
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
                    self.conn.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} ON {table} "
                        f"WHEN {predicate} BEGIN SELECT RAISE(ABORT, '{error}'); END"
                    )
            self.conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS factlane_v3_adapter_global_idempotency
                BEFORE INSERT ON adapter_records
                WHEN EXISTS (SELECT 1 FROM lifecycle_operations WHERE idempotency_key = NEW.idempotency_key)
                BEGIN SELECT RAISE(ABORT, 'FACTLANE_GLOBAL_IDEMPOTENCY_CONFLICT'); END
                """
            )
            self.conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS factlane_v3_operation_global_idempotency
                BEFORE INSERT ON lifecycle_operations
                WHEN EXISTS (SELECT 1 FROM adapter_records WHERE idempotency_key = NEW.idempotency_key)
                BEGIN SELECT RAISE(ABORT, 'FACTLANE_GLOBAL_IDEMPOTENCY_CONFLICT'); END
                """
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO adapter_meta(key, value) VALUES ('trust_clock_high_water_at','1970-01-01T00:00:00Z')"
            )
            self.conn.execute(
                "INSERT INTO adapter_meta(key, value) VALUES ('contract_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(STORAGE_CONTRACT_VERSION),),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _inspect_profile_metadata(self) -> None:
        assert self.conn is not None
        row = self.conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone()
        if row is None:
            self.embedding_profile_metadata = None
            self.embedding_profile_metadata_invalid = False
            materialized = sum(
                int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("adapter_records", "memories", "memory_embeddings")
            )
            if materialized:
                self.embedding_compatibility_state = "UNPROVEN"
                self.embedding_compatibility_reason = (
                    "existing database has materialized records without embedding profile metadata"
                )
            return
        try:
            actual = json.loads(row[0])
        except json.JSONDecodeError:
            self.embedding_profile_metadata = None
            self.embedding_profile_metadata_invalid = True
            self.embedding_compatibility_state = "UNPROVEN"
            self.embedding_compatibility_reason = "stored embedding profile metadata is invalid"
            return
        if not isinstance(actual, dict):
            self.embedding_profile_metadata = None
            self.embedding_profile_metadata_invalid = True
            self.embedding_compatibility_state = "UNPROVEN"
            self.embedding_compatibility_reason = "stored embedding profile metadata is invalid"
            return
        self.embedding_profile_metadata_invalid = False
        self.embedding_profile_metadata = actual
        self.embedding_compatibility_state = "UNPROVEN"
        self.embedding_compatibility_reason = "stored embedding profile requires runtime compatibility proof"
        parsed = parse_embedding_profile_metadata(actual)
        if parsed is not None:
            self.embedding_store_key = str(parsed["store_key"])

    def _compatibility_snapshot(self) -> dict[str, object]:
        assert self.conn is not None
        def distinct(sql: str) -> list[object]:
            return [row[0] for row in self.conn.execute(sql).fetchall()]

        adapter_count = int(self.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0])
        memory_count = int(self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        vector_count = int(self.conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0])
        return {
            "profile_metadata": self.embedding_profile_metadata,
            "profile_metadata_invalid": self.embedding_profile_metadata_invalid,
            "adapter_count": adapter_count,
            "memory_count": memory_count,
            "vector_count": vector_count,
            "vector_dimension": self.vector_dimension,
            "adapter_profile_ids": distinct("SELECT DISTINCT embedding_profile_id FROM adapter_records ORDER BY embedding_profile_id"),
            "adapter_model_digests": distinct("SELECT DISTINCT embedding_model_digest FROM adapter_records ORDER BY embedding_model_digest"),
            "adapter_output_dimensions": distinct("SELECT DISTINCT embedding_output_dimension FROM adapter_records ORDER BY embedding_output_dimension"),
            "native_stores": distinct("SELECT DISTINCT store FROM memories WHERE deleted_at IS NULL ORDER BY store"),
            "vector_stores": distinct("SELECT DISTINCT store FROM memory_embeddings ORDER BY store"),
        }

    async def compatibility_snapshot(self) -> dict[str, object]:
        return await self._run(self._compatibility_snapshot)

    def set_embedding_compatibility(
        self,
        state: str,
        *,
        reason: str | None,
        store_key: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.embedding_compatibility_state = state
        self.embedding_compatibility_reason = reason
        if store_key:
            self.embedding_store_key = store_key
        if metadata is not None:
            self.embedding_profile_metadata = metadata

    def _replace_writer_fences(self) -> None:
        assert self.conn is not None
        for name, action in (
            ("factlane_v2_adapter_insert_fence", "INSERT"),
            ("factlane_v2_adapter_update_fence", "UPDATE"),
            ("factlane_v2_adapter_delete_fence", "DELETE"),
        ):
            self.conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            self.conn.execute(
                f"CREATE TRIGGER {name} BEFORE {action} ON adapter_records "
                f"WHEN {_WRITER_FUNCTION}() != 1 BEGIN "
                "SELECT RAISE(ABORT, 'FACTLANE_STORAGE_V2_COMPAT_WRITER_REQUIRED'); END"
            )

    def _bind_embedding_profile(
        self,
        metadata: dict[str, object],
        expected_profile: dict[str, object] | None,
        preserve_legacy_profile: bool,
        expect_missing: bool,
    ) -> None:
        assert self.conn is not None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone()
            current: object | None = None
            if row is not None:
                try:
                    current = json.loads(row[0])
                except json.JSONDecodeError as exc:
                    raise AdapterError("SCHEMA_MISMATCH", "adapter profile metadata is invalid") from exc
            if expect_missing and current is not None:
                raise AdapterError("PROFILE_MISMATCH", "embedding profile was initialized concurrently")
            if expected_profile is not None and current != expected_profile:
                raise AdapterError("PROFILE_MISMATCH", "embedding profile changed during compatibility proof")
            if preserve_legacy_profile and expected_profile is not None:
                self.conn.execute(
                    "INSERT INTO adapter_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
                    ("embedding_profile_legacy_v1", canonical_json(expected_profile)),
                )
            self.conn.execute(
                "INSERT INTO adapter_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("embedding_profile", canonical_json(metadata)),
            )
            self._migration_checkpoint("after_embedding_profile_binding")
            self._replace_writer_fences()
            self._migration_checkpoint("after_embedding_writer_fence")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    async def bind_embedding_profile(
        self,
        metadata: dict[str, object],
        *,
        expected_profile: dict[str, object] | None = None,
        preserve_legacy_profile: bool = False,
        expect_missing: bool = False,
    ) -> None:
        await self._run(
            self._bind_embedding_profile,
            metadata,
            expected_profile,
            preserve_legacy_profile,
            expect_missing,
        )
        self.embedding_profile_metadata = metadata
        self.embedding_store_key = str(metadata["store_key"])
        self.embedding_compatibility_state = "COMPATIBLE"
        self.embedding_compatibility_reason = None

    def require_embedding_compatibility(self) -> None:
        if self.embedding_compatibility_state != "COMPATIBLE":
            raise AdapterError(
                "PROFILE_MISMATCH",
                self.embedding_compatibility_reason or "embedding compatibility is not proven",
            )

    @staticmethod
    def _scope_where(scope: ScopeContext, alias: str = "a") -> tuple[str, list[Any]]:
        fields = {
            "scope": scope.scope,
            "project_id": scope.project_id,
            "worktree_id": scope.worktree_id,
            "workflow_id": scope.workflow_id,
            "agent_id": scope.agent_id,
        }
        clauses: list[str] = []
        params: list[Any] = []
        for field, value in fields.items():
            if value is None:
                clauses.append(f"{alias}.{field} IS NULL")
            else:
                clauses.append(f"{alias}.{field} = ?")
                params.append(value)
        return " AND ".join(clauses), params

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        return dict(zip(_RECORD_COLUMNS, row, strict=True))

    def _select_sql(self, extra: str = "") -> str:
        columns = ", ".join(f"a.{column}" for column in _RECORD_COLUMNS)
        return f"SELECT {columns} FROM adapter_records a {extra}"

    async def get_record(self, memory_id: str, scope: ScopeContext, history: bool = False) -> list[dict[str, Any]]:
        where, params = self._scope_where(scope)
        lifecycle = "" if history else " AND a.lifecycle_state = 'VALIDATED_CURRENT'"

        def query() -> list[dict[str, Any]]:
            assert self.conn is not None
            rows = self.conn.execute(
                self._select_sql(f"WHERE a.memory_id = ? AND {where}{lifecycle} ORDER BY a.revision DESC"),
                [memory_id, *params],
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

        return await self._run(query)

    async def select_logical_export_records(
        self,
        scope: ScopeContext,
        *,
        lifecycle_states: Sequence[str],
        max_records: int = MAX_LOGICAL_EXPORT_RECORDS,
    ) -> list[dict[str, Any]]:
        """Return one exact-scope, explicitly selected logical-export record set."""

        if not isinstance(scope, ScopeContext):
            raise AdapterError("INVALID_ENVELOPE", "logical export requires one exact ScopeContext")
        exact_scope = validate_scope(
            scope.scope,
            scope.project_id,
            scope.worktree_id,
            scope.workflow_id,
            scope.agent_id,
        )
        if exact_scope != scope:
            raise AdapterError("CROSS_SCOPE_DENIED", "logical export scope identity is not exact")
        if (
            isinstance(lifecycle_states, (str, bytes, bytearray))
            or not isinstance(lifecycle_states, Sequence)
            or not lifecycle_states
            or len(lifecycle_states) > len(LOGICAL_EXPORT_LIFECYCLE_STATES)
        ):
            raise AdapterError("INVALID_ENVELOPE", "logical export lifecycle selection is invalid or unbounded")

        normalized_states: list[str] = []
        for state in lifecycle_states:
            if not isinstance(state, str) or state not in LOGICAL_EXPORT_LIFECYCLE_STATES:
                raise AdapterError("INVALID_ENUM", "logical export lifecycle selection contains an unsupported state")
            if state not in normalized_states:
                normalized_states.append(state)
        if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= MAX_LOGICAL_EXPORT_RECORDS:
            raise AdapterError(
                "INVALID_ENVELOPE",
                f"logical export max_records must be between 1 and {MAX_LOGICAL_EXPORT_RECORDS}",
            )

        where, params = self._scope_where(exact_scope)
        selected_states = sorted(normalized_states)
        marks = ",".join("?" for _ in selected_states)

        def query() -> list[dict[str, Any]]:
            assert self.conn is not None
            rows = self.conn.execute(
                self._select_sql(
                    f"WHERE {where} AND a.lifecycle_state IN ({marks}) "
                    "ORDER BY a.memory_id ASC, a.revision ASC, a.record_id ASC LIMIT ?"
                ),
                [*params, *selected_states, max_records + 1],
            ).fetchall()
            if len(rows) > max_records:
                raise AdapterError(
                    "LIMIT_EXCEEDED",
                    f"logical export selection exceeds the {max_records} record limit",
                )
            return [self._row_to_dict(row) for row in rows]

        return await self._run(query)

    async def resolve_permanent_purge_targets(
        self,
        identities: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Resolve exact permanent-purge identities into bounded persisted target facts."""

        if (
            isinstance(identities, (str, bytes, bytearray))
            or not isinstance(identities, Sequence)
            or not 1 <= len(identities) <= MAX_PERMANENT_PURGE_TARGETS
        ):
            raise AdapterError(
                "PURGE_TARGET_INVALID",
                f"permanent purge requires 1 to {MAX_PERMANENT_PURGE_TARGETS} exact targets",
            )

        normalized: list[tuple[ScopeContext, str, str, int]] = []
        seen: set[tuple[Any, ...]] = set()
        for raw in identities:
            if not isinstance(raw, Mapping) or set(raw) != {"scope", "memory_id", "record_id", "revision"}:
                raise AdapterError("PURGE_TARGET_INVALID", "permanent purge target identity is malformed")
            scope = raw.get("scope")
            if not isinstance(scope, ScopeContext):
                raise AdapterError("PURGE_TARGET_INVALID", "permanent purge target requires one exact ScopeContext")
            exact_scope = validate_scope(
                scope.scope,
                scope.project_id,
                scope.worktree_id,
                scope.workflow_id,
                scope.agent_id,
            )
            if exact_scope != scope:
                raise AdapterError("PURGE_TARGET_INVALID", "permanent purge target scope identity is not exact")
            memory_id = validate_identifier(raw.get("memory_id"), "memory_id", required=True)
            record_id = validate_identifier(raw.get("record_id"), "record_id", required=True)
            revision = raw.get("revision")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise AdapterError("PURGE_TARGET_INVALID", "permanent purge target revision is invalid")
            assert memory_id is not None and record_id is not None
            key = (
                exact_scope.scope,
                exact_scope.project_id,
                exact_scope.worktree_id,
                exact_scope.workflow_id,
                exact_scope.agent_id,
                memory_id,
                record_id,
                revision,
            )
            if key in seen:
                raise AdapterError("PURGE_TARGET_INVALID", "permanent purge target set contains duplicates")
            seen.add(key)
            normalized.append((exact_scope, memory_id, record_id, revision))

        def belief_refs_hash(content_hash: str) -> bool:
            assert self.conn is not None
            table = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='beliefs'"
            ).fetchone()
            if table is None:
                return False
            for row in self.conn.execute("SELECT derived_from, contradicted_by FROM beliefs").fetchall():
                for raw in (row[0], row[1]):
                    try:
                        values = json.loads(raw or "[]")
                    except json.JSONDecodeError:
                        return True
                    if not isinstance(values, list):
                        return True
                    for value in values:
                        if (
                            not isinstance(value, str)
                            or len(value) != 64
                            or any(character not in "0123456789abcdefABCDEF" for character in value)
                        ):
                            return True
                    if content_hash in values:
                        return True
            return False

        def query() -> list[dict[str, Any]]:
            assert self.conn is not None
            results: list[dict[str, Any]] = []
            orphan_vectors: int | None = None
            for scope, memory_id, record_id, revision in normalized:
                row = self.conn.execute(
                    self._select_sql("WHERE a.record_id=?"),
                    (record_id,),
                ).fetchone()
                if row is None:
                    raise AdapterError("PURGE_TARGET_DRIFT", "exact permanent purge target record is missing")
                record = self._row_to_dict(row)
                actual_scope = (
                    record["scope"],
                    record["project_id"],
                    record["worktree_id"],
                    record["workflow_id"],
                    record["agent_id"],
                )
                requested_scope = (
                    scope.scope,
                    scope.project_id,
                    scope.worktree_id,
                    scope.workflow_id,
                    scope.agent_id,
                )
                if (
                    record["memory_id"] != memory_id
                    or int(record["revision"]) != revision
                    or actual_scope != requested_scope
                ):
                    raise AdapterError("PURGE_TARGET_DRIFT", "exact permanent purge target binding changed")
                lifecycle = record["lifecycle_state"]
                if lifecycle not in LOGICAL_EXPORT_LIFECYCLE_STATES:
                    raise AdapterError("PURGE_TARGET_DRIFT", "permanent purge target lifecycle is unsupported")
                content_hash = record["native_content_hash"]
                if not isinstance(content_hash, str) or not content_hash:
                    raise AdapterError("PURGE_TARGET_DRIFT", "permanent purge target native identity is malformed")

                native_rows = self.conn.execute(
                    "SELECT id, deleted_at FROM memories WHERE content_hash=?",
                    (content_hash,),
                ).fetchall()
                if len(native_rows) > 1:
                    raise AdapterError("PURGE_TARGET_AMBIGUOUS", "permanent purge native materialization is ambiguous")

                if native_rows:
                    native_id = int(native_rows[0][0])
                    if native_rows[0][1] is not None:
                        raise AdapterError(
                            "PURGE_TARGET_MATERIALIZATION_UNSUPPORTED",
                            "permanent purge target has unexpected deleted native materialization",
                        )
                    stores = [
                        str(item[0])
                        for item in self.conn.execute(
                            "SELECT store FROM memory_embeddings WHERE rowid=? ORDER BY store",
                            (native_id,),
                        ).fetchall()
                    ]
                    if stores != [self.embedding_store_key] or belief_refs_hash(content_hash):
                        raise AdapterError(
                            "PURGE_TARGET_MATERIALIZATION_UNSUPPORTED",
                            "permanent purge target has unsupported derived materialization",
                        )
                    materialization = _PURGE_PM1
                    native_materialized = True
                else:
                    if orphan_vectors is None:
                        orphan_vectors = int(
                            self.conn.execute(
                                "SELECT COUNT(*) FROM memory_embeddings e WHERE NOT EXISTS "
                                "(SELECT 1 FROM memories m WHERE m.id=e.rowid)"
                            ).fetchone()[0]
                        )
                    if orphan_vectors or belief_refs_hash(content_hash) or lifecycle != "HISTORICAL":
                        raise AdapterError(
                            "PURGE_TARGET_MATERIALIZATION_UNSUPPORTED",
                            "permanent purge target has unsupported partial materialization",
                        )
                    materialization = _PURGE_PM0
                    native_materialized = False

                results.append(
                    {
                        "scope": scope.scope,
                        "project_id": scope.project_id,
                        "worktree_id": scope.worktree_id,
                        "workflow_id": scope.workflow_id,
                        "agent_id": scope.agent_id,
                        "memory_id": memory_id,
                        "record_id": record_id,
                        "revision": revision,
                        "lifecycle_state": lifecycle,
                        "materialization_class": materialization,
                        "native_materialized": native_materialized,
                        "native_content_hash": content_hash,
                    }
                )
            return results

        return await self._run(query)

    async def memory_exists_outside_scope(self, memory_id: str, scope: ScopeContext) -> bool:
        where, params = self._scope_where(scope)

        def query() -> bool:
            assert self.conn is not None
            row = self.conn.execute(
                f"SELECT 1 FROM adapter_records a WHERE a.memory_id = ? AND NOT ({where}) LIMIT 1",
                [memory_id, *params],
            ).fetchone()
            return row is not None

        return await self._run(query)

    async def find_idempotency(self, key: str) -> dict[str, Any] | None:
        def query() -> dict[str, Any] | None:
            assert self.conn is not None
            row = self.conn.execute(self._select_sql("WHERE a.idempotency_key = ?"), (key,)).fetchone()
            return self._row_to_dict(row) if row else None

        return await self._run(query)

    async def find_exact(self, native_content_hash_value: str) -> dict[str, Any] | None:
        def query() -> dict[str, Any] | None:
            assert self.conn is not None
            row = self.conn.execute(
                self._select_sql("WHERE a.native_content_hash = ? ORDER BY a.revision DESC LIMIT 1"),
                (native_content_hash_value,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

        return await self._run(query)

    async def find_contradictions(self, key: str, scope: ScopeContext) -> list[dict[str, Any]]:
        where, params = self._scope_where(scope)

        def query() -> list[dict[str, Any]]:
            assert self.conn is not None
            rows = self.conn.execute(
                self._select_sql(
                    f"WHERE a.contradiction_key = ? AND {where} "
                    "AND a.lifecycle_state = 'VALIDATED_CURRENT' "
                    "ORDER BY a.created_at, a.record_id"
                ),
                [key, *params],
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

        return await self._run(query)

    def _latest_freshness_event_sync(self, record_id: str, revision: int) -> dict[str, Any]:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT event_id,freshness_outcome,freshness_generation FROM lifecycle_events "
            "WHERE target_record_id=? AND target_revision=? AND freshness_generation IS NOT NULL "
            "ORDER BY freshness_generation DESC LIMIT 1",
            (record_id, revision),
        ).fetchone()
        if row is None:
            return {
                "freshness_gate_state": "NONE",
                "latest_freshness_event_id": None,
                "freshness_generation": 0,
            }
        outcome = str(row[1]) if row[1] is not None else None
        return {
            "freshness_gate_state": _ADVERSE_FRESHNESS_GATES.get(outcome, "NONE"),
            "latest_freshness_event_id": str(row[0]),
            "freshness_generation": int(row[2]),
        }

    async def freshness_state(self, record_id: str, revision: int) -> dict[str, Any]:
        return await self._run(self._latest_freshness_event_sync, record_id, revision)

    def _group_observation_sync(self, scope: ScopeContext, contradiction_key: str) -> dict[str, Any]:
        assert self.conn is not None
        where, params = self._scope_where(scope)
        state_marks = ",".join("?" for _ in _GOVERNANCE_ACTIVE_STATES)
        ordered_states = sorted(_GOVERNANCE_ACTIVE_STATES)
        count = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM adapter_records a WHERE a.contradiction_key=? AND {where} "
                f"AND a.lifecycle_state IN ({state_marks})",
                [contradiction_key, *params, *ordered_states],
            ).fetchone()[0]
        )
        if count > MAX_CONTRADICTION_GROUP_MEMBERS:
            raise AdapterError(
                "GROUP_LIMIT_EXCEEDED",
                "contradiction group exceeds the mutation-safe member bound",
                member_count=count,
                limit=MAX_CONTRADICTION_GROUP_MEMBERS,
            )
        rows = self.conn.execute(
            self._select_sql(
                f"WHERE a.contradiction_key=? AND {where} AND a.lifecycle_state IN ({state_marks}) "
                "ORDER BY a.record_id"
            ),
            [contradiction_key, *params, *ordered_states],
        ).fetchall()
        members: list[dict[str, Any]] = []
        compact_members: list[dict[str, Any]] = []
        for raw in rows:
            record = self._row_to_dict(raw)
            freshness = self._latest_freshness_event_sync(record["record_id"], int(record["revision"]))
            hidden = {
                "memory_id": record["memory_id"],
                "record_id": record["record_id"],
                "parent_record_id": record["parent_record_id"],
                "revision": int(record["revision"]),
                "lifecycle_state": record["lifecycle_state"],
                "contradiction_state": record["contradiction_state"],
                "payload_fingerprint": record["payload_fingerprint"],
                **freshness,
            }
            member_token = hashlib.sha256(
                canonical_json(
                    {
                        "parent_record_id": hidden["parent_record_id"],
                        "payload_fingerprint": hidden["payload_fingerprint"],
                        "latest_freshness_event_id": hidden["latest_freshness_event_id"],
                        "freshness_generation": hidden["freshness_generation"],
                    }
                ).encode("utf-8")
            ).hexdigest()
            members.append(hidden)
            compact_members.append(
                {
                    "memory_id": hidden["memory_id"],
                    "record_id": hidden["record_id"],
                    "revision": hidden["revision"],
                    "lifecycle_state": hidden["lifecycle_state"],
                    "contradiction_state": hidden["contradiction_state"],
                    "freshness_gate_state": hidden["freshness_gate_state"],
                    "member_token": member_token,
                }
            )
        server_observation = {
            "schema_version": 2,
            "scope": scope.to_dict(),
            "contradiction_key": contradiction_key,
            "member_count": count,
            "members": members,
        }
        observed_group_token = hashlib.sha256(canonical_json(server_observation).encode("utf-8")).hexdigest()
        compact = {
            "schema_version": 2,
            "scope": scope.to_dict(),
            "contradiction_key": contradiction_key,
            "member_count": count,
            "observed_group_token": observed_group_token,
            "members": compact_members,
        }
        encoded_size = len(canonical_json(compact).encode("utf-8"))
        if encoded_size > MAX_GROUP_CAS_BYTES:
            raise AdapterError(
                "GROUP_OBSERVATION_TOO_LARGE",
                "contradiction group observation exceeds the mutation-safe byte bound",
                member_count=count,
                serialized_bytes=encoded_size,
                limit=MAX_GROUP_CAS_BYTES,
            )
        return {"server": server_observation, "public": compact}

    async def group_observation(self, scope: ScopeContext, contradiction_key: str) -> dict[str, Any]:
        return await self._run(self._group_observation_sync, scope, contradiction_key)

    def _require_group_token_sync(
        self,
        scope: ScopeContext,
        contradiction_key: str,
        observed_group_token: str,
    ) -> dict[str, Any]:
        observed = self._group_observation_sync(scope, contradiction_key)
        if observed["public"]["observed_group_token"] != observed_group_token:
            raise AdapterError(
                "GROUP_VERSION_CONFLICT",
                "contradiction group changed after Owner observation",
                refetch_required=True,
                current_group_token=observed["public"]["observed_group_token"],
                member_count=observed["public"]["member_count"],
            )
        return observed

    @staticmethod
    def _normalized_fact(value: str) -> str:
        return " ".join(value.casefold().split())

    def _operation_row_sync(self, idempotency_key: str) -> dict[str, Any] | None:
        assert self.conn is not None
        columns = ",".join(_LIFECYCLE_OPERATION_COLUMNS)
        row = self.conn.execute(
            f"SELECT {columns} FROM lifecycle_operations WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return dict(zip(_LIFECYCLE_OPERATION_COLUMNS, row, strict=True)) if row else None

    async def operation_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        return await self._run(self._operation_row_sync, idempotency_key)

    def _idempotency_owner_sync(self, idempotency_key: str) -> str | None:
        assert self.conn is not None
        rows = self.conn.execute(
            "SELECT owner FROM ("
            "SELECT 'operation' AS owner FROM lifecycle_operations WHERE idempotency_key=? "
            "UNION ALL "
            "SELECT 'adapter' AS owner FROM adapter_records WHERE idempotency_key=?"
            ") LIMIT 2",
            (idempotency_key, idempotency_key),
        ).fetchall()
        if len(rows) > 1:
            raise AdapterError("STORAGE_INTEGRITY", "idempotency key is reserved in multiple global namespaces")
        return str(rows[0][0]) if rows else None

    async def idempotency_owner(self, idempotency_key: str) -> str | None:
        return await self._run(self._idempotency_owner_sync, idempotency_key)

    def _operation_replay_sync(
        self,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> dict[str, Any] | None:
        assert self.conn is not None
        if self.conn.execute(
            "SELECT 1 FROM adapter_records WHERE idempotency_key=? LIMIT 1", (idempotency_key,)
        ).fetchone():
            raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is reserved by a legacy write")
        replay = self._operation_row_sync(idempotency_key)
        if replay is None:
            return None
        if replay["replay_status"] == "RECOVERY_INVALIDATED":
            raise AdapterError(
                "RECOVERY_INVALIDATED_REPLAY",
                "operation replay was invalidated by sensitive Recovery",
            )
        if replay["request_fingerprint"] != request_fingerprint:
            raise AdapterError(
                "IDEMPOTENCY_CONFLICT",
                "idempotency key is bound to another governed operation",
            )
        return self._operation_result(replay, replay=True)

    async def operation_replay(
        self,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._operation_replay_sync, idempotency_key, request_fingerprint)

    def _logical_import_replay_sync(
        self,
        idempotency_key: str,
        operation_id: str,
        request_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        assert self.conn is not None
        operation = self._operation_row_sync(idempotency_key)
        if operation is not None:
            if operation["replay_status"] == "RECOVERY_INVALIDATED":
                raise AdapterError(
                    "RECOVERY_INVALIDATED_REPLAY",
                    "logical import replay was invalidated by sensitive Recovery",
                )
            if (
                operation["operation_kind"] != "LOGICAL_IMPORT"
                or operation["operation_id"] != operation_id
                or (
                    request_fingerprint is not None
                    and operation["request_fingerprint"] != request_fingerprint
                )
            ):
                raise AdapterError(
                    "IDEMPOTENCY_CONFLICT",
                    "logical import idempotency key is bound to another governed operation",
                )
            return self._operation_result(operation, replay=True)

        columns = ",".join(_LIFECYCLE_OPERATION_COLUMNS)
        row = self.conn.execute(
            f"SELECT {columns} FROM lifecycle_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is not None:
            existing = dict(zip(_LIFECYCLE_OPERATION_COLUMNS, row, strict=True))
            if existing["replay_status"] == "RECOVERY_INVALIDATED":
                raise AdapterError(
                    "RECOVERY_INVALIDATED_REPLAY",
                    "logical import replay was invalidated by sensitive Recovery",
                )
            if (
                existing["idempotency_key"] != idempotency_key
                or existing["operation_kind"] != "LOGICAL_IMPORT"
                or (
                    request_fingerprint is not None
                    and existing["request_fingerprint"] != request_fingerprint
                )
            ):
                raise AdapterError(
                    "IDEMPOTENCY_CONFLICT",
                    "logical import operation identity is already bound to another governed operation",
                )
            return self._operation_result(existing, replay=True)

        if self.conn.execute(
            "SELECT 1 FROM adapter_records WHERE idempotency_key=? LIMIT 1",
            (idempotency_key,),
        ).fetchone():
            raise AdapterError(
                "IDEMPOTENCY_CONFLICT",
                "logical import idempotency key is already reserved by another write",
            )
        return None

    async def logical_import_replay(
        self,
        idempotency_key: str,
        operation_id: str,
    ) -> dict[str, Any] | None:
        """Return an exact logical-import replay before any new embedding work."""

        return await self._run(self._logical_import_replay_sync, idempotency_key, operation_id)

    def _logical_import_receipt_sync(self, operation_id: str) -> dict[str, Any] | None:
        assert self.conn is not None
        columns = ",".join(_LIFECYCLE_OPERATION_COLUMNS)
        row = self.conn.execute(
            f"SELECT {columns} FROM lifecycle_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        operation = dict(zip(_LIFECYCLE_OPERATION_COLUMNS, row, strict=True))
        if operation["operation_kind"] != "LOGICAL_IMPORT":
            return None
        if operation["replay_status"] == "RECOVERY_INVALIDATED":
            raise AdapterError(
                "RECOVERY_INVALIDATED_REPLAY",
                "logical import evidence receipt was invalidated by sensitive Recovery",
            )
        return self._operation_result(operation, replay=False)

    async def logical_import_receipt(self, operation_id: str) -> dict[str, Any] | None:
        """Read one durable LOGICAL_IMPORT receipt without mutating storage."""

        return await self._run(self._logical_import_receipt_sync, operation_id)

    @staticmethod
    def _operation_result(operation: dict[str, Any], *, replay: bool) -> dict[str, Any]:
        if operation["operation_kind"] == "MANUAL_RECHECK" and operation["replay_status"] == "BLOCKED":
            return {
                "status": "BLOCKED",
                "error": operation["decision_error"],
                "request_id": operation["request_id"],
                "operation_id": operation["operation_id"],
                "decision_at": operation["decision_at"],
                "idempotent_replay": replay,
            }
        affected = json.loads(operation["affected_members_json"] or "[]")
        event_ids = json.loads(operation["event_ids_json"] or "[]")
        if operation["operation_kind"] == "MANUAL_RECHECK":
            gate = {
                "CHANGED": "SOURCE_CHANGE_REVIEW_REQUIRED",
                "UNAVAILABLE": "RECHECK_UNAVAILABLE",
                "INDETERMINATE": "RECHECK_INDETERMINATE",
            }.get(operation["result_outcome"], "NONE")
            return {
                "status": "OK",
                "operation_id": operation["operation_id"],
                "event_ids": event_ids,
                "request_id": operation["request_id"],
                "idempotent_replay": replay,
                "target_memory_id": operation["target_memory_id"],
                "prior_record_id": operation["target_record_id"],
                "resulting_record_id": operation["result_record_id"],
                "resulting_revision": operation["result_revision"],
                "outcome": operation["result_outcome"],
                "observed_at": operation["observed_at"],
                "accepted_at": operation["commit_timestamp"],
                "freshness_gate_state": gate,
                "action_required": (
                    "NONE"
                    if gate == "NONE"
                    else "REPLACE_OR_RESOLVE_OR_REVOKE"
                    if gate == "SOURCE_CHANGE_REVIEW_REQUIRED"
                    else "RETRY_WITH_NEW_EVIDENCE"
                ),
                "resulting_group_token": operation["result_group_token"],
                "accepted_event_freshness_generation": operation["accepted_event_freshness_generation"],
                "resulting_member_freshness_generation": operation["resulting_member_freshness_generation"],
            }
        if operation["operation_kind"] == "RESOLVE":
            return {
                "status": "OK",
                "action": "RESOLVE",
                "operation_id": operation["operation_id"],
                "request_id": operation["request_id"],
                "idempotent_replay": replay,
                "winner": {
                    "memory_id": operation["winner_memory_id"],
                    "parent_record_id": operation["winner_parent_record_id"],
                    "result_record_id": operation["result_record_id"],
                    "resulting_revision": operation["result_revision"],
                    "lifecycle_state": "VALIDATED_CURRENT",
                },
                "affected_count": len(affected),
                "affected_members": affected,
                "event_ids": event_ids,
                "resulting_group": {
                    "conflict_state": "CLOSED",
                    "member_count": operation["result_group_count"],
                    "group_token": operation["result_group_token"],
                },
            }
        if operation["operation_kind"] == "READMIT":
            result: dict[str, Any] = {
                "status": operation["result_outcome"] or "OK",
                "operation_id": operation["operation_id"],
                "event_ids": event_ids,
                "request_id": operation["request_id"],
                "idempotent_replay": replay,
                "memory_id": operation["target_memory_id"],
                "revoked_record_id": operation["target_record_id"],
                "result_record_id": operation["result_record_id"],
                "resulting_revision": operation["result_revision"],
                "resulting_lifecycle": operation["result_lifecycle_state"],
                "resulting_contradiction_state": operation["result_contradiction_state"],
                "resulting_group_token": operation["result_group_token"],
            }
            return result
        if operation["operation_kind"] == "LOGICAL_IMPORT":
            affected = json.loads(operation["affected_members_json"] or "[]")
            if not isinstance(affected, list) or any(not isinstance(item, dict) for item in affected):
                raise AdapterError("STORAGE_INTEGRITY", "logical import evidence receipt is malformed")
            for item in affected:
                if (
                    item.get("evidence_class") != "NON_AUTHORITATIVE_SOURCE_RECORD"
                    or item.get("authority_effect") != "NONE"
                    or not isinstance(item.get("source_record"), dict)
                    or item.get("status") not in {"ADMITTED", "EXACT_DUPLICATE", "EQUIVALENT", "HISTORY_ONLY"}
                ):
                    raise AdapterError("STORAGE_INTEGRITY", "logical import evidence receipt is malformed")
                if item["status"] == "HISTORY_ONLY" and any(
                    item.get(field) is not None
                    for field in (
                        "destination_memory_id",
                        "destination_record_id",
                        "destination_lifecycle_state",
                        "destination_verified_by",
                        "destination_authority_role",
                    )
                ):
                    raise AdapterError("STORAGE_INTEGRITY", "logical import history evidence gained destination authority")
            event_ids = json.loads(operation["event_ids_json"] or "[]")
            active_evidence = [item for item in affected if item["status"] != "HISTORY_ONLY"]
            external_history = [item for item in affected if item["status"] == "HISTORY_ONLY"]
            return {
                "status": "OK",
                "action": "LOGICAL_IMPORT",
                "operation_id": operation["operation_id"],
                "request_id": operation["request_id"],
                "idempotency_key": operation["idempotency_key"],
                "idempotent_replay": replay,
                "evidence_policy": "NON_AUTHORITATIVE_IMPORT_EVIDENCE",
                "admitted": sum(item.get("status") == "ADMITTED" for item in active_evidence),
                "duplicates": sum(
                    item.get("status") in {"EXACT_DUPLICATE", "EQUIVALENT"}
                    for item in active_evidence
                ),
                "contradictions": sum(
                    item.get("status") == "ADMITTED"
                    and item.get("contradiction_state") == "UNRESOLVED"
                    for item in active_evidence
                ),
                "results": active_evidence,
                "source_evidence": affected,
                "active_source_evidence": active_evidence,
                "active_source_evidence_count": len(active_evidence),
                "external_history": external_history,
                "external_history_count": len(external_history),
                "event_ids": event_ids,
                "committed_at": operation["commit_timestamp"],
            }
        return {
            "status": "OK",
            "action": operation["operation_kind"],
            "operation_id": operation["operation_id"],
            "request_id": operation["request_id"],
            "idempotent_replay": replay,
            "target_memory_id": operation["target_memory_id"],
            "target_record_id": operation["target_record_id"],
            "result_record_id": operation["result_record_id"],
            "resulting_revision": operation["result_revision"],
            "resulting_lifecycle_state": operation["result_lifecycle_state"],
            "resulting_contradiction_state": operation["result_contradiction_state"],
            "affected_count": len(affected),
            "affected_members": affected,
            "event_ids": event_ids,
            "resulting_group": {
                "member_count": operation["result_group_count"],
                "group_token": operation["result_group_token"],
            },
        }

    def _insert_lifecycle_event_sync(
        self,
        *,
        event_id: str,
        operation_id: str | None,
        record: dict[str, Any],
        action: str,
        actor_class: str,
        actor_ref_digest: str | None,
        grant_ref_digest: str,
        occurred_at: str,
        prior_lifecycle_state: str | None = None,
        result_lifecycle_state: str | None = None,
        prior_contradiction_state: str | None = None,
        result_contradiction_state: str | None = None,
        reason_code: str | None = None,
        expected_revision: int | None = None,
        result_revision: int | None = None,
        related_record_id: str | None = None,
        related_memory_id: str | None = None,
        freshness_outcome: str | None = None,
        observation_origin: str | None = None,
        observed_at: str | None = None,
        accepted_at: str | None = None,
        evidence_ref_digest: str | None = None,
        freshness_generation: int | None = None,
        clock_anomaly: str = "NONE",
    ) -> None:
        assert self.conn is not None
        self.conn.execute(
            "INSERT INTO lifecycle_events("
            "event_id,operation_id,memory_id,record_id,target_record_id,target_revision,action,"
            "prior_lifecycle_state,result_lifecycle_state,prior_contradiction_state,result_contradiction_state,"
            "reason_code,actor_class,actor_ref_digest,grant_ref_digest,occurred_at,expected_revision,result_revision,"
            "related_record_id,related_memory_id,freshness_outcome,observation_origin,observed_at,accepted_at,"
            "evidence_ref_digest,freshness_generation,clock_anomaly"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                operation_id,
                record["memory_id"],
                record["record_id"],
                record["record_id"],
                int(record["revision"]),
                action,
                prior_lifecycle_state,
                result_lifecycle_state,
                prior_contradiction_state,
                result_contradiction_state,
                reason_code,
                actor_class,
                actor_ref_digest,
                grant_ref_digest,
                occurred_at,
                expected_revision,
                result_revision,
                related_record_id,
                related_memory_id,
                freshness_outcome,
                observation_origin,
                observed_at,
                accepted_at,
                evidence_ref_digest,
                freshness_generation,
                clock_anomaly,
            ),
        )

    def _recompute_candidate_contradictions_sync(
        self,
        scope: ScopeContext,
        contradiction_key: str,
        *,
        operation_id: str,
        actor_class: str,
        actor_ref_digest: str | None,
        grant_ref_digest: str,
        occurred_at: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        assert self.conn is not None
        where, params = self._scope_where(scope)
        marks = ",".join("?" for _ in _GOVERNANCE_ACTIVE_STATES)
        states = sorted(_GOVERNANCE_ACTIVE_STATES)
        rows = [
            self._row_to_dict(row)
            for row in self.conn.execute(
                self._select_sql(
                    f"WHERE a.contradiction_key=? AND {where} AND a.lifecycle_state IN ({marks}) ORDER BY a.record_id"
                ),
                [contradiction_key, *params, *states],
            ).fetchall()
        ]
        currents = [row for row in rows if row["lifecycle_state"] == "VALIDATED_CURRENT"]
        if len({self._normalized_fact(row["fact"]) for row in currents}) > 1:
            raise AdapterError("LIFECYCLE_INVARIANT_VIOLATION", "contradiction group contains incompatible Current rows")
        affected: list[dict[str, Any]] = []
        event_ids: list[str] = []
        for candidate in (row for row in rows if row["lifecycle_state"] == "CANDIDATE"):
            candidate_fact = self._normalized_fact(candidate["fact"])
            incompatible = any(
                peer["record_id"] != candidate["record_id"]
                and self._normalized_fact(peer["fact"]) != candidate_fact
                for peer in rows
            )
            desired = "UNRESOLVED" if incompatible else "NONE"
            if candidate["contradiction_state"] == desired:
                continue
            prior = candidate["contradiction_state"]
            self.conn.execute(
                "UPDATE adapter_records SET contradiction_state=? WHERE record_id=?",
                (desired, candidate["record_id"]),
            )
            event_id = str(uuid.uuid4())
            self._insert_lifecycle_event_sync(
                event_id=event_id,
                operation_id=operation_id,
                record=candidate,
                action="CONTRADICTION_RECOMPUTED",
                actor_class=actor_class,
                actor_ref_digest=actor_ref_digest,
                grant_ref_digest=grant_ref_digest,
                occurred_at=occurred_at,
                prior_lifecycle_state=candidate["lifecycle_state"],
                result_lifecycle_state=candidate["lifecycle_state"],
                prior_contradiction_state=prior,
                result_contradiction_state=desired,
                expected_revision=int(candidate["revision"]),
                result_revision=int(candidate["revision"]),
            )
            affected.append(
                {
                    "memory_id": candidate["memory_id"],
                    "record_id": candidate["record_id"],
                    "from_lifecycle_state": candidate["lifecycle_state"],
                    "to_lifecycle_state": candidate["lifecycle_state"],
                    "from_contradiction_state": prior,
                    "to_contradiction_state": desired,
                }
            )
            event_ids.append(event_id)
        return affected, event_ids

    def _terminal_disposition_sync(
        self,
        scope: ScopeContext,
        *,
        action: str,
        memory_id: str,
        expected_record_id: str,
        expected_revision: int,
        observed_group_token: str,
        idempotency_key: str,
        request_fingerprint: str,
        request_id: str,
        reason_code: str,
        actor_class: str,
        actor_ref_digest: str | None,
        grant_ref_digest: str,
        committed_at: str,
    ) -> dict[str, Any]:
        assert self.conn is not None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            replay = self._operation_row_sync(idempotency_key)
            if replay is not None:
                if replay["replay_status"] == "RECOVERY_INVALIDATED":
                    raise AdapterError("RECOVERY_INVALIDATED_REPLAY", "operation replay was invalidated by sensitive Recovery")
                if replay["request_fingerprint"] != request_fingerprint:
                    raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is bound to another governed operation")
                result = self._operation_result(replay, replay=True)
                self.conn.rollback()
                return result
            adapter_collision = self.conn.execute(
                "SELECT 1 FROM adapter_records WHERE idempotency_key=? LIMIT 1", (idempotency_key,)
            ).fetchone()
            if adapter_collision is not None:
                raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is already reserved by another write")
            self._sample_trust_clock_sync(iso_now())
            self._sample_trust_clock_sync(committed_at)
            where, params = self._scope_where(scope)
            target_row = self.conn.execute(
                self._select_sql(
                    f"WHERE a.memory_id=? AND a.record_id=? AND {where} LIMIT 1"
                ),
                [memory_id, expected_record_id, *params],
            ).fetchone()
            if target_row is None:
                outside = self.conn.execute(
                    f"SELECT 1 FROM adapter_records a WHERE a.memory_id=? AND NOT ({where}) LIMIT 1",
                    [memory_id, *params],
                ).fetchone()
                if outside is not None:
                    raise AdapterError("CROSS_SCOPE_DENIED", "memory_id exists outside the requested scope")
                raise AdapterError("NOT_FOUND", "target record was not found in the requested scope")
            target = self._row_to_dict(target_row)
            required_state = "VALIDATED_CURRENT" if action == "REVOKE" else "CANDIDATE"
            if target["lifecycle_state"] != required_state:
                raise AdapterError("VERSION_CONFLICT", f"{action} target is no longer {required_state}")
            if int(target["revision"]) != expected_revision:
                raise AdapterError("VERSION_CONFLICT", "expected_revision does not match the target revision")
            observed = self._group_observation_sync(scope, target["contradiction_key"])
            if observed["public"]["observed_group_token"] != observed_group_token:
                raise AdapterError(
                    "GROUP_VERSION_CONFLICT",
                    "contradiction group changed after Owner observation",
                    refetch_required=True,
                    current_group_token=observed["public"]["observed_group_token"],
                    member_count=observed["public"]["member_count"],
                )
            if any(
                member["lifecycle_state"] in {"STALE", "QUARANTINED"}
                for member in observed["server"]["members"]
            ):
                raise AdapterError("GROUP_MEMBER_NOT_ADMISSIBLE", "group contains a stale or quarantined blocking member")
            operation_id = str(uuid.uuid4())
            result_state = "REVOKED" if action == "REVOKE" else "REJECTED"
            prior_contradiction = target["contradiction_state"]
            self.conn.execute(
                "UPDATE adapter_records SET lifecycle_state=?, contradiction_state='RESOLVED' WHERE record_id=?",
                (result_state, target["record_id"]),
            )
            target_event = str(uuid.uuid4())
            self._insert_lifecycle_event_sync(
                event_id=target_event,
                operation_id=operation_id,
                record=target,
                action=action,
                actor_class=actor_class,
                actor_ref_digest=actor_ref_digest,
                grant_ref_digest=grant_ref_digest,
                occurred_at=committed_at,
                prior_lifecycle_state=target["lifecycle_state"],
                result_lifecycle_state=result_state,
                prior_contradiction_state=prior_contradiction,
                result_contradiction_state="RESOLVED",
                reason_code=reason_code,
                expected_revision=expected_revision,
                result_revision=expected_revision,
            )
            affected = [
                {
                    "memory_id": target["memory_id"],
                    "record_id": target["record_id"],
                    "from_lifecycle_state": target["lifecycle_state"],
                    "to_lifecycle_state": result_state,
                    "from_contradiction_state": prior_contradiction,
                    "to_contradiction_state": "RESOLVED",
                }
            ]
            recomputed, recompute_events = self._recompute_candidate_contradictions_sync(
                scope,
                target["contradiction_key"],
                operation_id=operation_id,
                actor_class=actor_class,
                actor_ref_digest=actor_ref_digest,
                grant_ref_digest=grant_ref_digest,
                occurred_at=committed_at,
            )
            affected.extend(recomputed)
            event_ids = [target_event, *recompute_events]
            resulting_group = self._group_observation_sync(scope, target["contradiction_key"])["public"]
            self.conn.execute(
                "INSERT INTO lifecycle_operations("
                "idempotency_key,request_fingerprint,operation_id,request_id,operation_kind,actor_class,actor_ref_digest,"
                "grant_ref_digest,scope_digest,target_memory_id,target_record_id,result_record_id,result_revision,"
                "result_lifecycle_state,result_contradiction_state,affected_members_json,event_ids_json,"
                "result_group_token,result_group_count,replay_status,commit_timestamp"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    idempotency_key,
                    request_fingerprint,
                    operation_id,
                    request_id,
                    action,
                    actor_class,
                    actor_ref_digest,
                    grant_ref_digest,
                    hashlib.sha256(scope.key().encode("utf-8")).hexdigest(),
                    memory_id,
                    expected_record_id,
                    expected_record_id,
                    expected_revision,
                    result_state,
                    "RESOLVED",
                    canonical_json(affected),
                    canonical_json(event_ids),
                    resulting_group["observed_group_token"],
                    resulting_group["member_count"],
                    "COMMITTED",
                    committed_at,
                ),
            )
            operation = self._operation_row_sync(idempotency_key)
            assert operation is not None
            self.conn.commit()
            return self._operation_result(operation, replay=False)
        except BaseException:
            self.conn.rollback()
            raise

    async def terminal_disposition(
        self,
        scope: ScopeContext,
        **values: Any,
    ) -> dict[str, Any]:
        return await self._run(lambda: self._terminal_disposition_sync(scope, **values))

    def _copy_materialized_successor_sync(
        self,
        parent: dict[str, Any],
        successor: dict[str, Any],
    ) -> None:
        """Insert an immutable-fact successor while reusing the parent's qualified embedding bytes."""
        assert self.conn is not None
        _validate_current_authority_metadata(successor)
        if successor["memory_id"] != parent["memory_id"]:
            raise AdapterError("INVALID_ENVELOPE", "governed successor must preserve memory_id")
        if successor["parent_record_id"] != parent["record_id"] or int(successor["revision"]) != int(parent["revision"]) + 1:
            raise AdapterError("INVALID_ENVELOPE", "governed successor lineage is invalid")
        if self._normalized_fact(successor["fact"]) != self._normalized_fact(parent["fact"]):
            raise AdapterError("INVALID_ENVELOPE", "governed proof-only successor cannot change fact content")
        native_row = self.conn.execute(
            "SELECT id FROM memories WHERE content_hash=? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
            (parent["native_content_hash"],),
        ).fetchone()
        if native_row is None:
            raise AdapterError("BACKEND_CORRUPT", "parent native materialization is unavailable")
        embedding_row = self.conn.execute(
            "SELECT content_embedding,store FROM memory_embeddings WHERE rowid=? AND store=? LIMIT 1",
            (int(native_row[0]), self.embedding_store_key),
        ).fetchone()
        if embedding_row is None:
            raise AdapterError("PROFILE_MISMATCH", "parent embedding materialization is unavailable for current store")
        values = [
            successor["record_id"], successor["memory_id"], successor["revision"], successor.get("parent_record_id"),
            successor["scope"], successor.get("project_id"), successor.get("worktree_id"), successor.get("workflow_id"),
            successor.get("agent_id"), successor["memory_type"], successor["fact"], canonical_json(successor["source_provenance"]),
            successor.get("source_timestamp"), successor["created_at"], successor.get("last_verified_at"), successor["verified_by"],
            successor["authority_role"], canonical_json(successor.get("contribution_origin") or {"contributor_class": "LEGACY_UNKNOWN", "contributor_ref": None}),
            canonical_json(successor["freshness_policy"]), canonical_json(successor.get("supersedes", [])), successor["contradiction_key"],
            successor["contradiction_state"], successor["confidence"], canonical_json(successor.get("tags", [])), successor["lifecycle_state"],
            successor["native_content_hash"], successor["payload_fingerprint"], successor["idempotency_key"], successor["embedding_profile_id"],
            successor["embedding_model_digest"], successor["embedding_output_dimension"],
        ]
        self.conn.execute(
            f"INSERT INTO adapter_records ({','.join(_RECORD_COLUMNS)}) VALUES ({','.join('?' for _ in _RECORD_COLUMNS)})",
            values,
        )
        native_metadata = canonical_json(
            {
                "adapter_record_id": successor["record_id"],
                "adapter_memory_id": successor["memory_id"],
                "embedding_profile_id": successor["embedding_profile_id"],
                "lifecycle_state": successor["lifecycle_state"],
            }
        )
        native_values: dict[str, Any] = {
            "content_hash": successor["native_content_hash"],
            "content": successor["fact"],
            "tags": ",".join(successor.get("tags", [])),
            "memory_type": successor["memory_type"],
            "metadata": native_metadata,
            "created_at": parse_iso(successor["created_at"], required=True).timestamp(),
            "updated_at": parse_iso(successor["created_at"], required=True).timestamp(),
            "created_at_iso": successor["created_at"],
            "updated_at_iso": successor["created_at"],
            "store": self.embedding_store_key,
        }
        if "confidence" in self.native_columns:
            native_values["confidence"] = successor["confidence"]
        if "last_accessed" in self.native_columns:
            native_values["last_accessed"] = None
        native_fields = [field for field in native_values if field in self.native_columns]
        cursor = self.conn.execute(
            f"INSERT INTO memories ({','.join(native_fields)}) VALUES ({','.join('?' for _ in native_fields)})",
            [native_values[field] for field in native_fields],
        )
        if cursor.lastrowid is None:
            raise AdapterError("BACKEND_CORRUPT", "successor native row could not be materialized")
        self.conn.execute(
            "INSERT INTO memory_embeddings(rowid,content_embedding,store) VALUES (?,?,?)",
            (cursor.lastrowid, embedding_row[0], embedding_row[1]),
        )

    def _resolve_group_sync(
        self,
        scope: ScopeContext,
        *,
        contradiction_key: str,
        observed_group_token: str,
        winner_memory_id: str,
        winner_record_id: str,
        winner_expected_revision: int,
        idempotency_key: str,
        request_fingerprint: str,
        request_id: str,
        reason_code: str,
        actor_class: str,
        actor_ref_digest: str | None,
        grant_ref_digest: str,
        committed_at: str,
        candidate_successor: dict[str, Any] | None,
    ) -> dict[str, Any]:
        assert self.conn is not None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            replay = self._operation_row_sync(idempotency_key)
            if replay is not None:
                if replay["replay_status"] == "RECOVERY_INVALIDATED":
                    raise AdapterError("RECOVERY_INVALIDATED_REPLAY", "operation replay was invalidated by sensitive Recovery")
                if replay["request_fingerprint"] != request_fingerprint:
                    raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is bound to another governed operation")
                result = self._operation_result(replay, replay=True)
                self.conn.rollback()
                return result
            if self.conn.execute("SELECT 1 FROM adapter_records WHERE idempotency_key=? LIMIT 1", (idempotency_key,)).fetchone():
                raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is already reserved by another write")
            self._sample_trust_clock_sync(iso_now())
            self._sample_trust_clock_sync(committed_at)
            observed = self._group_observation_sync(scope, contradiction_key)
            if observed["public"]["observed_group_token"] != observed_group_token:
                raise AdapterError(
                    "GROUP_VERSION_CONFLICT",
                    "contradiction group changed after Owner observation",
                    refetch_required=True,
                    current_group_token=observed["public"]["observed_group_token"],
                    member_count=observed["public"]["member_count"],
                )
            if any(member["lifecycle_state"] in {"STALE", "QUARANTINED"} for member in observed["server"]["members"]):
                raise AdapterError("GROUP_MEMBER_NOT_ADMISSIBLE", "group contains a stale or quarantined blocking member")
            where, params = self._scope_where(scope)
            winner_row = self.conn.execute(
                self._select_sql(
                    f"WHERE a.contradiction_key=? AND a.memory_id=? AND a.record_id=? AND {where} LIMIT 1"
                ),
                [contradiction_key, winner_memory_id, winner_record_id, *params],
            ).fetchone()
            if winner_row is None:
                raise AdapterError("INVALID_WINNER", "winner is not a member of the observed contradiction group")
            winner = self._row_to_dict(winner_row)
            if int(winner["revision"]) != winner_expected_revision:
                raise AdapterError("VERSION_CONFLICT", "winner expected_revision no longer matches")
            if winner["lifecycle_state"] not in {"VALIDATED_CURRENT", "CANDIDATE"}:
                raise AdapterError("INVALID_WINNER", "winner lifecycle is not admissible")
            if winner["lifecycle_state"] == "VALIDATED_CURRENT":
                gate = self._latest_freshness_event_sync(winner_record_id, winner_expected_revision)["freshness_gate_state"]
                if gate != "NONE":
                    raise AdapterError("GROUP_MEMBER_NOT_ADMISSIBLE", "freshness-held Current cannot be retained as RESOLVE winner")
                if candidate_successor is not None:
                    raise AdapterError("WINNER_PROOF_INVALID", "existing Current winner does not accept candidate verification payload")
            else:
                if candidate_successor is None:
                    raise AdapterError("WINNER_PROOF_REQUIRED", "Candidate RESOLVE winner requires complete fresh verification")
                if (
                    candidate_successor.get("memory_id") != winner["memory_id"]
                    or candidate_successor.get("parent_record_id") != winner["record_id"]
                    or int(candidate_successor.get("revision", 0)) != int(winner["revision"]) + 1
                    or candidate_successor.get("contradiction_key") != contradiction_key
                    or candidate_successor.get("lifecycle_state") != "VALIDATED_CURRENT"
                    or candidate_successor.get("contradiction_state") != "NONE"
                ):
                    raise AdapterError("WINNER_PROOF_INVALID", "Candidate winner successor identity is invalid")
                for field, value in (
                    ("scope", scope.scope),
                    ("project_id", scope.project_id),
                    ("worktree_id", scope.worktree_id),
                    ("workflow_id", scope.workflow_id),
                    ("agent_id", scope.agent_id),
                ):
                    if candidate_successor.get(field) != value:
                        raise AdapterError("WINNER_PROOF_INVALID", "Candidate winner successor changed exact scope")
            operation_id = str(uuid.uuid4())
            affected: list[dict[str, Any]] = []
            event_ids: list[str] = []

            active_rows = [
                self._row_to_dict(row)
                for row in self.conn.execute(
                    self._select_sql(
                        f"WHERE a.contradiction_key=? AND {where} AND a.lifecycle_state IN ('VALIDATED_CURRENT','CANDIDATE') ORDER BY a.record_id"
                    ),
                    [contradiction_key, *params],
                ).fetchall()
            ]
            current_rows = [row for row in active_rows if row["lifecycle_state"] == "VALIDATED_CURRENT"]
            if len(current_rows) > 1:
                raise AdapterError("LIFECYCLE_INVARIANT_VIOLATION", "RESOLVE cannot proceed with multiple Current rows")
            if winner["lifecycle_state"] == "CANDIDATE":
                assert candidate_successor is not None
                self._require_transactional_freshness_sync(
                    candidate_successor,
                    error_code="WINNER_PROOF_INVALID",
                    safe_message="Candidate winner verification expired before Current admission",
                )

            result_record_id = winner["record_id"]
            result_revision = int(winner["revision"])
            winner_parent_record_id = winner.get("parent_record_id")
            if winner["lifecycle_state"] == "CANDIDATE":
                assert candidate_successor is not None
                for current in current_rows:
                    prior_contradiction = current["contradiction_state"]
                    self.conn.execute(
                        "UPDATE adapter_records SET lifecycle_state='SUPERSEDED',contradiction_state='RESOLVED' WHERE record_id=?",
                        (current["record_id"],),
                    )
                    event_id = str(uuid.uuid4())
                    self._insert_lifecycle_event_sync(
                        event_id=event_id,
                        operation_id=operation_id,
                        record=current,
                        action="RESOLVE_SUPERSEDE_CURRENT",
                        actor_class=actor_class,
                        actor_ref_digest=actor_ref_digest,
                        grant_ref_digest=grant_ref_digest,
                        occurred_at=committed_at,
                        prior_lifecycle_state="VALIDATED_CURRENT",
                        result_lifecycle_state="SUPERSEDED",
                        prior_contradiction_state=prior_contradiction,
                        result_contradiction_state="RESOLVED",
                        reason_code=reason_code,
                        expected_revision=int(current["revision"]),
                        result_revision=int(current["revision"]),
                        related_record_id=candidate_successor["record_id"],
                        related_memory_id=candidate_successor["memory_id"],
                    )
                    event_ids.append(event_id)
                    affected.append(
                        {
                            "memory_id": current["memory_id"],
                            "record_id": current["record_id"],
                            "from_lifecycle_state": "VALIDATED_CURRENT",
                            "to_lifecycle_state": "SUPERSEDED",
                            "from_contradiction_state": prior_contradiction,
                            "to_contradiction_state": "RESOLVED",
                        }
                    )
                prior_winner_contradiction = winner["contradiction_state"]
                self.conn.execute(
                    "UPDATE adapter_records SET lifecycle_state='SUPERSEDED',contradiction_state='RESOLVED' WHERE record_id=?",
                    (winner["record_id"],),
                )
                self._copy_materialized_successor_sync(winner, candidate_successor)
                winner_event = str(uuid.uuid4())
                self._insert_lifecycle_event_sync(
                    event_id=winner_event,
                    operation_id=operation_id,
                    record=winner,
                    action="RESOLVE_PROMOTE_CANDIDATE",
                    actor_class=actor_class,
                    actor_ref_digest=actor_ref_digest,
                    grant_ref_digest=grant_ref_digest,
                    occurred_at=committed_at,
                    prior_lifecycle_state="CANDIDATE",
                    result_lifecycle_state="SUPERSEDED",
                    prior_contradiction_state=prior_winner_contradiction,
                    result_contradiction_state="RESOLVED",
                    reason_code=reason_code,
                    expected_revision=int(winner["revision"]),
                    result_revision=int(candidate_successor["revision"]),
                    related_record_id=candidate_successor["record_id"],
                    related_memory_id=candidate_successor["memory_id"],
                )
                event_ids.append(winner_event)
                affected.append(
                    {
                        "memory_id": winner["memory_id"],
                        "record_id": winner["record_id"],
                        "from_lifecycle_state": "CANDIDATE",
                        "to_lifecycle_state": "SUPERSEDED",
                        "from_contradiction_state": prior_winner_contradiction,
                        "to_contradiction_state": "RESOLVED",
                    }
                )
                result_record_id = candidate_successor["record_id"]
                result_revision = int(candidate_successor["revision"])
                winner_parent_record_id = winner["record_id"]
            else:
                retain_event = str(uuid.uuid4())
                self._insert_lifecycle_event_sync(
                    event_id=retain_event,
                    operation_id=operation_id,
                    record=winner,
                    action="RESOLVE_RETAIN_CURRENT",
                    actor_class=actor_class,
                    actor_ref_digest=actor_ref_digest,
                    grant_ref_digest=grant_ref_digest,
                    occurred_at=committed_at,
                    prior_lifecycle_state="VALIDATED_CURRENT",
                    result_lifecycle_state="VALIDATED_CURRENT",
                    prior_contradiction_state=winner["contradiction_state"],
                    result_contradiction_state=winner["contradiction_state"],
                    reason_code=reason_code,
                    expected_revision=int(winner["revision"]),
                    result_revision=int(winner["revision"]),
                )
                event_ids.append(retain_event)

            for loser in active_rows:
                if loser["record_id"] == winner["record_id"] or loser["lifecycle_state"] != "CANDIDATE":
                    continue
                prior = loser["contradiction_state"]
                self.conn.execute(
                    "UPDATE adapter_records SET lifecycle_state='REJECTED',contradiction_state='RESOLVED' WHERE record_id=?",
                    (loser["record_id"],),
                )
                event_id = str(uuid.uuid4())
                self._insert_lifecycle_event_sync(
                    event_id=event_id,
                    operation_id=operation_id,
                    record=loser,
                    action="RESOLVE_REJECT_CANDIDATE",
                    actor_class=actor_class,
                    actor_ref_digest=actor_ref_digest,
                    grant_ref_digest=grant_ref_digest,
                    occurred_at=committed_at,
                    prior_lifecycle_state="CANDIDATE",
                    result_lifecycle_state="REJECTED",
                    prior_contradiction_state=prior,
                    result_contradiction_state="RESOLVED",
                    reason_code=reason_code,
                    expected_revision=int(loser["revision"]),
                    result_revision=int(loser["revision"]),
                    related_record_id=result_record_id,
                    related_memory_id=winner["memory_id"],
                )
                event_ids.append(event_id)
                affected.append(
                    {
                        "memory_id": loser["memory_id"],
                        "record_id": loser["record_id"],
                        "from_lifecycle_state": "CANDIDATE",
                        "to_lifecycle_state": "REJECTED",
                        "from_contradiction_state": prior,
                        "to_contradiction_state": "RESOLVED",
                    }
                )
            resulting_group = self._group_observation_sync(scope, contradiction_key)["public"]
            self.conn.execute(
                "INSERT INTO lifecycle_operations("
                "idempotency_key,request_fingerprint,operation_id,request_id,operation_kind,actor_class,actor_ref_digest,"
                "grant_ref_digest,scope_digest,target_memory_id,target_record_id,winner_memory_id,winner_record_id,"
                "winner_parent_record_id,result_record_id,result_revision,result_lifecycle_state,result_contradiction_state,"
                "affected_members_json,event_ids_json,result_group_token,result_group_count,replay_status,commit_timestamp"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    idempotency_key,
                    request_fingerprint,
                    operation_id,
                    request_id,
                    "RESOLVE",
                    actor_class,
                    actor_ref_digest,
                    grant_ref_digest,
                    hashlib.sha256(scope.key().encode("utf-8")).hexdigest(),
                    winner_memory_id,
                    winner_record_id,
                    winner_memory_id,
                    winner_record_id,
                    winner_parent_record_id,
                    result_record_id,
                    result_revision,
                    "VALIDATED_CURRENT",
                    "NONE",
                    canonical_json(affected),
                    canonical_json(event_ids),
                    resulting_group["observed_group_token"],
                    resulting_group["member_count"],
                    "COMMITTED",
                    committed_at,
                ),
            )
            operation = self._operation_row_sync(idempotency_key)
            assert operation is not None
            self.conn.commit()
            return self._operation_result(operation, replay=False)
        except BaseException:
            self.conn.rollback()
            raise

    async def resolve_group(self, scope: ScopeContext, **values: Any) -> dict[str, Any]:
        return await self._run(lambda: self._resolve_group_sync(scope, **values))

    def _require_transactional_freshness_sync(
        self,
        record: dict[str, Any],
        *,
        error_code: str,
        safe_message: str,
    ) -> None:
        """Recheck Current-admission freshness at the serialized write boundary."""
        assert self.conn is not None
        _, effective_now = self._sample_trust_clock_sync(iso_now())
        if is_fresh(
            record["freshness_policy"],
            record.get("last_verified_at"),
            record["source_provenance"],
            now=parse_iso(effective_now, required=True),
        ):
            return
        # No lifecycle/native/vector/adjudication write is allowed before this
        # point. Preserve only the monotonic trust-clock safety observation so a
        # later wall-clock rollback cannot resurrect proof already seen expired.
        self.conn.commit()
        raise AdapterError(error_code, safe_message)

    def _sample_trust_clock_sync(self, sampled_at: str) -> tuple[bool, str]:
        """Return clock health and monotonically maintain the durable audit high-water mark."""
        assert self.conn is not None
        sample = parse_iso(sampled_at, required=True)
        row = self.conn.execute(
            "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
        ).fetchone()
        highwater_text = str(row[0]) if row is not None else "1970-01-01T00:00:00Z"
        highwater = parse_iso(highwater_text, required=True)
        healthy = sample >= highwater
        if sample > highwater:
            self.conn.execute(
                "INSERT INTO adapter_meta(key,value) VALUES ('trust_clock_high_water_at',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (sampled_at,),
            )
            highwater_text = sampled_at
        return healthy, highwater_text

    def _insert_manual_recheck_operation_sync(
        self,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        operation_id: str,
        request_id: str,
        actor_class: str,
        actor_ref_digest: str | None,
        grant_ref_digest: str,
        scope: ScopeContext,
        target: dict[str, Any],
        outcome: str | None,
        event_ids: list[str],
        result_record_id: str | None,
        result_revision: int | None,
        result_group_token: str | None,
        result_group_count: int | None,
        replay_status: str,
        accepted_at: str,
        observed_at: str,
        decision_error: str | None,
        accepted_event_freshness_generation: int | None,
        resulting_member_freshness_generation: int | None,
    ) -> None:
        assert self.conn is not None
        self.conn.execute(
            "INSERT INTO lifecycle_operations("
            "idempotency_key,request_fingerprint,operation_id,request_id,operation_kind,actor_class,actor_ref_digest,"
            "grant_ref_digest,scope_digest,target_memory_id,target_record_id,result_record_id,"
            "result_revision,result_lifecycle_state,result_outcome,observed_at,affected_members_json,event_ids_json,"
            "result_group_token,result_group_count,replay_status,commit_timestamp,decision_error,decision_at,"
            "accepted_event_freshness_generation,resulting_member_freshness_generation"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                idempotency_key,
                request_fingerprint,
                operation_id,
                request_id,
                "MANUAL_RECHECK",
                actor_class,
                actor_ref_digest,
                grant_ref_digest,
                hashlib.sha256(scope.key().encode("utf-8")).hexdigest(),
                target["memory_id"],
                target["record_id"],
                result_record_id,
                result_revision,
                target["lifecycle_state"] if result_record_id == target["record_id"] else "VALIDATED_CURRENT" if result_record_id else None,
                outcome,
                observed_at,
                "[]",
                canonical_json(event_ids),
                result_group_token,
                result_group_count,
                replay_status,
                accepted_at,
                decision_error,
                accepted_at if decision_error else None,
                accepted_event_freshness_generation,
                resulting_member_freshness_generation,
            ),
        )

    def _manual_recheck_sync(
        self,
        scope: ScopeContext,
        *,
        memory_id: str,
        expected_record_id: str,
        expected_revision: int,
        observed_group_token: str,
        idempotency_key: str,
        request_fingerprint: str,
        request_id: str,
        claim: str,
        recheck_ref: str,
        observed_at: str,
        observed_source_hash: str | None,
        evidence_ref_digest: str | None,
        method: str,
        actor_class: str,
        actor_ref_digest: str | None,
        grant_ref_digest: str,
    ) -> dict[str, Any]:
        assert self.conn is not None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            replay = self._operation_row_sync(idempotency_key)
            if replay is not None:
                if replay["replay_status"] == "RECOVERY_INVALIDATED":
                    raise AdapterError("RECOVERY_INVALIDATED_REPLAY", "operation replay was invalidated by sensitive Recovery")
                if replay["request_fingerprint"] != request_fingerprint:
                    raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is bound to another governed operation")
                result = self._operation_result(replay, replay=True)
                self.conn.rollback()
                return result
            if self.conn.execute("SELECT 1 FROM adapter_records WHERE idempotency_key=? LIMIT 1", (idempotency_key,)).fetchone():
                raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is already reserved by another write")
            where, params = self._scope_where(scope)
            row = self.conn.execute(
                self._select_sql(
                    f"WHERE a.memory_id=? AND a.record_id=? AND a.lifecycle_state='VALIDATED_CURRENT' AND {where} LIMIT 1"
                ),
                [memory_id, expected_record_id, *params],
            ).fetchone()
            if row is None:
                raise AdapterError("VERSION_CONFLICT", "manual recheck target is no longer the exact Current revision")
            target = self._row_to_dict(row)
            if int(target["revision"]) != expected_revision:
                raise AdapterError("VERSION_CONFLICT", "manual recheck expected_revision no longer matches")
            observed_group = self._group_observation_sync(scope, target["contradiction_key"])
            if observed_group["public"]["observed_group_token"] != observed_group_token:
                raise AdapterError(
                    "GROUP_VERSION_CONFLICT",
                    "contradiction group changed after Owner observation",
                    refetch_required=True,
                    current_group_token=observed_group["public"]["observed_group_token"],
                )
            freshness = json.loads(target["freshness_policy"])
            provenance = json.loads(target["source_provenance"])
            if freshness.get("kind") != "on_change":
                raise AdapterError("NOT_ON_CHANGE", "manual recheck requires an on_change Current")
            if freshness.get("recheck_ref") != recheck_ref:
                raise AdapterError("EVIDENCE_BINDING_MISMATCH", "recheck_ref does not match the Current policy")
            if freshness.get("source_fingerprint") != provenance.get("source_hash"):
                raise AdapterError("EVIDENCE_BINDING_MISMATCH", "stored source fingerprint no longer matches Current provenance")
            observed_dt = parse_iso(observed_at, required=True)
            last_verified_dt = parse_iso(target["last_verified_at"], required=True)
            latest_observation = self.conn.execute(
                "SELECT observed_at FROM lifecycle_events WHERE target_record_id=? AND target_revision=? "
                "AND freshness_generation IS NOT NULL AND observed_at IS NOT NULL "
                "ORDER BY freshness_generation DESC LIMIT 1",
                (target["record_id"], int(target["revision"])),
            ).fetchone()
            latest_dt = parse_iso(str(latest_observation[0]), required=True) if latest_observation else last_verified_dt
            if observed_dt <= last_verified_dt or observed_dt <= latest_dt:
                raise AdapterError("EVIDENCE_STALE", "observed_at is not newer than accepted verification/recheck evidence")
            accepted_at = iso_now()
            accepted_dt = parse_iso(accepted_at, required=True)
            clock_healthy, _ = self._sample_trust_clock_sync(accepted_at)
            delta_seconds = (accepted_dt - observed_dt).total_seconds()
            if claim == "OBSERVED_HASH":
                if not isinstance(observed_source_hash, str) or len(observed_source_hash) != 64:
                    raise AdapterError("EVIDENCE_INVALID", "OBSERVED_HASH requires a SHA-256 observed_source_hash")
                outcome = "UNCHANGED" if observed_source_hash.lower() == str(provenance.get("source_hash", "")).lower() else "CHANGED"
            elif claim in {"UNAVAILABLE", "INDETERMINATE"}:
                if observed_source_hash is not None:
                    raise AdapterError("EVIDENCE_INVALID", f"{claim} must not include observed_source_hash")
                outcome = claim
            else:
                raise AdapterError("EVIDENCE_INVALID", "manual recheck claim is invalid")
            operation_id = str(uuid.uuid4())

            def durable_reject(error: str) -> dict[str, Any]:
                self._insert_manual_recheck_operation_sync(
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    operation_id=operation_id,
                    request_id=request_id,
                    actor_class=actor_class,
                    actor_ref_digest=actor_ref_digest,
                    grant_ref_digest=grant_ref_digest,
                    scope=scope,
                    target=target,
                    outcome=None,
                    event_ids=[],
                    result_record_id=None,
                    result_revision=None,
                    result_group_token=None,
                    result_group_count=None,
                    replay_status="BLOCKED",
                    accepted_at=accepted_at,
                    observed_at=observed_at,
                    decision_error=error,
                    accepted_event_freshness_generation=None,
                    resulting_member_freshness_generation=None,
                )
                op = self._operation_row_sync(idempotency_key)
                assert op is not None
                self.conn.commit()
                return self._operation_result(op, replay=False)

            if outcome == "UNCHANGED":
                if clock_healthy and delta_seconds < -MAX_FUTURE_EVIDENCE_SKEW_SECONDS:
                    raise AdapterError("EVIDENCE_INVALID", "observed_at exceeds the healthy-clock future-skew bound")
                if not clock_healthy:
                    return durable_reject("TRUST_CLOCK_UNRELIABLE")
                if delta_seconds < 0:
                    return durable_reject("EVIDENCE_NOT_YET_OBSERVED")
                if delta_seconds > MAX_TRUST_RENEWAL_EVIDENCE_AGE_SECONDS:
                    return durable_reject("EVIDENCE_TOO_OLD_FOR_TRUST_RENEWAL")
            elif clock_healthy and delta_seconds < -MAX_FUTURE_EVIDENCE_SKEW_SECONDS:
                raise AdapterError("EVIDENCE_INVALID", "adverse observed_at exceeds the healthy-clock future-skew bound")

            generation_row = self.conn.execute(
                "SELECT MAX(freshness_generation) FROM lifecycle_events WHERE target_record_id=? AND target_revision=?",
                (target["record_id"], int(target["revision"])),
            ).fetchone()
            latest_generation = int(generation_row[0] or 0)
            if latest_generation >= 9_223_372_036_854_775_807:
                raise AdapterError("FRESHNESS_SEQUENCE_EXHAUSTED", "freshness generation cannot be incremented")
            generation = latest_generation + 1
            clock_anomaly = "NONE" if clock_healthy or delta_seconds >= -MAX_FUTURE_EVIDENCE_SKEW_SECONDS else "CLOCK_UNTRUSTED_TIME"
            event_id = str(uuid.uuid4())
            result_record_id = target["record_id"]
            result_revision = int(target["revision"])
            resulting_member_generation = generation
            if outcome == "UNCHANGED":
                successor_id = str(uuid.uuid4())
                successor = dict(target)
                successor.update(
                    {
                        "record_id": successor_id,
                        "revision": int(target["revision"]) + 1,
                        "parent_record_id": target["record_id"],
                        "created_at": accepted_at,
                        "last_verified_at": observed_at,
                        "verified_by": "OWNER",
                        "lifecycle_state": "VALIDATED_CURRENT",
                        "contradiction_state": target["contradiction_state"],
                        "native_content_hash": native_hash(scope, target["fact"], revision_key=successor_id),
                        "payload_fingerprint": request_fingerprint,
                        "idempotency_key": f"internal-recheck-{operation_id}",
                    }
                )
                successor_provenance = dict(provenance)
                if evidence_ref_digest is not None:
                    successor_provenance["review_ref"] = f"evidence:{evidence_ref_digest[:24]}"
                successor_provenance["extraction_method"] = method
                successor["source_provenance"] = successor_provenance
                self.conn.execute(
                    "UPDATE adapter_records SET lifecycle_state='SUPERSEDED' WHERE record_id=?",
                    (target["record_id"],),
                )
                self._copy_materialized_successor_sync(target, successor)
                if "superseded_by" in self.native_columns:
                    self.conn.execute(
                        "UPDATE memories SET superseded_by=? WHERE content_hash=?",
                        (successor["native_content_hash"], target["native_content_hash"]),
                    )
                result_record_id = successor_id
                result_revision = int(successor["revision"])
                resulting_member_generation = 0
            self._insert_lifecycle_event_sync(
                event_id=event_id,
                operation_id=operation_id,
                record=target,
                action="MANUAL_RECHECK",
                actor_class=actor_class,
                actor_ref_digest=actor_ref_digest,
                grant_ref_digest=grant_ref_digest,
                occurred_at=accepted_at,
                prior_lifecycle_state="VALIDATED_CURRENT",
                result_lifecycle_state="SUPERSEDED" if outcome == "UNCHANGED" else "VALIDATED_CURRENT",
                prior_contradiction_state=target["contradiction_state"],
                result_contradiction_state=target["contradiction_state"],
                expected_revision=expected_revision,
                result_revision=result_revision,
                related_record_id=result_record_id if result_record_id != target["record_id"] else None,
                related_memory_id=memory_id,
                freshness_outcome=outcome,
                observation_origin="OWNER_SUPPLIED",
                observed_at=observed_at,
                accepted_at=accepted_at,
                evidence_ref_digest=evidence_ref_digest,
                freshness_generation=generation,
                clock_anomaly=clock_anomaly,
            )
            resulting_group = self._group_observation_sync(scope, target["contradiction_key"])["public"]
            self._insert_manual_recheck_operation_sync(
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                operation_id=operation_id,
                request_id=request_id,
                actor_class=actor_class,
                actor_ref_digest=actor_ref_digest,
                grant_ref_digest=grant_ref_digest,
                scope=scope,
                target=target,
                outcome=outcome,
                event_ids=[event_id],
                result_record_id=result_record_id,
                result_revision=result_revision,
                result_group_token=resulting_group["observed_group_token"],
                result_group_count=resulting_group["member_count"],
                replay_status="COMMITTED",
                accepted_at=accepted_at,
                observed_at=observed_at,
                decision_error=None,
                accepted_event_freshness_generation=generation,
                resulting_member_freshness_generation=resulting_member_generation,
            )
            operation = self._operation_row_sync(idempotency_key)
            assert operation is not None
            self.conn.commit()
            return self._operation_result(operation, replay=False)
        except BaseException:
            self.conn.rollback()
            raise

    async def manual_recheck(self, scope: ScopeContext, **values: Any) -> dict[str, Any]:
        return await self._run(lambda: self._manual_recheck_sync(scope, **values))

    def _readmit_sync(
        self,
        scope: ScopeContext,
        *,
        memory_id: str,
        revoked_record_id: str,
        expected_revoked_revision: int,
        observed_group_token: str,
        idempotency_key: str,
        request_fingerprint: str,
        request_id: str,
        verified_successor: dict[str, Any],
        actor_class: str,
        actor_ref_digest: str | None,
        grant_ref_digest: str,
        committed_at: str,
    ) -> dict[str, Any]:
        assert self.conn is not None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            replay = self._operation_row_sync(idempotency_key)
            if replay is not None:
                if replay["replay_status"] == "RECOVERY_INVALIDATED":
                    raise AdapterError("RECOVERY_INVALIDATED_REPLAY", "operation replay was invalidated by sensitive Recovery")
                if replay["request_fingerprint"] != request_fingerprint:
                    raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is bound to another governed operation")
                result = self._operation_result(replay, replay=True)
                self.conn.rollback()
                return result
            if self.conn.execute("SELECT 1 FROM adapter_records WHERE idempotency_key=? LIMIT 1", (idempotency_key,)).fetchone():
                raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is already reserved by another write")

            self._sample_trust_clock_sync(iso_now())
            self._sample_trust_clock_sync(committed_at)

            where, params = self._scope_where(scope)
            target_row = self.conn.execute(
                self._select_sql(f"WHERE a.memory_id=? AND a.record_id=? AND {where} LIMIT 1"),
                [memory_id, revoked_record_id, *params],
            ).fetchone()
            if target_row is None:
                outside = self.conn.execute(
                    f"SELECT 1 FROM adapter_records a WHERE a.memory_id=? AND NOT ({where}) LIMIT 1",
                    [memory_id, *params],
                ).fetchone()
                if outside is not None:
                    raise AdapterError("CROSS_SCOPE_DENIED", "readmit target exists outside the requested scope")
                raise AdapterError("NOT_REVOKED", "readmit target was not found")
            target = self._row_to_dict(target_row)
            if target["lifecycle_state"] == "QUARANTINED":
                raise AdapterError("READMIT_RECOVERY_PURGED_DENIED", "Recovery-quarantined target cannot be readmitted")
            if target["lifecycle_state"] != "REVOKED":
                raise AdapterError("NOT_REVOKED", "readmit target is not REVOKED")
            if int(target["revision"]) != expected_revoked_revision:
                raise AdapterError("VERSION_CONFLICT", "readmit expected_revoked_revision no longer matches")

            lineage_rows = [
                self._row_to_dict(row)
                for row in self.conn.execute(
                    self._select_sql("WHERE a.memory_id=? ORDER BY a.revision,a.created_at,a.record_id"),
                    (memory_id,),
                ).fetchall()
            ]
            higher = [row for row in lineage_rows if int(row["revision"]) > expected_revoked_revision]
            # Parent linkage is global: REPLACE may create a successor with a
            # different memory_id. Same-memory lineage alone is insufficient.
            direct_children = [
                self._row_to_dict(row)
                for row in self.conn.execute(
                    self._select_sql("WHERE a.parent_record_id=? ORDER BY a.record_id"),
                    (revoked_record_id,),
                ).fetchall()
            ]
            if higher or direct_children:
                if any(row["lifecycle_state"] == "QUARANTINED" for row in [*higher, *direct_children]):
                    raise AdapterError(
                        "READMIT_RECOVERY_PURGED_DENIED",
                        "Recovery-quarantined descendant permanently blocks older revoked readmission",
                    )
                raise AdapterError("LINEAGE_CHANGED", "revoked target is no longer the unique latest lineage tip")
            latest_revision = max(int(row["revision"]) for row in lineage_rows)
            if latest_revision != expected_revoked_revision:
                raise AdapterError("LINEAGE_CHANGED", "revoked target is not the unique latest lineage tip")

            observed = self._group_observation_sync(scope, target["contradiction_key"])
            if observed["public"]["observed_group_token"] != observed_group_token:
                raise AdapterError(
                    "GROUP_VERSION_CONFLICT",
                    "contradiction group changed after Owner observation",
                    refetch_required=True,
                    current_group_token=observed["public"]["observed_group_token"],
                )
            if any(
                member["lifecycle_state"] in {"STALE", "QUARANTINED"}
                or (member["lifecycle_state"] == "VALIDATED_CURRENT" and member["freshness_gate_state"] != "NONE")
                for member in observed["server"]["members"]
            ):
                raise AdapterError("GROUP_MEMBER_NOT_ADMISSIBLE", "blocking group member prevents readmission")

            if (
                verified_successor.get("memory_id") != memory_id
                or verified_successor.get("parent_record_id") != revoked_record_id
                or int(verified_successor.get("revision", 0)) != expected_revoked_revision + 1
                or verified_successor.get("memory_type") != target["memory_type"]
                or verified_successor.get("contradiction_key") != target["contradiction_key"]
                or self._normalized_fact(str(verified_successor.get("fact", "")))
                != self._normalized_fact(target["fact"])
            ):
                raise AdapterError("SEMANTIC_IDENTITY_MISMATCH", "readmit successor changes logical/semantic identity")
            for field, value in (
                ("scope", scope.scope),
                ("project_id", scope.project_id),
                ("worktree_id", scope.worktree_id),
                ("workflow_id", scope.workflow_id),
                ("agent_id", scope.agent_id),
            ):
                if verified_successor.get(field) != value:
                    raise AdapterError("SEMANTIC_IDENTITY_MISMATCH", "readmit successor changes exact scope")
            if verified_successor.get("verified_by") != "OWNER":
                raise AdapterError("VERIFICATION_REQUIRED", "readmit requires fresh Owner verification")
            parse_iso(verified_successor.get("source_timestamp"), required=True)
            parse_iso(verified_successor.get("last_verified_at"), required=True)
            native = self.conn.execute(
                "SELECT id FROM memories WHERE content_hash=? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
                (target["native_content_hash"],),
            ).fetchone()
            if native is None:
                raise AdapterError("READMIT_RECOVERY_PURGED_DENIED", "readmit target payload was physically purged")
            self._require_transactional_freshness_sync(
                verified_successor,
                error_code="VERIFICATION_REQUIRED",
                safe_message="readmit verification expired before Current admission",
            )

            active_rows = [
                self._row_to_dict(row)
                for row in self.conn.execute(
                    self._select_sql(
                        f"WHERE a.contradiction_key=? AND {where} "
                        "AND a.lifecycle_state IN ('VALIDATED_CURRENT','CANDIDATE','STALE','QUARANTINED') "
                        "ORDER BY a.record_id"
                    ),
                    [target["contradiction_key"], *params],
                ).fetchall()
            ]
            target_fact = self._normalized_fact(target["fact"])
            equivalent_current = next(
                (
                    row for row in active_rows
                    if row["lifecycle_state"] == "VALIDATED_CURRENT"
                    and self._normalized_fact(row["fact"]) == target_fact
                ),
                None,
            )
            equivalent_candidate = next(
                (
                    row for row in active_rows
                    if row["lifecycle_state"] == "CANDIDATE"
                    and self._normalized_fact(row["fact"]) == target_fact
                ),
                None,
            )
            operation_id = str(uuid.uuid4())
            event_ids: list[str] = []
            result_status = "OK"
            result_record_id: str | None = None
            result_revision: int | None = None
            result_lifecycle: str | None = None
            result_contradiction: str | None = None

            if equivalent_current is not None:
                result_status = "ALREADY_CURRENT_EQUIVALENT"
            elif equivalent_candidate is not None:
                result_status = "ALREADY_CANDIDATE_EQUIVALENT"
            else:
                incompatible = [row for row in active_rows if self._normalized_fact(row["fact"]) != target_fact]
                successor = dict(verified_successor)
                if incompatible:
                    successor["lifecycle_state"] = "CANDIDATE"
                    successor["contradiction_state"] = "UNRESOLVED"
                    successor["authority_role"] = "UNRESOLVED"
                    result_lifecycle = "CANDIDATE"
                    result_contradiction = "UNRESOLVED"
                else:
                    successor["lifecycle_state"] = "VALIDATED_CURRENT"
                    successor["contradiction_state"] = "NONE"
                    result_lifecycle = "VALIDATED_CURRENT"
                    result_contradiction = "NONE"
                self._copy_materialized_successor_sync(target, successor)
                result_record_id = str(successor["record_id"])
                result_revision = int(successor["revision"])
                event_id = str(uuid.uuid4())
                self._insert_lifecycle_event_sync(
                    event_id=event_id,
                    operation_id=operation_id,
                    record=target,
                    action="READMIT",
                    actor_class=actor_class,
                    actor_ref_digest=actor_ref_digest,
                    grant_ref_digest=grant_ref_digest,
                    occurred_at=committed_at,
                    prior_lifecycle_state="REVOKED",
                    result_lifecycle_state=result_lifecycle,
                    prior_contradiction_state=target["contradiction_state"],
                    result_contradiction_state=result_contradiction,
                    expected_revision=expected_revoked_revision,
                    result_revision=result_revision,
                    related_record_id=result_record_id,
                    related_memory_id=memory_id,
                )
                event_ids.append(event_id)
                if result_lifecycle == "CANDIDATE":
                    _, recompute_events = self._recompute_candidate_contradictions_sync(
                        scope,
                        target["contradiction_key"],
                        operation_id=operation_id,
                        actor_class=actor_class,
                        actor_ref_digest=actor_ref_digest,
                        grant_ref_digest=grant_ref_digest,
                        occurred_at=committed_at,
                    )
                    event_ids.extend(recompute_events)

            resulting_group = self._group_observation_sync(scope, target["contradiction_key"])["public"]
            self.conn.execute(
                "INSERT INTO lifecycle_operations("
                "idempotency_key,request_fingerprint,operation_id,request_id,operation_kind,actor_class,actor_ref_digest,"
                "grant_ref_digest,scope_digest,target_memory_id,target_record_id,result_record_id,result_revision,"
                "result_lifecycle_state,result_contradiction_state,result_outcome,affected_members_json,event_ids_json,"
                "result_group_token,result_group_count,replay_status,commit_timestamp"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    idempotency_key,
                    request_fingerprint,
                    operation_id,
                    request_id,
                    "READMIT",
                    actor_class,
                    actor_ref_digest,
                    grant_ref_digest,
                    hashlib.sha256(scope.key().encode("utf-8")).hexdigest(),
                    memory_id,
                    revoked_record_id,
                    result_record_id,
                    result_revision,
                    result_lifecycle,
                    result_contradiction,
                    result_status,
                    "[]",
                    canonical_json(event_ids),
                    resulting_group["observed_group_token"],
                    resulting_group["member_count"],
                    "COMMITTED",
                    committed_at,
                ),
            )
            operation = self._operation_row_sync(idempotency_key)
            assert operation is not None
            self.conn.commit()
            return self._operation_result(operation, replay=False)
        except BaseException:
            self.conn.rollback()
            raise

    async def readmit(self, scope: ScopeContext, **values: Any) -> dict[str, Any]:
        return await self._run(lambda: self._readmit_sync(scope, **values))

    def _insert_materialized_record(self, record: dict[str, Any], embedding: list[float]) -> None:
        """Insert one adapter/native/vector record inside an existing transaction."""
        assert self.conn is not None
        values = [
            record["record_id"], record["memory_id"], record["revision"], record.get("parent_record_id"),
            record["scope"], record.get("project_id"), record.get("worktree_id"), record.get("workflow_id"),
            record.get("agent_id"), record["memory_type"], record["fact"], canonical_json(record["source_provenance"]),
            record.get("source_timestamp"), record["created_at"], record.get("last_verified_at"), record["verified_by"],
            record["authority_role"], canonical_json(record.get("contribution_origin") or {"contributor_class": "LEGACY_UNKNOWN", "contributor_ref": None}),
            canonical_json(record["freshness_policy"]), canonical_json(record.get("supersedes", [])), record["contradiction_key"],
            record["contradiction_state"], record["confidence"], canonical_json(record.get("tags", [])), record["lifecycle_state"],
            record["native_content_hash"], record["payload_fingerprint"], record["idempotency_key"], record["embedding_profile_id"],
            record["embedding_model_digest"], record["embedding_output_dimension"],
        ]
        self.conn.execute(
            f"INSERT INTO adapter_records ({','.join(_RECORD_COLUMNS)}) VALUES ({','.join('?' for _ in _RECORD_COLUMNS)})",
            values,
        )
        native_metadata = canonical_json({
            "adapter_record_id": record["record_id"], "adapter_memory_id": record["memory_id"],
            "embedding_profile_id": record["embedding_profile_id"], "lifecycle_state": record["lifecycle_state"],
        })
        native_values: dict[str, Any] = {
            "content_hash": record["native_content_hash"], "content": record["fact"], "tags": ",".join(record.get("tags", [])),
            "memory_type": record["memory_type"], "metadata": native_metadata,
            "created_at": parse_iso(record["created_at"], required=True).timestamp(),
            "updated_at": parse_iso(record["created_at"], required=True).timestamp(),
            "created_at_iso": record["created_at"], "updated_at_iso": record["created_at"], "store": self.embedding_store_key,
        }
        if "confidence" in self.native_columns:
            native_values["confidence"] = record["confidence"]
        if "last_accessed" in self.native_columns:
            native_values["last_accessed"] = None
        native_fields = [field for field in native_values if field in self.native_columns]
        cursor = self.conn.execute(
            f"INSERT INTO memories ({','.join(native_fields)}) VALUES ({','.join('?' for _ in native_fields)})",
            [native_values[field] for field in native_fields],
        )
        if cursor.lastrowid is None:
            raise sqlite3.IntegrityError("native memory rowid was not returned")
        from sqlite_vec import serialize_float32
        self.conn.execute(
            "INSERT INTO memory_embeddings (rowid, content_embedding, store) VALUES (?, ?, ?)",
            (cursor.lastrowid, serialize_float32(embedding), self.embedding_store_key),
        )

    async def write_logical_import_candidate_batch(
        self,
        records: list[dict[str, Any]],
        embeddings: list[list[float]],
        *,
        source_evidence: list[dict[str, Any]],
        idempotency_key: str,
        request_fingerprint: str,
        operation_id: str,
        request_id: str,
        scope_identity_digest: str,
        actor_class: str,
        actor_ref_digest: str | None,
        grant_ref_digest: str,
        committed_at: str,
    ) -> dict[str, Any]:
        """Atomically admit one governed batch of root Candidate records.

        Records are prepared by MemoryAdapter. This storage boundary revalidates the
        critical Candidate/scope/vector invariants and rechecks duplicate and
        contradiction state while one BEGIN IMMEDIATE transaction excludes competing
        writers. One lifecycle_operations row is the durable replay receipt.
        """

        self.require_embedding_compatibility()
        if not records or len(records) != len(embeddings) or len(records) > 1000:
            raise AdapterError("INVALID_ENVELOPE", "Candidate batch shape is invalid")
        if not isinstance(source_evidence, list) or not source_evidence or len(source_evidence) > 1000:
            raise AdapterError("INVALID_ENVELOPE", "logical import source evidence shape is invalid")

        def transaction() -> dict[str, Any]:
            assert self.conn is not None
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                replay = self._logical_import_replay_sync(
                    idempotency_key,
                    operation_id,
                    request_fingerprint,
                )
                if replay is not None:
                    self.conn.rollback()
                    return replay

                self._sample_trust_clock_sync(iso_now())
                self._sample_trust_clock_sync(committed_at)

                evidence_by_source: dict[str, dict[str, Any]] = {}
                evidence_order: list[str] = []
                active_evidence_ids: set[str] = set()
                for member in source_evidence:
                    if not isinstance(member, dict) or set(member) != {
                        "evidence_class",
                        "authority_effect",
                        "source_record",
                        "source_claim_digest",
                    }:
                        raise AdapterError("INVALID_ENVELOPE", "logical import source evidence member is invalid")
                    if (
                        member.get("evidence_class") != "NON_AUTHORITATIVE_SOURCE_RECORD"
                        or member.get("authority_effect") != "NONE"
                    ):
                        raise AdapterError("INVALID_ENVELOPE", "logical import evidence cannot claim authority")
                    source_record = member.get("source_record")
                    if not isinstance(source_record, dict):
                        raise AdapterError("INVALID_ENVELOPE", "logical import source evidence record is invalid")
                    source_record_id = source_record.get("source_record_id")
                    active = source_record.get("active")
                    if not isinstance(source_record_id, str) or not isinstance(active, bool):
                        raise AdapterError("INVALID_ENVELOPE", "logical import source evidence identity is invalid")
                    if member.get("source_claim_digest") != digest(source_record):
                        raise AdapterError("INVALID_ENVELOPE", "logical import source evidence digest is invalid")
                    if source_record_id in evidence_by_source:
                        raise AdapterError("INVALID_ENVELOPE", "logical import source evidence identity is duplicated")
                    evidence_by_source[source_record_id] = member
                    evidence_order.append(source_record_id)
                    if active:
                        active_evidence_ids.add(source_record_id)

                seen_record_ids: set[str] = set()
                seen_memory_ids: set[str] = set()
                seen_idempotency_keys: set[str] = set()
                record_source_ids: set[str] = set()
                checked: list[tuple[dict[str, Any], list[float], ScopeContext]] = []
                for record, embedding in zip(records, embeddings, strict=True):
                    scope = validate_scope(
                        record.get("scope"),
                        record.get("project_id"),
                        record.get("worktree_id"),
                        record.get("workflow_id"),
                        record.get("agent_id"),
                    )
                    if (
                        record.get("lifecycle_state") != "CANDIDATE"
                        or record.get("verified_by") != "UNVERIFIED"
                        or record.get("authority_role") != "UNRESOLVED"
                        or record.get("last_verified_at") is not None
                    ):
                        raise AdapterError(
                            "INVALID_ENVELOPE",
                            "logical import batch accepts only CANDIDATE/UNVERIFIED records",
                        )
                    if (
                        record.get("revision") != 1
                        or record.get("parent_record_id") is not None
                        or record.get("supersedes") != []
                    ):
                        raise AdapterError(
                            "INVALID_ENVELOPE",
                            "logical import Candidate must have a root lineage shape",
                        )
                    _validate_cross_project_storage_semantics(record)
                    if record.get("native_content_hash") != native_hash(scope, str(record.get("fact", ""))):
                        raise AdapterError(
                            "SEMANTIC_IDENTITY_MISMATCH",
                            "logical import Candidate native identity does not match exact scope and fact",
                        )
                    if record.get("embedding_profile_id") != self.profile.profile_id:
                        raise AdapterError("PROFILE_MISMATCH", "logical import embedding profile changed before commit")
                    if record.get("embedding_model_digest") != self.profile.model_digest:
                        raise AdapterError("PROFILE_MISMATCH", "logical import embedding model digest changed before commit")
                    if record.get("embedding_output_dimension") != self.profile.output_dimension:
                        raise AdapterError("PROFILE_MISMATCH", "logical import embedding dimension changed before commit")
                    finite_vector(embedding)
                    if len(embedding) != self.profile.output_dimension:
                        raise AdapterError("PROFILE_MISMATCH", "logical import vector dimension does not match profile")

                    record_id = str(record.get("record_id"))
                    memory_id = str(record.get("memory_id"))
                    record_key = str(record.get("idempotency_key"))
                    source_record_id = record.get("import_source_record_id")
                    if not isinstance(source_record_id, str) or source_record_id in record_source_ids:
                        raise AdapterError("INVALID_ENVELOPE", "logical import Candidate source identity is invalid")
                    evidence_member = evidence_by_source.get(source_record_id)
                    if (
                        evidence_member is None
                        or evidence_member.get("source_claim_digest") != record.get("external_source_claim_digest")
                    ):
                        raise AdapterError(
                            "INVALID_ENVELOPE",
                            "logical import Candidate is not bound to its source evidence",
                        )
                    record_source_ids.add(source_record_id)
                    if record_id in seen_record_ids or memory_id in seen_memory_ids or record_key in seen_idempotency_keys:
                        raise AdapterError("INVALID_ENVELOPE", "logical import batch contains duplicate generated identities")
                    if record_key == idempotency_key:
                        raise AdapterError("IDEMPOTENCY_CONFLICT", "record idempotency key cannot equal batch identity")
                    seen_record_ids.add(record_id)
                    seen_memory_ids.add(memory_id)
                    seen_idempotency_keys.add(record_key)
                    if self._idempotency_owner_sync(record_key) is not None:
                        raise AdapterError(
                            "IDEMPOTENCY_CONFLICT",
                            "logical import record idempotency key is already globally reserved",
                        )
                    checked.append((record, embedding, scope))

                if record_source_ids != active_evidence_ids:
                    raise AdapterError(
                        "INVALID_ENVELOPE",
                        "logical import active source evidence does not match Candidate admissions",
                    )

                outcomes_by_source: dict[str, dict[str, Any]] = {}
                touched_groups: dict[tuple[str, str], tuple[ScopeContext, str]] = {}
                for record, embedding, scope in checked:
                    exact_row = self.conn.execute(
                        self._select_sql(
                            "WHERE a.native_content_hash=? ORDER BY a.revision DESC LIMIT 1"
                        ),
                        (record["native_content_hash"],),
                    ).fetchone()
                    if exact_row is not None:
                        exact = self._row_to_dict(exact_row)
                        source_id = record["import_source_record_id"]
                        outcomes_by_source[source_id] = {
                            "source_record_id": source_id,
                            "source_claim_digest": record["external_source_claim_digest"],
                            "status": "EXACT_DUPLICATE",
                            "scope": scope.to_dict(),
                            "memory_id": exact["memory_id"],
                            "record_id": exact["record_id"],
                            "destination_memory_id": exact["memory_id"],
                            "destination_record_id": exact["record_id"],
                            "destination_lifecycle_state": exact["lifecycle_state"],
                            "destination_verified_by": exact["verified_by"],
                            "destination_authority_role": exact["authority_role"],
                            "contradiction_state": exact["contradiction_state"],
                        }
                        continue

                    where, params = self._scope_where(scope)
                    marks = ",".join("?" for _ in _GOVERNANCE_ACTIVE_STATES)
                    active_rows = [
                        self._row_to_dict(row)
                        for row in self.conn.execute(
                            self._select_sql(
                                f"WHERE a.contradiction_key=? AND {where} "
                                f"AND a.lifecycle_state IN ({marks}) ORDER BY a.created_at,a.record_id"
                            ),
                            [record["contradiction_key"], *params, *sorted(_GOVERNANCE_ACTIVE_STATES)],
                        ).fetchall()
                    ]
                    normalized_fact = self._normalized_fact(record["fact"])
                    equivalent = next(
                        (
                            row
                            for row in active_rows
                            if self._normalized_fact(row["fact"]) == normalized_fact
                        ),
                        None,
                    )
                    if equivalent is not None:
                        source_id = record["import_source_record_id"]
                        outcomes_by_source[source_id] = {
                            "source_record_id": source_id,
                            "source_claim_digest": record["external_source_claim_digest"],
                            "status": "EQUIVALENT",
                            "scope": scope.to_dict(),
                            "memory_id": equivalent["memory_id"],
                            "record_id": equivalent["record_id"],
                            "destination_memory_id": equivalent["memory_id"],
                            "destination_record_id": equivalent["record_id"],
                            "destination_lifecycle_state": equivalent["lifecycle_state"],
                            "destination_verified_by": equivalent["verified_by"],
                            "destination_authority_role": equivalent["authority_role"],
                            "contradiction_state": equivalent["contradiction_state"],
                        }
                        continue

                    incoming = dict(record)
                    incoming["contradiction_state"] = (
                        "UNRESOLVED"
                        if any(self._normalized_fact(row["fact"]) != normalized_fact for row in active_rows)
                        else "NONE"
                    )
                    self._insert_materialized_record(incoming, embedding)
                    touched_groups[(scope.key(), incoming["contradiction_key"])] = (
                        scope,
                        incoming["contradiction_key"],
                    )
                    source_id = incoming["import_source_record_id"]
                    outcomes_by_source[source_id] = {
                        "source_record_id": source_id,
                        "source_claim_digest": incoming["external_source_claim_digest"],
                        "status": "ADMITTED",
                        "scope": scope.to_dict(),
                        "memory_id": incoming["memory_id"],
                        "record_id": incoming["record_id"],
                        "destination_memory_id": incoming["memory_id"],
                        "destination_record_id": incoming["record_id"],
                        "destination_lifecycle_state": "CANDIDATE",
                        "destination_verified_by": "UNVERIFIED",
                        "destination_authority_role": "UNRESOLVED",
                        "contradiction_state": incoming["contradiction_state"],
                    }

                event_ids: list[str] = []
                for scope, contradiction_key in touched_groups.values():
                    _, group_events = self._recompute_candidate_contradictions_sync(
                        scope,
                        contradiction_key,
                        operation_id=operation_id,
                        actor_class=actor_class,
                        actor_ref_digest=actor_ref_digest,
                        grant_ref_digest=grant_ref_digest,
                        occurred_at=committed_at,
                    )
                    event_ids.extend(group_events)

                for outcome in outcomes_by_source.values():
                    if outcome["status"] != "ADMITTED":
                        continue
                    row = self.conn.execute(
                        "SELECT contradiction_state FROM adapter_records WHERE record_id=?",
                        (outcome["destination_record_id"],),
                    ).fetchone()
                    if row is None:
                        raise AdapterError("STORAGE_INTEGRITY", "logical import Candidate disappeared before commit")
                    outcome["contradiction_state"] = str(row[0])

                receipt_members: list[dict[str, Any]] = []
                for source_id in evidence_order:
                    evidence = evidence_by_source[source_id]
                    outcome = outcomes_by_source.get(source_id)
                    if outcome is not None:
                        receipt_members.append({**evidence, **outcome})
                        continue
                    source_record = evidence["source_record"]
                    if source_record.get("active") is True:
                        raise AdapterError(
                            "STORAGE_INTEGRITY",
                            "active logical import source evidence has no Candidate outcome",
                        )
                    receipt_members.append(
                        {
                            **evidence,
                            "source_record_id": source_id,
                            "status": "HISTORY_ONLY",
                            "scope": source_record.get("scope"),
                            "memory_id": None,
                            "record_id": None,
                            "destination_memory_id": None,
                            "destination_record_id": None,
                            "destination_lifecycle_state": None,
                            "destination_verified_by": None,
                            "destination_authority_role": None,
                            "contradiction_state": None,
                        }
                    )

                result_outcome = (
                    "ADMITTED"
                    if any(item["status"] == "ADMITTED" for item in outcomes_by_source.values())
                    else "NO_NEW"
                )
                self.conn.execute(
                    "INSERT INTO lifecycle_operations("
                    "idempotency_key,request_fingerprint,operation_id,request_id,operation_kind,actor_class,"
                    "actor_ref_digest,grant_ref_digest,scope_digest,result_outcome,affected_members_json,event_ids_json,"
                    "result_group_count,replay_status,commit_timestamp"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        idempotency_key,
                        request_fingerprint,
                        operation_id,
                        request_id,
                        "LOGICAL_IMPORT",
                        actor_class,
                        actor_ref_digest,
                        grant_ref_digest,
                        scope_identity_digest,
                        result_outcome,
                        canonical_json(receipt_members),
                        canonical_json(event_ids),
                        len(receipt_members),
                        "COMMITTED",
                        committed_at,
                    ),
                )
                operation = self._operation_row_sync(idempotency_key)
                if operation is None:
                    raise AdapterError("STORAGE_INTEGRITY", "logical import receipt was not persisted")
                self.conn.commit()
                return self._operation_result(operation, replay=False)
            except sqlite3.IntegrityError as exc:
                self.conn.rollback()
                message = str(exc)
                if "FACTLANE_GLOBAL_IDEMPOTENCY_CONFLICT" in message:
                    raise AdapterError(
                        "IDEMPOTENCY_CONFLICT",
                        "logical import identity collided with another governed write",
                    ) from None
                raise
            except BaseException:
                self.conn.rollback()
                raise

        return await self._run(transaction)

    async def write_record(
        self,
        record: dict[str, Any],
        embedding: list[float],
        supersede_record_id: str | None = None,
        *,
        observed_group_token: str | None = None,
    ) -> None:
        self.require_embedding_compatibility()
        def transaction() -> None:
            assert self.conn is not None
            now = time.time()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                owner = self._idempotency_owner_sync(str(record["idempotency_key"]))
                if owner == "operation":
                    raise RecordUniquenessConflict("global_idempotency")
                if owner == "adapter":
                    raise RecordUniquenessConflict("idempotency_key")
                self._sample_trust_clock_sync(iso_now())
                self._sample_trust_clock_sync(str(record["created_at"]))
                _validate_current_authority_metadata(record)
                if supersede_record_id is None and (
                    record.get("revision") != 1
                    or record.get("parent_record_id") is not None
                    or record.get("supersedes", []) != []
                ):
                    raise AdapterError(
                        "INVALID_ENVELOPE",
                        "initial storage record must have a root lineage shape",
                    )
                if supersede_record_id is None and record.get("lifecycle_state") != "VALIDATED_CURRENT":
                    validate_scope(
                        record.get("scope"),
                        record.get("project_id"),
                        record.get("worktree_id"),
                        record.get("workflow_id"),
                        record.get("agent_id"),
                    )
                superseded_native_hash: str | None = None
                if supersede_record_id is not None:
                    old = self.conn.execute(
                        "SELECT native_content_hash, lifecycle_state,scope,project_id,worktree_id,workflow_id,agent_id,"
                        "memory_id,revision,contradiction_state,contradiction_key "
                        "FROM adapter_records WHERE record_id = ?",
                        (supersede_record_id,),
                    ).fetchone()
                    if not old or old[1] != "VALIDATED_CURRENT":
                        raise AdapterError(
                            "VERSION_CONFLICT",
                            "expected revision is no longer the current lineage parent",
                        )
                    if observed_group_token is not None:
                        exact_scope = ScopeContext(
                            str(old[2]),
                            old[3],
                            old[4],
                            old[5],
                            old[6],
                        )
                        self._require_group_token_sync(exact_scope, str(old[10]), observed_group_token)
                    if record.get("parent_record_id") != supersede_record_id:
                        raise AdapterError("VERSION_CONFLICT", "update lineage parent does not match expected revision")
                    if (
                        any(
                            record.get(field) != old[index]
                            for index, field in enumerate(
                                ("scope", "project_id", "worktree_id", "workflow_id", "agent_id"),
                                start=2,
                            )
                        )
                        or record.get("lifecycle_state") != "VALIDATED_CURRENT"
                    ):
                        raise AdapterError(
                            "INVALID_ENVELOPE",
                            "update successor cannot change scope identity or leave validated-current lifecycle",
                        )
                    parent_memory_id = str(old[7])
                    parent_revision = int(old[8])
                    successor_memory_id = str(record["memory_id"])
                    successor_revision = int(record["revision"])
                    if successor_memory_id == parent_memory_id:
                        valid_lineage_shape = successor_revision == parent_revision + 1
                    else:
                        valid_lineage_shape = (
                            successor_revision == 1
                            and record.get("supersedes") == [parent_memory_id]
                        )
                    if not valid_lineage_shape:
                        raise AdapterError(
                            "INVALID_ENVELOPE",
                            "update successor has an invalid memory lineage shape",
                        )
                    if str(old[9]) not in {"NONE", "RESOLVED"}:
                        raise AdapterError(
                            "INVALID_ENVELOPE",
                            "validated-current predecessor has an invalid contradiction state",
                        )
                    superseded_native_hash = str(old[0])

                _validate_cross_project_storage_semantics(record)

                if supersede_record_id is None and record.get("lifecycle_state") not in {
                    "CANDIDATE",
                    "VALIDATED_CURRENT",
                }:
                    raise AdapterError(
                        "INVALID_ENVELOPE",
                        "initial storage record lifecycle must be CANDIDATE or VALIDATED_CURRENT",
                    )

                _validate_current_contradiction_state(record)

                if record["lifecycle_state"] == "VALIDATED_CURRENT":
                    exact_scope = ScopeContext(record["scope"], record.get("project_id"), record.get("worktree_id"), record.get("workflow_id"), record.get("agent_id"))
                    where, params = self._scope_where(exact_scope)
                    current_rows = self.conn.execute(self._select_sql(f"WHERE a.contradiction_key=? AND a.lifecycle_state='VALIDATED_CURRENT' AND {where} ORDER BY a.created_at, a.record_id"), [record["contradiction_key"], *params]).fetchall()
                    incoming_fact = " ".join(str(record["fact"]).casefold().split())
                    for current_row in current_rows:
                        current = self._row_to_dict(current_row)
                        if supersede_record_id is not None and current["record_id"] == supersede_record_id:
                            continue
                        if " ".join(str(current["fact"]).casefold().split()) != incoming_fact:
                            raise AdapterError("CONTRADICTION", "a different validated current fact already exists for this contradiction key")

                values = [
                    record["record_id"],
                    record["memory_id"],
                    record["revision"],
                    record.get("parent_record_id"),
                    record["scope"],
                    record.get("project_id"),
                    record.get("worktree_id"),
                    record.get("workflow_id"),
                    record.get("agent_id"),
                    record["memory_type"],
                    record["fact"],
                    canonical_json(record["source_provenance"]),
                    record.get("source_timestamp"),
                    record["created_at"],
                    record.get("last_verified_at"),
                    record["verified_by"],
                    record["authority_role"],
                    canonical_json(
                        record.get("contribution_origin")
                        or {"contributor_class": "LEGACY_UNKNOWN", "contributor_ref": None}
                    ),
                    canonical_json(record["freshness_policy"]),
                    canonical_json(record.get("supersedes", [])),
                    record["contradiction_key"],
                    record["contradiction_state"],
                    record["confidence"],
                    canonical_json(record.get("tags", [])),
                    record["lifecycle_state"],
                    record["native_content_hash"],
                    record["payload_fingerprint"],
                    record["idempotency_key"],
                    record["embedding_profile_id"],
                    record["embedding_model_digest"],
                    record["embedding_output_dimension"],
                ]
                placeholders = ",".join("?" for _ in _RECORD_COLUMNS)
                self.conn.execute(
                    f"INSERT INTO adapter_records ({','.join(_RECORD_COLUMNS)}) VALUES ({placeholders})",
                    values,
                )
                native_metadata = canonical_json(
                    {
                        "adapter_record_id": record["record_id"],
                        "adapter_memory_id": record["memory_id"],
                        "embedding_profile_id": record["embedding_profile_id"],
                        "lifecycle_state": record["lifecycle_state"],
                    }
                )
                native_values: dict[str, Any] = {
                    "content_hash": record["native_content_hash"],
                    "content": record["fact"],
                    "tags": ",".join(record.get("tags", [])),
                    "memory_type": record["memory_type"],
                    "metadata": native_metadata,
                    "created_at": parse_iso(record["created_at"], required=True).timestamp(),
                    "updated_at": parse_iso(record["created_at"], required=True).timestamp(),
                    "created_at_iso": record["created_at"],
                    "updated_at_iso": record["created_at"],
                    "store": self.embedding_store_key,
                }
                if "confidence" in self.native_columns:
                    native_values["confidence"] = record["confidence"]
                if "last_accessed" in self.native_columns:
                    native_values["last_accessed"] = None
                native_fields = [field for field in native_values if field in self.native_columns]
                native_sql = (
                    f"INSERT INTO memories ({','.join(native_fields)}) VALUES "
                    f"({','.join('?' for _ in native_fields)})"
                )
                cursor = self.conn.execute(native_sql, [native_values[field] for field in native_fields])
                rowid = cursor.lastrowid
                if rowid is None:
                    raise sqlite3.IntegrityError("native memory rowid was not returned")
                from sqlite_vec import serialize_float32

                self.conn.execute(
                    "INSERT INTO memory_embeddings (rowid, content_embedding, store) VALUES (?, ?, ?)",
                    (rowid, serialize_float32(embedding), self.embedding_store_key),
                )
                if supersede_record_id is not None:
                    self.conn.execute(
                        "UPDATE adapter_records SET lifecycle_state='SUPERSEDED' WHERE record_id = ?",
                        (supersede_record_id,),
                    )
                    if "superseded_by" in self.native_columns:
                        assert superseded_native_hash is not None
                        self.conn.execute(
                            "UPDATE memories SET superseded_by = ? WHERE content_hash = ?",
                            (record["native_content_hash"], superseded_native_hash),
                        )
                self.conn.commit()
            except sqlite3.IntegrityError as exc:
                self.conn.rollback()
                message = str(exc)
                if "FACTLANE_GLOBAL_IDEMPOTENCY_CONFLICT" in message:
                    raise RecordUniquenessConflict("global_idempotency") from None
                if "adapter_records.native_content_hash" in message:
                    raise RecordUniquenessConflict("native_content_hash") from None
                if "adapter_records.idempotency_key" in message:
                    raise RecordUniquenessConflict("idempotency_key") from None
                raise
            except Exception:
                self.conn.rollback()
                raise

        await self._run(transaction)

    async def promote_candidate(
        self,
        record: dict[str, Any],
        embedding: list[float],
        scope: ScopeContext,
        *,
        expected_record_id: str,
        expected_revision: int,
        observed_group_token: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically promote one exact Candidate lineage parent to validated current."""
        self.require_embedding_compatibility()
        where, params = self._scope_where(scope)

        def transaction() -> dict[str, Any] | None:
            assert self.conn is not None
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if self._operation_row_sync(str(record["idempotency_key"])) is not None:
                    raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is reserved by another governed write")
                if self.conn.execute(
                    "SELECT 1 FROM adapter_records WHERE idempotency_key=? LIMIT 1",
                    (record["idempotency_key"],),
                ).fetchone():
                    raise RecordUniquenessConflict("idempotency_key")
                self._sample_trust_clock_sync(iso_now())
                self._sample_trust_clock_sync(str(record["created_at"]))
                _validate_current_authority_metadata(record)
                parent_row = self.conn.execute(
                    self._select_sql(
                        f"WHERE a.memory_id=? AND a.record_id=? AND a.revision=? "
                        f"AND a.lifecycle_state='CANDIDATE' AND {where}"
                    ),
                    [record["memory_id"], expected_record_id, expected_revision, *params],
                ).fetchone()
                if parent_row is None:
                    raise AdapterError("VERSION_CONFLICT", "candidate lineage parent no longer matches expected record and revision")
                parent = self._row_to_dict(parent_row)
                if observed_group_token is not None:
                    self._require_group_token_sync(scope, parent["contradiction_key"], observed_group_token)
                if parent["contradiction_state"] in {"UNRESOLVED", "QUARANTINED"}:
                    raise AdapterError("CONTRADICTION", "candidate has an unresolved contradiction and cannot be promoted")
                if record.get("parent_record_id") != expected_record_id or record.get("revision") != expected_revision + 1:
                    raise AdapterError("VERSION_CONFLICT", "promotion successor does not match candidate lineage")
                if (
                    any(
                        record.get(field) != parent[field]
                        for field in ("scope", "project_id", "worktree_id", "workflow_id", "agent_id")
                    )
                    or record.get("memory_type") != parent["memory_type"]
                    or record.get("fact") != parent["fact"]
                    or record.get("contradiction_key") != parent["contradiction_key"]
                    or record.get("lifecycle_state") != "VALIDATED_CURRENT"
                ):
                    raise AdapterError(
                        "INVALID_ENVELOPE",
                        "candidate REVERIFY successor cannot change logical or contradiction identity",
                    )
                _validate_cross_project_storage_semantics(record)
                _validate_current_contradiction_state(record)

                state_marks = ",".join("?" for _ in _GOVERNANCE_ACTIVE_STATES)
                active_rows = self.conn.execute(
                    self._select_sql(
                        f"WHERE a.contradiction_key=? AND a.lifecycle_state IN ({state_marks}) AND {where} "
                        "ORDER BY a.created_at, a.record_id"
                    ),
                    [parent["contradiction_key"], *sorted(_GOVERNANCE_ACTIVE_STATES), *params],
                ).fetchall()
                equivalent_current: dict[str, Any] | None = None
                parent_fact = self._normalized_fact(parent["fact"])
                for active_row in active_rows:
                    peer = self._row_to_dict(active_row)
                    if peer["record_id"] == parent["record_id"]:
                        continue
                    if self._normalized_fact(peer["fact"]) != parent_fact:
                        if peer["lifecycle_state"] in {"STALE", "QUARANTINED"}:
                            raise AdapterError(
                                "GROUP_MEMBER_NOT_ADMISSIBLE",
                                "an incompatible stale or quarantined group member blocks Candidate promotion",
                            )
                        raise AdapterError(
                            "CONTRADICTION",
                            "an incompatible governance-active peer blocks Candidate promotion",
                        )
                    if peer["lifecycle_state"] == "VALIDATED_CURRENT":
                        equivalent_current = peer
                if equivalent_current is not None:
                    self.conn.commit()
                    return equivalent_current

                self._insert_materialized_record(record, embedding)
                updated = self.conn.execute(
                    "UPDATE adapter_records SET lifecycle_state='SUPERSEDED' "
                    "WHERE record_id=? AND memory_id=? AND revision=? AND lifecycle_state='CANDIDATE'",
                    (expected_record_id, record["memory_id"], expected_revision),
                )
                if updated.rowcount != 1:
                    raise AdapterError("VERSION_CONFLICT", "candidate lineage changed during promotion")
                if "superseded_by" in self.native_columns:
                    self.conn.execute(
                        "UPDATE memories SET superseded_by=? WHERE content_hash=?",
                        (record["native_content_hash"], parent["native_content_hash"]),
                    )
                self.conn.commit()
                return None
            except Exception:
                self.conn.rollback()
                raise

        return await self._run(transaction)

    async def compact_superseded_record(self, record_id: str) -> bool:
        """Compact one fully materialized superseded record into history."""
        self.require_embedding_compatibility()

        def transaction() -> bool:
            assert self.conn is not None
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT lifecycle_state, native_content_hash FROM adapter_records WHERE record_id = ?",
                    (record_id,),
                ).fetchone()
                if not row:
                    raise AdapterError("NOT_FOUND", "record_id was not found")

                lifecycle_state, content_hash = row
                native = self.conn.execute(
                    "SELECT id, deleted_at FROM memories WHERE content_hash = ?",
                    (content_hash,),
                ).fetchone()
                native_id = native[0] if native else None
                vector = (
                    self.conn.execute(
                        "SELECT 1 FROM memory_embeddings WHERE rowid = ? AND store = ?",
                        (native_id, self.embedding_store_key),
                    ).fetchone()
                    if native_id is not None
                    else None
                )
                graph = self.conn.execute(
                    "SELECT 1 FROM memory_graph WHERE source_hash = ? OR target_hash = ? LIMIT 1",
                    (content_hash, content_hash),
                ).fetchone()

                if lifecycle_state == "HISTORICAL":
                    if native is None and graph is None:
                        self.conn.commit()
                        return False
                    raise AdapterError(
                        "COMPACTION_STATE_INVALID",
                        "historical record still has materialized state",
                    )
                if lifecycle_state != "SUPERSEDED":
                    raise AdapterError(
                        "COMPACTION_NOT_ELIGIBLE",
                        "only superseded records can be compacted",
                    )
                if native is None or native[1] is not None or vector is None:
                    raise AdapterError(
                        "COMPACTION_STATE_INVALID",
                        "superseded record has partial materialization",
                    )

                updated = self.conn.execute(
                    "UPDATE adapter_records SET lifecycle_state = 'HISTORICAL' "
                    "WHERE record_id = ? AND lifecycle_state = 'SUPERSEDED'",
                    (record_id,),
                )
                if updated.rowcount != 1:
                    raise AdapterError("COMPACTION_NOT_ELIGIBLE", "record is no longer superseded")
                self.conn.execute(
                    "DELETE FROM memory_embeddings WHERE rowid = ? AND store = ?",
                    (native_id, self.embedding_store_key),
                )
                self.conn.execute(
                    "DELETE FROM memory_graph WHERE source_hash = ? OR target_hash = ?",
                    (content_hash, content_hash),
                )
                deleted = self.conn.execute(
                    "DELETE FROM memories WHERE id = ? AND content_hash = ? AND deleted_at IS NULL",
                    (native_id, content_hash),
                )
                if deleted.rowcount != 1:
                    raise AdapterError("COMPACTION_STATE_INVALID", "native materialization changed during compaction")
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

        return await self._run(transaction)

    async def _housekeeping_health(self, scope: ScopeContext) -> tuple[int, int, str, str]:
        where, params = self._scope_where(scope)

        def query() -> tuple[int, int, str, str]:
            assert self.conn is not None
            vector_orphans = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM memory_embeddings e "
                    "WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = e.rowid)"
                ).fetchone()[0]
            )
            fork_rows = self.conn.execute(
                "SELECT a.memory_id, COUNT(*) FROM adapter_records a "
                f"WHERE {where} AND a.lifecycle_state = 'VALIDATED_CURRENT' "
                "GROUP BY a.memory_id HAVING COUNT(*) > 1",
                params,
            ).fetchall()
            current_lineage_forks = sum(int(row[1]) - 1 for row in fork_rows)

            def check(name: str) -> str:
                try:
                    row = self.conn.execute(f"PRAGMA {name}").fetchone()
                except sqlite3.Error:
                    return "ERROR"
                return str(row[0]) if row else "ERROR"

            return vector_orphans, current_lineage_forks, check("quick_check"), check("integrity_check")

        return await self._run(query)

    async def housekeep_superseded(
        self,
        scope: ScopeContext,
        *,
        max_records: int,
        required_free_bytes: int,
    ) -> dict[str, Any]:
        """Compact a bounded set of eligible superseded records under pressure."""
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
            raise AdapterError("INVALID_ENVELOPE", "max_records must be a positive integer")
        if isinstance(required_free_bytes, bool) or not isinstance(required_free_bytes, int) or required_free_bytes < 0:
            raise AdapterError("INVALID_ENVELOPE", "required_free_bytes must be a non-negative integer")

        before = await self.status(scope)
        before_capacity = before["capacity"]
        before_retention = before["retention"]

        def capacity_requirement_met(capacity: dict[str, Any]) -> bool | None:
            free_bytes = capacity["filesystem_free_bytes"]
            if capacity["observation"] != "KNOWN" or free_bytes is None:
                return None
            return free_bytes >= required_free_bytes

        def report(
            outcome: str,
            next_action: str,
            *,
            mutation: str = "NONE",
            selected_records: int = 0,
            compacted_records: int = 0,
            after: dict[str, Any] | None = None,
            health_after: tuple[int | None, int | None, str | None, str | None] | None = None,
        ) -> dict[str, Any]:
            after_capacity = after["capacity"] if after else before_capacity
            after_retention = after["retention"] if after else before_retention
            vector_orphans, current_lineage_forks, quick_check, integrity_check = health_after or (None, None, None, None)
            return {
                "outcome": outcome,
                "next_action": next_action,
                "mutation": mutation,
                "required_free_bytes": required_free_bytes,
                "max_records": max_records,
                "filesystem_free_bytes_before": before_capacity["filesystem_free_bytes"],
                "filesystem_free_bytes_after": after_capacity["filesystem_free_bytes"],
                "capacity_requirement_met_before": capacity_requirement_met(before_capacity),
                "capacity_requirement_met_after": capacity_requirement_met(after_capacity),
                "compaction_ready_before": before_retention["compaction_ready"],
                "selected_records": selected_records,
                "compacted_records": compacted_records,
                "compaction_ready_after": after_retention["compaction_ready"],
                "compaction_blocked_partial": before_retention["compaction_blocked_partial"],
                "historical_total_before": before_retention["historical_total"],
                "historical_total_after": after_retention["historical_total"],
                "vector_orphans_after": vector_orphans,
                "current_lineage_forks_after": current_lineage_forks,
                "quick_check_after": quick_check,
                "integrity_check_after": integrity_check,
                "physical_file_shrink_claimed": False,
                "automatic_housekeeping": False,
            }

        if before_capacity["observation"] != "KNOWN":
            return report(
                "BLOCKED_UNKNOWN_CAPACITY",
                "RESTORE_CAPACITY_OBSERVABILITY_BEFORE_MEMORY_MUTATION",
            )
        if capacity_requirement_met(before_capacity):
            return report("NO_ACTION_CAPACITY_REQUIREMENT_ALREADY_MET", "NO_ACTION_REQUIRED")
        if before_retention["compaction_blocked_partial"]:
            return report("BLOCKED_PARTIAL_RECLAIM_STATE", "REPAIR_PARTIAL_RECLAIM_STATE_BEFORE_MEMORY_MUTATION")

        before_health = await self._housekeeping_health(scope)
        if before_health[0]:
            return report(
                "BLOCKED_PREEXISTING_VECTOR_ORPHAN",
                "REPAIR_VECTOR_ORPHANS_BEFORE_MEMORY_MUTATION",
                health_after=before_health,
            )
        if before_health[1]:
            return report(
                "BLOCKED_PREEXISTING_CURRENT_FORK",
                "REPAIR_CURRENT_LINEAGE_FORK_BEFORE_MEMORY_MUTATION",
                health_after=before_health,
            )
        if before_health[2] != "ok" or before_health[3] != "ok":
            return report(
                "BLOCKED_DATABASE_INTEGRITY",
                "REPAIR_DATABASE_INTEGRITY_BEFORE_MEMORY_MUTATION",
                health_after=before_health,
            )

        where, params = self._scope_where(scope)

        def select_records() -> list[str]:
            assert self.conn is not None
            rows = self.conn.execute(
                "SELECT a.record_id FROM adapter_records a "
                "JOIN memories m ON m.content_hash = a.native_content_hash AND m.deleted_at IS NULL "
                "JOIN memory_embeddings e ON e.rowid = m.id AND e.store = ? "
                f"WHERE {where} AND a.lifecycle_state = 'SUPERSEDED' "
                "ORDER BY a.created_at, a.record_id LIMIT ?",
                [self.embedding_store_key, *params, max_records],
            ).fetchall()
            return [str(row[0]) for row in rows]

        selected = await self._run(select_records)
        if not selected:
            return report(
                "NO_ELIGIBLE_RECORDS_CAPACITY_REQUIREMENT_NOT_MET",
                "ADDITIONAL_CAPACITY_OR_OPERATOR_ACTION_REQUIRED",
                health_after=before_health,
            )

        compacted = 0
        for record_id in selected:
            if await self.compact_superseded_record(record_id):
                compacted += 1

        after = await self.status(scope)
        after_health = await self._housekeeping_health(scope)
        if after_health[0] or after_health[1] or after_health[2] != "ok" or after_health[3] != "ok":
            return report(
                "BLOCKED_POST_MAINTENANCE_HEALTH",
                "REPAIR_POST_MAINTENANCE_HEALTH_BEFORE_MEMORY_MUTATION",
                mutation="BOUNDED_COMPACTION",
                selected_records=len(selected),
                compacted_records=compacted,
                after=after,
                health_after=after_health,
            )
        after_capacity_met = capacity_requirement_met(after["capacity"])
        if after_capacity_met is None:
            return report(
                "BLOCKED_POST_MAINTENANCE_UNKNOWN_CAPACITY",
                "RESTORE_CAPACITY_OBSERVABILITY_BEFORE_MEMORY_MUTATION",
                mutation="BOUNDED_COMPACTION",
                selected_records=len(selected),
                compacted_records=compacted,
                after=after,
                health_after=after_health,
            )
        if after_capacity_met:
            return report(
                "HOUSEKEEPING_COMPLETE_CAPACITY_REQUIREMENT_MET",
                "NO_ACTION_REQUIRED",
                mutation="BOUNDED_COMPACTION",
                selected_records=len(selected),
                compacted_records=compacted,
                after=after,
                health_after=after_health,
            )
        return report(
            "HOUSEKEEPING_COMPLETE_CAPACITY_REQUIREMENT_NOT_MET",
            "ADDITIONAL_CAPACITY_OR_OPERATOR_ACTION_REQUIRED",
            mutation="BOUNDED_COMPACTION",
            selected_records=len(selected),
            compacted_records=compacted,
            after=after,
            health_after=after_health,
        )

    async def vector_candidates(
        self,
        vector: list[float],
        scope: ScopeContext,
        *,
        limit: int,
        history: bool,
    ) -> list[tuple[dict[str, Any], float]]:
        self.require_embedding_compatibility()
        where, params = self._scope_where(scope)

        def query() -> list[tuple[dict[str, Any], float]]:
            assert self.conn is not None
            from sqlite_vec import serialize_float32

            columns = ", ".join(f"a.{column}" for column in _RECORD_COLUMNS)
            if history:
                rows = self.conn.execute(
                    f"SELECT {columns}, e.distance FROM adapter_records a "
                    "JOIN memories m ON m.content_hash = a.native_content_hash "
                    "JOIN (SELECT rowid, distance FROM memory_embeddings "
                    "WHERE content_embedding MATCH ? AND k = ? AND store = ?) e ON e.rowid = m.id "
                    f"WHERE m.deleted_at IS NULL AND {where} "
                    "ORDER BY e.distance ASC LIMIT ?",
                    [serialize_float32(vector), max(limit * 4, limit), self.embedding_store_key, *params, limit],
                ).fetchall()
            else:
                eligible_where, eligible_params = self._scope_where(scope, alias="eligible")
                rows = self.conn.execute(
                    f"SELECT {columns}, e.distance FROM adapter_records a "
                    "JOIN memories m ON m.content_hash = a.native_content_hash "
                    "JOIN (SELECT rowid, distance FROM memory_embeddings "
                    "WHERE content_embedding MATCH ? "
                    "AND rowid IN ("
                    "SELECT native.id FROM memories native "
                    "JOIN adapter_records eligible ON eligible.native_content_hash = native.content_hash "
                    f"WHERE native.deleted_at IS NULL AND {eligible_where} "
                    "AND eligible.lifecycle_state = 'VALIDATED_CURRENT') "
                    "AND k = ? AND store = ?) e ON e.rowid = m.id "
                    f"WHERE m.deleted_at IS NULL AND {where} AND a.lifecycle_state = 'VALIDATED_CURRENT' "
                    "ORDER BY e.distance ASC LIMIT ?",
                    [serialize_float32(vector), *eligible_params, limit, self.embedding_store_key, *params, limit],
                ).fetchall()
            return [(self._row_to_dict(row[:-1]), float(row[-1])) for row in rows]

        return await self._run(query)

    async def keyword_candidates(
        self,
        query_text: str,
        scope: ScopeContext,
        *,
        limit: int,
        history: bool,
        exact: bool = False,
    ) -> list[dict[str, Any]]:
        where, params = self._scope_where(scope)
        lifecycle = "" if history else " AND a.lifecycle_state = 'VALIDATED_CURRENT'"
        operator = "=" if exact else "LIKE"
        value = query_text.casefold() if exact else f"%{query_text.casefold()}%"

        def query() -> list[dict[str, Any]]:
            assert self.conn is not None
            rows = self.conn.execute(
                self._select_sql(
                    f"WHERE {_CASEFOLD_FUNCTION}(a.fact) {operator} ? AND {where}{lifecycle} "
                    "ORDER BY a.created_at DESC, a.record_id LIMIT ?"
                ),
                [value, *params, limit],
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

        return await self._run(query)

    async def has_unmaterialized_history(self, scope: ScopeContext) -> bool:
        """Return whether compacted/corrupt historical rows are invisible to vector retrieval."""
        where, params = self._scope_where(scope)

        def query() -> bool:
            assert self.conn is not None
            row = self.conn.execute(
                "SELECT 1 FROM adapter_records a "
                "LEFT JOIN memories m ON m.content_hash = a.native_content_hash AND m.deleted_at IS NULL "
                "LEFT JOIN memory_embeddings e ON e.rowid = m.id AND e.store = ? "
                f"WHERE {where} AND a.lifecycle_state = 'HISTORICAL' "
                "AND (m.id IS NULL OR e.rowid IS NULL) LIMIT 1",
                [self.embedding_store_key, *params],
            ).fetchone()
            return row is not None

        return await self._run(query)

    async def contradiction_summary(self, scope: ScopeContext) -> list[dict[str, Any]]:
        where, params = self._scope_where(scope)

        def query() -> list[dict[str, Any]]:
            assert self.conn is not None
            rows = self.conn.execute(
                "SELECT a.record_id, a.memory_id, a.contradiction_key, a.contradiction_state, "
                "a.lifecycle_state, a.authority_role, a.source_provenance "
                f"FROM adapter_records a WHERE {where} AND a.contradiction_state IN ('UNRESOLVED','QUARANTINED') "
                "ORDER BY a.created_at, a.record_id LIMIT 16",
                params,
            ).fetchall()
            return [
                {
                    "record_id": row[0],
                    "memory_id": row[1],
                    "contradiction_key": row[2],
                    "contradiction_state": row[3],
                    "lifecycle_state": row[4],
                    "authority_role": row[5],
                    "source_class": json.loads(row[6]).get("source_class"),
                }
                for row in rows
            ]

        return await self._run(query)

    async def status(self, scope: ScopeContext | None) -> dict[str, Any]:
        if scope:
            where, params = self._scope_where(scope)
            suffix = "WHERE " + where
        else:
            suffix, params = "", []

        def query() -> dict[str, Any]:
            assert self.conn is not None
            counts = {
                state: 0
                for state in (
                    "CANDIDATE",
                    "VALIDATED_CURRENT",
                    "SUPERSEDED",
                    "STALE",
                    "QUARANTINED",
                    "HISTORICAL",
                    "REVOKED",
                    "REJECTED",
                )
            }
            rows = self.conn.execute(
                f"SELECT lifecycle_state, COUNT(*) FROM adapter_records a {suffix} GROUP BY lifecycle_state",
                params,
            ).fetchall()
            for state, count in rows:
                if state in counts:
                    counts[state] = int(count)
            superseded_suffix = (
                f"WHERE {where} AND a.lifecycle_state = 'SUPERSEDED'"
                if scope
                else "WHERE a.lifecycle_state = 'SUPERSEDED'"
            )
            superseded_total, compaction_ready = self.conn.execute(
                f"""
                SELECT
                    COUNT(*),
                    COALESCE(SUM(
                        CASE WHEN EXISTS (
                            SELECT 1
                            FROM memories m
                            WHERE m.content_hash = a.native_content_hash
                              AND m.deleted_at IS NULL
                              AND EXISTS (
                                  SELECT 1
                                  FROM memory_embeddings e
                                  WHERE e.rowid = m.id
                                    AND e.store = ?
                              )
                        ) THEN 1 ELSE 0 END
                    ), 0)
                FROM adapter_records a
                {superseded_suffix}
                """,
                [self.embedding_store_key, *params],
            ).fetchone()
            superseded_total = int(superseded_total)
            compaction_ready = int(compaction_ready)
            compaction_blocked_partial = superseded_total - compaction_ready

            def file_size(path: str) -> int | None:
                try:
                    return os.path.getsize(path)
                except FileNotFoundError:
                    return 0
                except OSError:
                    return None

            page_size_bytes = int(self.conn.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(self.conn.execute("PRAGMA page_count").fetchone()[0])
            freelist_count = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
            try:
                filesystem_free_bytes: int | None = int(shutil.disk_usage(os.path.dirname(self.db_path)).free)
            except OSError:
                filesystem_free_bytes = None
            database_file_bytes = file_size(self.db_path)
            wal_file_bytes = file_size(f"{self.db_path}-wal")
            shm_file_bytes = file_size(f"{self.db_path}-shm")
            if filesystem_free_bytes is None or None in (database_file_bytes, wal_file_bytes, shm_file_bytes):
                capacity_observation = "UNKNOWN"
                mutation_preflight = "BLOCKED_UNKNOWN_CAPACITY"
                next_action = "RESTORE_CAPACITY_OBSERVABILITY_BEFORE_MEMORY_MUTATION"
            else:
                capacity_observation = "KNOWN"
                mutation_preflight = "REQUIRES_BOUNDED_OPERATION_REQUIREMENT"
                next_action = "COMPARE_FILESYSTEM_FREE_BYTES_TO_BOUNDED_OPERATION_REQUIREMENT"

            return {
                "available": True,
                "backend": "sqlite_vec",
                "profile": self.profile.to_dict(),
                "embedding_compatibility": {
                    "state": self.embedding_compatibility_state,
                    "reason": self.embedding_compatibility_reason,
                    "store_key": self.embedding_store_key,
                    "vector_dimension": self.vector_dimension,
                    "binding": self.embedding_profile_metadata,
                },
                "counts": counts,
                "native_columns": sorted(self.native_columns),
                "retention": {
                    "policy_version": 1,
                    "trigger": "CAPACITY_PRESSURE_NOT_SESSION_COUNT",
                    "automatic_housekeeping": False,
                    "eligible_lifecycle_states": ["SUPERSEDED"],
                    "current_authority_reclaim": False,
                    "duplicate_reclaim": "NOT_APPLICABLE_IN_CURRENT_SCHEMA",
                    "expired_reclaim": "NOT_APPLICABLE_IN_CURRENT_SCHEMA",
                    "superseded_total": counts["SUPERSEDED"],
                    "compaction_ready": compaction_ready,
                    "compaction_blocked_partial": compaction_blocked_partial,
                    "historical_total": counts["HISTORICAL"],
                    "validated_current_total": counts["VALIDATED_CURRENT"],
                },
                "capacity": {
                    "observation": capacity_observation,
                    "page_size_bytes": page_size_bytes,
                    "page_count": page_count,
                    "freelist_count": freelist_count,
                    "database_allocated_bytes": page_size_bytes * page_count,
                    "freelist_bytes": page_size_bytes * freelist_count,
                    "database_file_bytes": database_file_bytes,
                    "wal_file_bytes": wal_file_bytes,
                    "shm_file_bytes": shm_file_bytes,
                    "filesystem_free_bytes": filesystem_free_bytes,
                    "pressure_threshold_bytes": None,
                    "pressure_evaluation": "REQUIRES_BOUNDED_OPERATION_REQUIREMENT",
                    "mutation_preflight": mutation_preflight,
                    "next_action": next_action,
                },
            }

        return await self._run(query)

    async def close(self) -> None:
        if self._closed:
            return
        if self.storage is not None:
            try:
                await self.storage.close()
            except asyncio.CancelledError:
                # The backend may still own SQLite handles. Keep the shared lease.
                raise
            except Exception as exc:
                # Never make recovery eligible while backend handle closure is uncertain.
                raise AdapterError(
                    "BACKEND_UNAVAILABLE",
                    "backend could not be closed cleanly; maintenance exclusion remains active",
                ) from exc
        self.conn = None
        self.storage = None
        _release_runtime_maintenance_lease(self._maintenance_lease_handle)
        self._maintenance_lease_handle = None
        self._closed = True
