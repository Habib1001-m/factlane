from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .contract import PUBLIC_TOOL_NAMES, AdapterError, canonical_json
from .embeddings import EmbeddingProfile
from .storage import (
    SQLiteVecEngine,
    _MaintenanceCapability,
    _make_maintenance_capability,
    _maintenance_lock_path,
    assert_supported_sqlite_runtime,
    register_storage_v2_writer,
)


_SAFE_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PUBLIC_TOOLS = (
    "memory_search",
    "memory_get",
    "memory_store",
    "memory_update",
    "memory_status",
)
_RECOVERY_SENTINEL = "[purged-sensitive-memory]"
_LOGICAL_PURGE_IN_PROGRESS = "S1_LOGICAL_PURGE_IN_PROGRESS"
_SEALING_INCOMPLETE = "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
_LOCAL_PURGE_VERIFIED = "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
_STATE_PHASE_RANK = {
    "PREPARED_NO_MUTATION": 0,
    _LOGICAL_PURGE_IN_PROGRESS: 1,
    _SEALING_INCOMPLETE: 2,
    _LOCAL_PURGE_VERIFIED: 3,
}
_KNOWN_RECOVERY_TABLES = {
    "adapter_meta", "adapter_records", "beliefs", "memories",
    "memory_content_fts", "memory_content_fts_config", "memory_content_fts_data",
    "memory_content_fts_docsize", "memory_content_fts_idx", "memory_embeddings",
    "memory_embeddings_chunks", "memory_embeddings_info", "memory_embeddings_rowids",
    "memory_embeddings_vector_chunks00", "memory_graph", "metadata", "migration_registry",
}

PM0 = "PM0_ADAPTER_ONLY_EXPECTED"
PM1 = "PM1_FULL_NATIVE_MATERIALIZATION"
PM2 = "PM2_NATIVE_PRESENT_VECTOR_MISSING_OR_MISMATCHED"
PM3 = "PM3_NATIVE_MISSING_VECTOR_ORPHAN_PRESENT"
PM4 = "PM4_NATIVE_MISSING_NO_VECTOR_ATTRIBUTABLE"
PM5 = "PM5_UNEXPECTED_DERIVED_STATE"
_MATERIALIZATION_CLASSES = {PM0, PM1, PM2, PM3, PM4, PM5}


class RecoveryHold(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True, slots=True)
class RecoveryScope:
    scope: str
    project_id: str | None = None
    worktree_id: str | None = None
    workflow_id: str | None = None
    agent_id: str | None = None

    def tuple(self) -> tuple[str | None, ...]:
        return (self.scope, self.project_id, self.worktree_id, self.workflow_id, self.agent_id)


@dataclass(frozen=True, slots=True)
class RecoveryTarget:
    record_id: str
    memory_id: str
    revision: int
    scope: RecoveryScope
    expected_lifecycle: str
    expected_materialization: str
    expected_native_hash: str | None

    def __post_init__(self) -> None:
        if not self.record_id or not self.memory_id:
            raise ValueError("record_id and memory_id are required")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if self.expected_materialization not in _MATERIALIZATION_CLASSES:
            raise ValueError("unsupported materialization class")
        if self.expected_materialization == PM1 and not self.expected_native_hash:
            raise ValueError("PM1 requires expected_native_hash")


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    operation_id: str
    db_path: str
    profile: EmbeddingProfile
    targets: tuple[RecoveryTarget, ...]
    incident_class: str = "S1"
    propagation_state: str = "EXPLICIT_TARGET_SET_COMPLETE"
    retained_identity_fields_classified_non_sensitive: bool = False
    safe_ids_for_receipt: bool = False

    def __post_init__(self) -> None:
        if not _SAFE_OPERATION_RE.fullmatch(self.operation_id):
            raise ValueError("operation_id must be a bounded non-secret operator identifier")
        if self.incident_class not in {"S1", "M1"}:
            raise ValueError("only exact S1/M1 recovery plans are supported")
        if self.propagation_state == "ADDITIONAL_CONTAMINATED_RECORD_DISCOVERED":
            raise ValueError("additional contaminated record requires target-set refreeze before mutation")
        if self.propagation_state != "EXPLICIT_TARGET_SET_COMPLETE":
            raise ValueError("unknown or unbounded propagation is U1 and cannot be mutated")
        if not self.retained_identity_fields_classified_non_sensitive:
            raise ValueError("retained identity/scope fields must be independently classified non-sensitive")
        if not self.targets:
            raise ValueError("at least one exact target is required")
        record_ids = [target.record_id for target in self.targets]
        if len(set(record_ids)) != len(record_ids):
            raise ValueError("record_id targets must be unique")


@dataclass(frozen=True, slots=True)
class TargetInspection:
    target: RecoveryTarget
    materialization: str
    native_rowid: int | None
    graph_edges: int
    already_purged: bool
    observed_native_hash: str | None
    superseded_refs: int


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    operation_id: str
    state: str
    logical_purge_committed: bool
    promoted: bool
    receipt_path: str
    state_path: str
    target_count: int


@dataclass(frozen=True, slots=True)
class SurvivorSnapshot:
    adapter_count: int
    adapter_digest: str
    native_count: int
    native_digest: str
    vector_count: int
    vector_digest: str
    graph_count: int
    graph_digest: str


def _safe_digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _db_identity(db_path: str) -> str:
    return _safe_digest("factlane-db-path", os.path.abspath(db_path))


def _profile_projection(profile: EmbeddingProfile) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id, "provider_kind": profile.provider_kind,
        "base_model_identity": profile.base_model_identity, "model_digest": profile.model_digest,
        "source_dimension": profile.source_dimension, "output_dimension": profile.output_dimension,
        "normalization_policy": profile.normalization_policy, "distance_metric": profile.distance_metric,
        "projection_version": profile.projection_version,
    }


def _plan_binding(plan: RecoveryPlan) -> str:
    safe_projection = {
        "operation_id": plan.operation_id,
        "db_identity": _db_identity(plan.db_path),
        "incident_class": plan.incident_class,
        "propagation_state": plan.propagation_state,
        "profile": _profile_projection(plan.profile),
        "targets": [
            {
                "record_id": target.record_id,
                "memory_id": target.memory_id,
                "revision": target.revision,
                "scope": target.scope.tuple(),
                "expected_lifecycle": target.expected_lifecycle,
                "expected_materialization": target.expected_materialization,
            }
            for target in plan.targets
        ],
    }
    return hashlib.sha256(canonical_json(safe_projection).encode("utf-8")).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    with open(tmp, "wb") as handle:
        os.chmod(tmp, 0o600)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class MaintenanceLease:
    def __init__(self, db_path: str, operation_id: str) -> None:
        self.path = _maintenance_lock_path(db_path)
        self.db_path = os.path.realpath(os.path.abspath(db_path))
        self.operation_id = operation_id
        self._handle: Any = None
        self.capability: _MaintenanceCapability | None = None

    def __enter__(self) -> "MaintenanceLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RecoveryHold("HOLD_NONQUIESCENT_NO_MUTATION", "exclusive recovery maintenance lease is unavailable") from exc
        handle.seek(0); handle.truncate()
        handle.write(f"operation_id={self.operation_id}\npid={os.getpid()}\n")
        handle.flush(); os.fsync(handle.fileno())
        self._handle = handle
        self.capability = _make_maintenance_capability(self.db_path, handle)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close(); self._handle = None; self.capability = None


def linux_foreign_db_handles(db_path: str) -> list[dict[str, Any]]:
    proc = Path("/proc")
    if not proc.is_dir():
        raise RecoveryHold("HOLD_NONQUIESCENT_NO_MUTATION", "process handle inventory is unavailable")
    targets = {os.path.realpath(db_path), os.path.realpath(db_path + "-wal"), os.path.realpath(db_path + "-shm")}
    matches: list[dict[str, Any]] = []
    try:
        entries = list(proc.iterdir())
    except OSError as exc:
        raise RecoveryHold("HOLD_NONQUIESCENT_NO_MUTATION", "process handle inventory could not be enumerated") from exc
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as exc:
            raise RecoveryHold(
                "HOLD_NONQUIESCENT_NO_MUTATION",
                f"process handle inventory is incomplete for pid {entry.name}",
            ) from exc
        for fd in fds:
            try:
                raw = os.readlink(fd)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError as exc:
                raise RecoveryHold(
                    "HOLD_NONQUIESCENT_NO_MUTATION",
                    f"process handle inventory is incomplete for pid {entry.name}",
                ) from exc
            if raw.endswith(" (deleted)"):
                raw = raw[:-10]
            resolved = os.path.realpath(raw)
            if resolved in targets:
                matches.append({"pid": int(entry.name), "fd": fd.name, "path": resolved})
    return matches


class SensitiveMemoryRecoveryOperator:
    """Trusted local maintenance operator intentionally absent from MCP/Gateway."""

    def __init__(self, *, foreign_handle_provider=linux_foreign_db_handles) -> None:
        self._foreign_handle_provider = foreign_handle_provider

    async def execute(
        self,
        plan: RecoveryPlan,
        *,
        receipt_path: str,
        state_path: str | None = None,
    ) -> RecoveryResult:
        db_path = os.path.abspath(plan.db_path)
        receipt = Path(receipt_path).absolute()
        state = Path(state_path).absolute() if state_path else receipt.with_suffix(receipt.suffix + ".state.json")
        if os.path.realpath(receipt) == os.path.realpath(db_path) or os.path.realpath(state) == os.path.realpath(db_path):
            raise ValueError("receipt/state must be outside the primary memory DB")
        try:
            assert_supported_sqlite_runtime()
        except AdapterError as exc:
            raise RecoveryHold(
                "HOLD_SQLITE_RUNTIME_UNSUPPORTED_NO_MUTATION",
                exc.safe_message,
            ) from exc
        with MaintenanceLease(db_path, plan.operation_id) as maintenance_lease:
            self._require_quiescent(db_path)
            persisted = self._read_state(state, plan)
            if persisted is None:
                self._write_state(state, plan, "PREPARED_NO_MUTATION")
                persisted_phase = "PREPARED_NO_MUTATION"
            else:
                persisted_phase = str(persisted["phase"])
            conn = self._open_maintenance_connection(db_path)
            logical_committed = False
            inspections: list[TargetInspection] = []
            try:
                self._preflight_database(conn, plan)
                inspections = self._inspect_targets(conn, plan)
                if all(item.already_purged for item in inspections):
                    if persisted_phase not in {
                        _LOGICAL_PURGE_IN_PROGRESS,
                        _SEALING_INCOMPLETE,
                        _LOCAL_PURGE_VERIFIED,
                    }:
                        raise RecoveryHold(
                            "HOLD_RECOVERY_STATE_MISMATCH",
                            "purged targets are not backed by resumable persisted recovery state",
                        )
                    logical_committed = True
                    if persisted_phase == _LOGICAL_PURGE_IN_PROGRESS:
                        self._write_state(state, plan, _SEALING_INCOMPLETE)
                else:
                    if any(item.already_purged for item in inspections):
                        raise RecoveryHold("HOLD_TARGET_SET_PARTIALLY_APPLIED", "target set is partially applied; manual reconciliation is required")
                    if persisted_phase in {_SEALING_INCOMPLETE, _LOCAL_PURGE_VERIFIED}:
                        raise RecoveryHold(
                            "HOLD_RECOVERY_STATE_MISMATCH",
                            "persisted recovery state claims commit but exact tombstones are absent",
                        )
                    self._write_state(state, plan, _LOGICAL_PURGE_IN_PROGRESS)
                    self._apply_logical_purge(conn, plan, inspections)
                    logical_committed = True
                    self._write_state(state, plan, _SEALING_INCOMPLETE)
                survivor_snapshot = self._snapshot_survivors(conn, plan)
                self._write_state(state, plan, _SEALING_INCOMPLETE)
                self._require_quiescent(db_path)
                self._seal_and_promote(conn, plan, survivor_snapshot)
                conn = None
                assert maintenance_lease.capability is not None
                await self._real_engine_postflight(
                    plan,
                    survivor_snapshot,
                    maintenance_lease.capability,
                )
                self._write_state(state, plan, _LOCAL_PURGE_VERIFIED)
                _atomic_json(receipt, self._receipt(plan, inspections))
                return RecoveryResult(plan.operation_id, _LOCAL_PURGE_VERIFIED, True, True, str(receipt), str(state), len(plan.targets))
            except RecoveryHold:
                if logical_committed:
                    self._write_state(state, plan, _SEALING_INCOMPLETE)
                raise
            except Exception as exc:
                if logical_committed:
                    self._write_state(state, plan, _SEALING_INCOMPLETE)
                    raise RecoveryHold(_SEALING_INCOMPLETE, "logical purge committed but sealing or verification did not complete") from exc
                raise
            finally:
                if conn is not None:
                    conn.close()

    @staticmethod
    def _read_state(path: Path, plan: RecoveryPlan) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "persisted recovery state is unreadable") from exc
        expected = {
            "operation_id": plan.operation_id,
            "db_identity": _db_identity(plan.db_path),
            "target_count": len(plan.targets),
            "plan_binding": _plan_binding(plan),
            "raw_content_logged": False,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "persisted recovery state does not match the frozen plan")
        if payload.get("phase") not in _STATE_PHASE_RANK:
            raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "persisted recovery state has an unknown phase")
        return payload

    @classmethod
    def _write_state(cls, path: Path, plan: RecoveryPlan, phase: str) -> None:
        existing = cls._read_state(path, plan)
        if existing is not None and _STATE_PHASE_RANK[str(existing["phase"])] > _STATE_PHASE_RANK[phase]:
            return
        _atomic_json(path, {
            "operation_id": plan.operation_id,
            "db_identity": _db_identity(plan.db_path),
            "phase": phase,
            "target_count": len(plan.targets),
            "plan_binding": _plan_binding(plan),
            "raw_content_logged": False,
        })

    def _require_quiescent(self, db_path: str) -> None:
        try:
            matches = self._foreign_handle_provider(db_path)
        except RecoveryHold:
            raise
        except Exception as exc:
            raise RecoveryHold(
                "HOLD_NONQUIESCENT_NO_MUTATION",
                "process handle inventory could not prove maintenance quiescence",
            ) from exc
        if matches:
            raise RecoveryHold(
                "HOLD_NONQUIESCENT_NO_MUTATION",
                "non-operator DB/WAL/SHM handles are present",
            )

    @staticmethod
    def _open_maintenance_connection(db_path: str) -> sqlite3.Connection:
        if not os.path.isfile(db_path):
            raise RecoveryHold("HOLD_DB_NOT_FOUND_NO_MUTATION", "target database was not found")
        conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        register_storage_v2_writer(conn)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception as exc:
            conn.close()
            raise RecoveryHold(
                "HOLD_SQLITE_VEC_UNAVAILABLE_NO_MUTATION",
                "pinned sqlite-vec extension could not be loaded",
            ) from exc
        return conn

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
            (name,),
        ).fetchone() is not None

    @staticmethod
    def _require_known_recovery_schema(conn: sqlite3.Connection) -> None:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
        if tables - _KNOWN_RECOVERY_TABLES:
            raise RecoveryHold("HOLD_UNEXPECTED_DERIVED_STATE_NO_MUTATION", "database contains tables outside the frozen recovery schema")

    def _preflight_database(self, conn: sqlite3.Connection, plan: RecoveryPlan) -> None:
        self._require_known_recovery_schema(conn)
        if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
            raise RecoveryHold("HOLD_UNSUPPORTED_DB_MODE_NO_MUTATION", "database must already use WAL mode")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RecoveryHold("HOLD_DB_INTEGRITY_NO_MUTATION", "quick_check failed")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RecoveryHold("HOLD_DB_INTEGRITY_NO_MUTATION", "integrity_check failed")

        conn.execute("PRAGMA secure_delete=ON")
        if int(conn.execute("PRAGMA secure_delete").fetchone()[0]) != 1:
            raise RecoveryHold("HOLD_SECURE_DELETE_UNAVAILABLE_NO_MUTATION", "core secure_delete is unavailable")
        if not self._table_exists(conn, "memory_content_fts"):
            raise RecoveryHold("HOLD_FTS_UNAVAILABLE_NO_MUTATION", "memory_content_fts is unavailable")
        try:
            conn.execute("BEGIN")
            conn.execute("INSERT INTO memory_content_fts(memory_content_fts, rank) VALUES('secure-delete', 1)")
            config = dict(conn.execute("SELECT k, v FROM memory_content_fts_config").fetchall())
            if int(config.get("secure-delete", 0)) != 1:
                raise RecoveryHold(
                    "HOLD_FTS_SECURE_DELETE_UNAVAILABLE_NO_MUTATION",
                    "FTS5 secure-delete did not remain enabled during capability probe",
                )
            conn.rollback()
        except RecoveryHold:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise RecoveryHold(
                "HOLD_FTS_SECURE_DELETE_UNAVAILABLE_NO_MUTATION",
                "FTS5 secure-delete is unavailable",
            ) from exc

        profile_row = conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone()
        if not profile_row:
            raise RecoveryHold("HOLD_PROFILE_MISMATCH_NO_MUTATION", "embedding profile metadata is missing")
        try:
            actual_profile = json.loads(str(profile_row[0]))
        except json.JSONDecodeError as exc:
            raise RecoveryHold("HOLD_PROFILE_MISMATCH_NO_MUTATION", "embedding profile metadata is invalid") from exc
        if actual_profile != _profile_projection(plan.profile):
            raise RecoveryHold("HOLD_PROFILE_MISMATCH_NO_MUTATION", "embedding profile does not match the frozen plan")

        checkpoint = tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        if checkpoint != (0, 0, 0):
            raise RecoveryHold("HOLD_NONQUIESCENT_NO_MUTATION", "pre-mutation WAL checkpoint did not complete cleanly")
        if tuple(PUBLIC_TOOL_NAMES) != _PUBLIC_TOOLS:
            raise RecoveryHold("HOLD_PUBLIC_CONTRACT_DRIFT_NO_MUTATION", "public MCP tool boundary drifted")

    def _inspect_targets(self, conn: sqlite3.Connection, plan: RecoveryPlan) -> list[TargetInspection]:
        return [self._inspect_target(conn, plan, target) for target in plan.targets]

    def _inspect_target(self, conn: sqlite3.Connection, plan: RecoveryPlan, target: RecoveryTarget) -> TargetInspection:
        row = conn.execute(
            "SELECT * FROM adapter_records WHERE record_id = ?",
            (target.record_id,),
        ).fetchone()
        if row is None:
            raise RecoveryHold("HOLD_TARGET_DRIFT_NO_MUTATION", "exact target record is missing")
        if self._is_same_operation_tombstone(row, plan, target):
            return TargetInspection(target, target.expected_materialization, None, 0, True, target.expected_native_hash, 0)
        actual_scope = (row["scope"], row["project_id"], row["worktree_id"], row["workflow_id"], row["agent_id"])
        actual_native_hash = str(row["native_content_hash"])
        if (row["memory_id"] != target.memory_id or int(row["revision"]) != target.revision
            or actual_scope != target.scope.tuple() or row["lifecycle_state"] != target.expected_lifecycle
            or (target.expected_native_hash is not None and actual_native_hash != target.expected_native_hash)):
            raise RecoveryHold("HOLD_TARGET_DRIFT_NO_MUTATION", "exact target binding changed")

        child_count = int(conn.execute("SELECT COUNT(*) FROM adapter_records WHERE parent_record_id = ?", (target.record_id,)).fetchone()[0])
        sibling_count = 1
        if row["parent_record_id"] is not None:
            sibling_count = int(conn.execute("SELECT COUNT(*) FROM adapter_records WHERE parent_record_id = ?", (row["parent_record_id"],)).fetchone()[0])
        if child_count > 1 or sibling_count > 1:
            raise RecoveryHold("HOLD_UNEXPECTED_DERIVED_STATE_NO_MUTATION", "target participates in an unexpected lineage fork")

        native_rows = conn.execute(
            "SELECT id, deleted_at FROM memories WHERE content_hash = ?",
            (actual_native_hash,),
        ).fetchall()
        native_rowid: int | None = None
        if len(native_rows) > 1:
            actual = PM5
        elif native_rows:
            native_rowid = int(native_rows[0]["id"])
            if native_rows[0]["deleted_at"] is not None:
                actual = PM5
            else:
                stores = [
                    str(value[0])
                    for value in conn.execute(
                        "SELECT store FROM memory_embeddings WHERE rowid = ?",
                        (native_rowid,),
                    ).fetchall()
                ]
                actual = PM1 if stores == [plan.profile.profile_id] else PM2
        else:
            orphan_vectors = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_embeddings e WHERE NOT EXISTS "
                    "(SELECT 1 FROM memories m WHERE m.id = e.rowid)"
                ).fetchone()[0]
            )
            if orphan_vectors:
                actual = PM3
            elif target.expected_materialization == PM0:
                actual = PM0
            else:
                actual = PM4

        if self._belief_refs_hash(conn, actual_native_hash):
            actual = PM5
        if actual == PM0 and row["lifecycle_state"] != "HISTORICAL":
            raise RecoveryHold(
                "HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION",
                "adapter-only PM0 is allowed only for proven HISTORICAL records",
            )
        if actual in {PM2, PM3, PM4, PM5}:
            code = {
                PM2: "HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION",
                PM3: "HOLD_UNATTRIBUTABLE_VECTOR_NO_MUTATION",
                PM4: "HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION",
                PM5: "HOLD_UNEXPECTED_DERIVED_STATE_NO_MUTATION",
            }[actual]
            raise RecoveryHold(code, "target materialization is not safe for surgical purge")
        if actual != target.expected_materialization:
            raise RecoveryHold(
                "HOLD_MATERIALIZATION_DRIFT_NO_MUTATION",
                "target materialization does not match the immutable plan",
            )

        graph_edges = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_graph WHERE source_hash = ? OR target_hash = ?",
                (actual_native_hash, actual_native_hash),
            ).fetchone()[0]
        )
        native_columns = {str(value[1]) for value in conn.execute("PRAGMA table_info(memories)").fetchall()}
        superseded_refs = 0
        if "superseded_by" in native_columns:
            superseded_refs = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE superseded_by = ?",
                    (actual_native_hash,),
                ).fetchone()[0]
            )
        return TargetInspection(
            target,
            actual,
            native_rowid,
            graph_edges,
            False,
            actual_native_hash,
            superseded_refs,
        )

    @staticmethod
    def _belief_refs_hash(conn: sqlite3.Connection, content_hash: str | None) -> bool:
        if not content_hash or not SensitiveMemoryRecoveryOperator._table_exists(conn, "beliefs"):
            return False
        for row in conn.execute("SELECT derived_from, contradicted_by FROM beliefs").fetchall():
            for raw in (row[0], row[1]):
                try:
                    values = json.loads(raw or "[]")
                except json.JSONDecodeError:
                    return True
                if not isinstance(values, list):
                    return True
                if any(
                    not isinstance(value, str)
                    or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None
                    for value in values
                ):
                    return True
                if content_hash in values:
                    return True
        return False

    @staticmethod
    def _expected_tombstone_values(plan: RecoveryPlan, target: RecoveryTarget) -> dict[str, Any]:
        return {
            "fact": _RECOVERY_SENTINEL,
            "source_provenance": canonical_json({
                "source_class": "RECOVERY_TOMBSTONE",
                "source_ref": f"recovery:{plan.operation_id}",
                "source_hash": _safe_digest(plan.operation_id, target.record_id, "provenance"),
                "review_ref": "operator-recovery",
                "extraction_method": "SENSITIVE_MEMORY_PURGE",
            }),
            "source_timestamp": None,
            "last_verified_at": None,
            "verified_by": "UNVERIFIED",
            "authority_role": "UNRESOLVED",
            "freshness_policy": canonical_json({
                "kind": "manual",
                "ttl_seconds": None,
                "recheck_ref": None,
                "source_fingerprint": None,
            }),
            "supersedes": "[]",
            "contradiction_key": _safe_digest(plan.operation_id, target.record_id, "contradiction"),
            "contradiction_state": "QUARANTINED",
            "confidence": 0.0,
            "tags": "[]",
            "lifecycle_state": "QUARANTINED",
            "native_content_hash": _safe_digest(plan.operation_id, target.record_id, "native"),
            "payload_fingerprint": _safe_digest(plan.operation_id, target.record_id, "fingerprint"),
            "idempotency_key": f"recovery:{plan.operation_id}:{_safe_digest(target.record_id)[:24]}",
        }

    @classmethod
    def _is_same_operation_tombstone(
        cls, row: sqlite3.Row, plan: RecoveryPlan, target: RecoveryTarget
    ) -> bool:
        actual_scope = (row["scope"], row["project_id"], row["worktree_id"], row["workflow_id"], row["agent_id"])
        if (
            row["record_id"] != target.record_id
            or row["memory_id"] != target.memory_id
            or int(row["revision"]) != target.revision
            or actual_scope != target.scope.tuple()
        ):
            return False
        expected = cls._expected_tombstone_values(plan, target)
        return all(row[field] == value for field, value in expected.items())

    def _apply_logical_purge(
        self,
        conn: sqlite3.Connection,
        plan: RecoveryPlan,
        inspections: list[TargetInspection],
    ) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._require_quiescent(plan.db_path)
            conn.execute("INSERT INTO memory_content_fts(memory_content_fts, rank) VALUES('secure-delete', 1)")
            config = dict(conn.execute("SELECT k, v FROM memory_content_fts_config").fetchall())
            if int(config.get("secure-delete", 0)) != 1:
                raise RecoveryHold(
                    "HOLD_FTS_SECURE_DELETE_UNAVAILABLE_NO_MUTATION",
                    "FTS5 secure-delete did not remain enabled inside the logical purge transaction",
                )
            current = [self._inspect_target(conn, plan, item.target) for item in inspections]
            if any(item.already_purged for item in current):
                raise RecoveryHold(
                    "HOLD_TARGET_SET_PARTIALLY_APPLIED",
                    "target changed between preflight and mutation",
                )
            for item in current:
                self._tombstone_adapter_row(conn, plan, item.target)

            native_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
            old_hashes = tuple(
                dict.fromkeys(
                    old_hash
                    for item in current
                    if (old_hash := item.observed_native_hash or item.target.expected_native_hash)
                    is not None
                )
            )
            if len(old_hashes) != len(current):
                raise RecoveryHold(
                    "HOLD_TARGET_DRIFT_NO_MUTATION",
                    "one or more target native hashes could not be bound for cleanup",
                )
            hash_placeholders = ",".join("?" for _ in old_hashes)
            expected_graph_edges = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM memory_graph WHERE source_hash IN ({hash_placeholders}) "
                    f"OR target_hash IN ({hash_placeholders})",
                    (*old_hashes, *old_hashes),
                ).fetchone()[0]
            )
            removed_graph = conn.execute(
                f"DELETE FROM memory_graph WHERE source_hash IN ({hash_placeholders}) "
                f"OR target_hash IN ({hash_placeholders})",
                (*old_hashes, *old_hashes),
            )
            if removed_graph.rowcount != expected_graph_edges:
                raise RecoveryHold(
                    "HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION",
                    "exact target graph references changed during recovery",
                )

            if "superseded_by" in native_columns:
                expected_superseded_refs = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM memories WHERE superseded_by IN ({hash_placeholders})",
                        old_hashes,
                    ).fetchone()[0]
                )
                repaired_refs = conn.execute(
                    f"UPDATE memories SET superseded_by = NULL "
                    f"WHERE superseded_by IN ({hash_placeholders})",
                    old_hashes,
                )
                if repaired_refs.rowcount != expected_superseded_refs:
                    raise RecoveryHold(
                        "HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION",
                        "exact target superseded_by references changed during recovery",
                    )

            for item in current:
                old_hash = item.observed_native_hash or item.target.expected_native_hash
                if old_hash is None:
                    continue
                if item.native_rowid is not None:
                    removed = conn.execute(
                        "DELETE FROM memory_embeddings WHERE rowid = ? AND store = ?",
                        (item.native_rowid, plan.profile.profile_id),
                    )
                    if item.materialization == PM1 and removed.rowcount != 1:
                        raise RecoveryHold(
                            "HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION",
                            "exact target vector changed during recovery",
                        )
                if item.native_rowid is not None:
                    removed_native = conn.execute(
                        "DELETE FROM memories WHERE id = ? AND content_hash = ? AND deleted_at IS NULL",
                        (item.native_rowid, old_hash),
                    )
                    if removed_native.rowcount != 1:
                        raise RecoveryHold(
                            "HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION",
                            "exact target native row changed during recovery",
                        )

            conn.execute("INSERT INTO memory_content_fts(memory_content_fts) VALUES('rebuild')")
            conn.execute(
                "INSERT INTO memory_content_fts(memory_content_fts, rank) VALUES('integrity-check', 1)"
            )
            self._verify_logical_absence(conn, current)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _tombstone_adapter_row(
        conn: sqlite3.Connection,
        plan: RecoveryPlan,
        target: RecoveryTarget,
    ) -> None:
        values = SensitiveMemoryRecoveryOperator._expected_tombstone_values(plan, target)
        provenance = values["source_provenance"]
        freshness = values["freshness_policy"]
        contradiction_key = values["contradiction_key"]
        native_hash = values["native_content_hash"]
        fingerprint = values["payload_fingerprint"]
        idempotency = values["idempotency_key"]
        sql = (
            "UPDATE adapter_records SET fact = ?, source_provenance = ?, source_timestamp = NULL, "
            "last_verified_at = NULL, verified_by = 'UNVERIFIED', authority_role = 'UNRESOLVED', "
            "freshness_policy = ?, supersedes = '[]', contradiction_key = ?, "
            "contradiction_state = 'QUARANTINED', confidence = 0.0, tags = '[]', "
            "lifecycle_state = 'QUARANTINED', native_content_hash = ?, payload_fingerprint = ?, "
            "idempotency_key = ? WHERE record_id = ? AND memory_id = ? AND revision = ? "
            "AND lifecycle_state = ?"
        )
        params: tuple[Any, ...] = (
            _RECOVERY_SENTINEL, provenance, freshness, contradiction_key, native_hash,
            fingerprint, idempotency, target.record_id, target.memory_id, target.revision,
            target.expected_lifecycle,
        )
        if target.expected_native_hash is not None:
            sql += " AND native_content_hash = ?"
            params += (target.expected_native_hash,)
        updated = conn.execute(sql, params)
        if updated.rowcount != 1:
            raise RecoveryHold("HOLD_TARGET_DRIFT_NO_MUTATION", "target changed during tombstoning")

    @staticmethod
    def _verify_logical_absence(conn: sqlite3.Connection, inspections: Iterable[TargetInspection]) -> None:
        native_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
        for item in inspections:
            old_hash = item.observed_native_hash or item.target.expected_native_hash
            if old_hash is None:
                continue
            if conn.execute("SELECT 1 FROM memories WHERE content_hash = ? LIMIT 1", (old_hash,)).fetchone():
                raise RecoveryHold("HOLD_LOGICAL_PURGE_VERIFY_FAILED", "target native row remains")
            if item.native_rowid is not None and conn.execute("SELECT 1 FROM memory_embeddings WHERE rowid = ? LIMIT 1", (item.native_rowid,)).fetchone():
                raise RecoveryHold("HOLD_LOGICAL_PURGE_VERIFY_FAILED", "target vector remains")
            if conn.execute("SELECT 1 FROM memory_graph WHERE source_hash = ? OR target_hash = ? LIMIT 1", (old_hash, old_hash)).fetchone():
                raise RecoveryHold("HOLD_LOGICAL_PURGE_VERIFY_FAILED", "target graph edge remains")
            if "superseded_by" in native_columns and conn.execute(
                "SELECT 1 FROM memories WHERE superseded_by = ? LIMIT 1", (old_hash,)
            ).fetchone():
                raise RecoveryHold("HOLD_LOGICAL_PURGE_VERIFY_FAILED", "target superseded_by reference remains")
            if SensitiveMemoryRecoveryOperator._belief_refs_hash(conn, old_hash):
                raise RecoveryHold("HOLD_LOGICAL_PURGE_VERIFY_FAILED", "target belief reference remains")
        conn.execute("INSERT INTO memory_content_fts(memory_content_fts, rank) VALUES('integrity-check', 1)")
        orphan_count = int(conn.execute("SELECT COUNT(*) FROM memory_embeddings e WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = e.rowid)").fetchone()[0])
        if orphan_count:
            raise RecoveryHold("HOLD_LOGICAL_PURGE_VERIFY_FAILED", "vector orphan exists after logical purge")

    @staticmethod
    def _digest_query_rows(
        conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()
    ) -> tuple[int, str]:
        digest = hashlib.sha256()
        count = 0
        for row in conn.execute(sql, params).fetchall():
            count += 1
            for value in tuple(row):
                if value is None:
                    payload = b"N"
                elif isinstance(value, bytes):
                    payload = b"B" + value
                else:
                    payload = b"T" + str(value).encode("utf-8")
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
        return count, digest.hexdigest()

    def _snapshot_survivors(
        self, conn: sqlite3.Connection, plan: RecoveryPlan
    ) -> SurvivorSnapshot:
        record_ids = tuple(target.record_id for target in plan.targets)
        placeholders = ",".join("?" for _ in record_ids)
        adapter_count, adapter_digest = self._digest_query_rows(
            conn,
            f"SELECT * FROM adapter_records WHERE record_id NOT IN ({placeholders}) ORDER BY record_id",
            record_ids,
        )
        native_count, native_digest = self._digest_query_rows(
            conn, "SELECT * FROM memories ORDER BY id"
        )
        vector_count, vector_digest = self._digest_query_rows(
            conn, "SELECT rowid, * FROM memory_embeddings ORDER BY rowid, store"
        )
        graph_count, graph_digest = self._digest_query_rows(
            conn, "SELECT rowid, * FROM memory_graph ORDER BY rowid"
        )
        return SurvivorSnapshot(
            adapter_count, adapter_digest, native_count, native_digest,
            vector_count, vector_digest, graph_count, graph_digest,
        )

    def _verify_survivors(
        self, conn: sqlite3.Connection, plan: RecoveryPlan, expected: SurvivorSnapshot
    ) -> None:
        actual = self._snapshot_survivors(conn, plan)
        if actual != expected:
            raise RecoveryHold(
                _SEALING_INCOMPLETE,
                "unrelated survivor records or lineage changed during sealing/promotion",
            )

    def _seal_and_promote(
        self, conn: sqlite3.Connection, plan: RecoveryPlan, survivor_snapshot: SurvivorSnapshot
    ) -> None:
        db_path = os.path.abspath(plan.db_path)
        checkpoint1 = tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        if checkpoint1 != (0, 0, 0):
            raise RecoveryHold(_SEALING_INCOMPLETE, "first post-commit checkpoint did not complete")
        sanitized = Path(f"{db_path}.recovery-{plan.operation_id}.sanitized")
        if sanitized.exists():
            sanitized.unlink()
        conn.execute("VACUUM INTO ?", (str(sanitized),))
        checkpoint2 = tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        if checkpoint2 != (0, 0, 0):
            raise RecoveryHold(_SEALING_INCOMPLETE, "second post-sealing checkpoint did not complete")
        self._verify_db_health_and_fts(conn)
        self._verify_sanitized_image(sanitized, plan, survivor_snapshot)
        conn.close()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(db_path + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise RecoveryHold(_SEALING_INCOMPLETE, "non-empty WAL/SHM remained after successful sealing checkpoint")
            if sidecar.exists():
                sidecar.unlink()
        os.replace(sanitized, db_path)

    @staticmethod
    def _verify_db_health_and_fts(conn: sqlite3.Connection) -> None:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RecoveryHold(_SEALING_INCOMPLETE, "post-sealing quick_check failed")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RecoveryHold(_SEALING_INCOMPLETE, "post-sealing integrity_check failed")
        conn.execute("INSERT INTO memory_content_fts(memory_content_fts, rank) VALUES('integrity-check', 1)")

    def _verify_sanitized_image(
        self, image: Path, plan: RecoveryPlan, survivor_snapshot: SurvivorSnapshot
    ) -> None:
        verify = sqlite3.connect(str(image), timeout=5.0, isolation_level=None)
        verify.row_factory = sqlite3.Row
        try:
            try:
                import sqlite_vec

                verify.enable_load_extension(True)
                sqlite_vec.load(verify)
                verify.enable_load_extension(False)
            except Exception as exc:
                raise RecoveryHold(
                    _SEALING_INCOMPLETE,
                    "sqlite-vec could not be loaded for sanitized-image verification",
                ) from exc
            verify.execute("PRAGMA secure_delete=ON")
            self._verify_db_health_and_fts(verify)
            profile_row = verify.execute(
                "SELECT value FROM adapter_meta WHERE key='embedding_profile'"
            ).fetchone()
            if not profile_row or json.loads(str(profile_row[0])) != _profile_projection(plan.profile):
                raise RecoveryHold(_SEALING_INCOMPLETE, "sanitized image profile drifted")
            self._verify_survivors(verify, plan, survivor_snapshot)
            for target in plan.targets:
                row = verify.execute(
                    "SELECT * FROM adapter_records WHERE record_id = ?",
                    (target.record_id,),
                ).fetchone()
                if row is None or not self._is_same_operation_tombstone(row, plan, target):
                    raise RecoveryHold(_SEALING_INCOMPLETE, "sanitized tombstone verification failed")
                old_hash = target.expected_native_hash
                if old_hash and verify.execute(
                    "SELECT 1 FROM memories WHERE content_hash = ? LIMIT 1", (old_hash,)
                ).fetchone():
                    raise RecoveryHold(_SEALING_INCOMPLETE, "sanitized image retained target native row")
            orphan_count = int(
                verify.execute(
                    "SELECT COUNT(*) FROM memory_embeddings e WHERE NOT EXISTS "
                    "(SELECT 1 FROM memories m WHERE m.id = e.rowid)"
                ).fetchone()[0]
            )
            if orphan_count:
                raise RecoveryHold(_SEALING_INCOMPLETE, "sanitized image contains vector orphans")
        finally:
            verify.close()

    async def _real_engine_postflight(
        self,
        plan: RecoveryPlan,
        survivor_snapshot: SurvivorSnapshot,
        maintenance_capability: _MaintenanceCapability,
    ) -> None:
        engine = SQLiteVecEngine._for_maintenance(
            os.path.abspath(plan.db_path),
            plan.profile,
            maintenance_capability,
        )
        await engine.open()
        try:
            if engine.conn is None:
                raise RecoveryHold(_SEALING_INCOMPLETE, "real engine did not reopen")
            engine.conn.row_factory = sqlite3.Row
            self._verify_db_health_and_fts(engine.conn)
            if tuple(PUBLIC_TOOL_NAMES) != _PUBLIC_TOOLS:
                raise RecoveryHold(_SEALING_INCOMPLETE, "public MCP tool boundary drifted")
            self._verify_survivors(engine.conn, plan, survivor_snapshot)
            for target in plan.targets:
                row = engine.conn.execute(
                    "SELECT * FROM adapter_records WHERE record_id = ?",
                    (target.record_id,),
                ).fetchone()
                if row is None or not self._is_same_operation_tombstone(row, plan, target):
                    raise RecoveryHold(_SEALING_INCOMPLETE, "real-engine tombstone verification failed")
        finally:
            await engine.close()

    @staticmethod
    def _receipt(plan: RecoveryPlan, inspections: list[TargetInspection]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "operation_id": plan.operation_id,
            "incident_class": plan.incident_class,
            "db_identity": _db_identity(plan.db_path),
            "result": "LOCAL_FACTLANE_PURGE_PASS",
            "recovery_state": _LOCAL_PURGE_VERIFIED,
            "target_count": len(plan.targets),
            "materialization_classes": [item.materialization for item in inspections],
            "core_secure_delete": True,
            "fts5_secure_delete": True,
            "fts_rebuild_in_logical_transaction": True,
            "fts_integrity_check": True,
            "first_wal_checkpoint_truncate": "PASS",
            "vacuum_into_sanitized_image": "PASS",
            "second_wal_checkpoint_truncate": "PASS",
            "post_sealing_db_fts_verification": "PASS",
            "public_mcp_tool_count": 5,
            "plaintext_pre_mutation_backup": False,
            "raw_content_logged": False,
            "content_derived_confirmation_oracle": False,
            "external_residual_copy_status": "UNKNOWN",
            "physical_erasure_claim": False,
        }
        if plan.safe_ids_for_receipt:
            result["safe_target_ids"] = [
                {"record_id": target.record_id, "memory_id": target.memory_id, "revision": target.revision}
                for target in plan.targets
            ]
        return result


async def execute_recovery(
    plan: RecoveryPlan,
    *,
    receipt_path: str,
    state_path: str | None = None,
    foreign_handle_provider: Callable[[str], list[dict[str, Any]]] = linux_foreign_db_handles,
) -> RecoveryResult:
    operator = SensitiveMemoryRecoveryOperator(foreign_handle_provider=foreign_handle_provider)
    return await operator.execute(plan, receipt_path=receipt_path, state_path=state_path)


def execute_recovery_sync(
    plan: RecoveryPlan,
    *,
    receipt_path: str,
    state_path: str | None = None,
    foreign_handle_provider: Callable[[str], list[dict[str, Any]]] = linux_foreign_db_handles,
) -> RecoveryResult:
    return asyncio.run(
        execute_recovery(
            plan,
            receipt_path=receipt_path,
            state_path=state_path,
            foreign_handle_provider=foreign_handle_provider,
        )
    )
