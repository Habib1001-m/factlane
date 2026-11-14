from __future__ import annotations

import inspect
import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from .contract import AdapterError, PUBLIC_TOOL_NAMES, validate_identifier, validate_scope
from .embeddings import EmbeddingProfile
from .owner_destructive import (
    OwnerDestructiveError,
    OwnerDestructiveOrchestrator,
    _atomic_json,
    _ensure_private_directory,
    _windows_private_file_exists,
)
from .recovery import (
    LifecyclePreservationSnapshot,
    PM0,
    PM1,
    RecoveryActivationHooks,
    RecoveryHold,
    RecoveryPlan,
    RecoveryResult,
    RecoveryScope,
    RecoveryTarget,
    SensitiveMemoryRecoveryOperator,
    SurvivorSnapshot,
    _ACTIVATION_READY,
    _LOCAL_PURGE_VERIFIED,
    _LOGICAL_PURGE_IN_PROGRESS,
    _PUBLIC_TOOLS,
    _SEALING_INCOMPLETE,
    _db_identity,
    _plan_binding,
    _recovery_store_key,
)
from .storage import MAX_PERMANENT_PURGE_TARGETS, register_storage_recovery_writer
from .windows_maintenance import WindowsFileIdentity, WindowsMaintenanceError
from . import windows_maintenance as _windows_maintenance


_STATE_VERSION = 1
_STATE_KIND = "FACTLANE_WINDOWS_OWNER_PERMANENT_PURGE_IN_PLACE_V1"
_PREPARED = "PREPARED_NO_MUTATION"
_PHASE_RANK = {
    _PREPARED: 0,
    _LOGICAL_PURGE_IN_PROGRESS: 1,
    _SEALING_INCOMPLETE: 2,
    _ACTIVATION_READY: 3,
    _LOCAL_PURGE_VERIFIED: 4,
}
_RECOVERY_INTERLOCK_KEY = "sensitive_recovery_interlock"


class _WindowsInPlacePurgeLease(Protocol):
    identity: WindowsFileIdentity
    connection: sqlite3.Connection

    def revalidate(self) -> Any: ...

    def flush_file_buffers(self) -> None: ...

    def prepare_runtime_activation(self) -> None: ...

    def retain_fail_closed(self) -> None: ...

    def close(self) -> None: ...


def _identity_mapping(identity: WindowsFileIdentity) -> dict[str, Any]:
    return {
        "volume_serial_number": int(identity.volume_serial_number),
        "file_id": identity.file_id_hex,
    }


def _validate_identity_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"volume_serial_number", "file_id"}:
        raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "Windows purge database identity is malformed")
    volume = value.get("volume_serial_number")
    file_id = value.get("file_id")
    if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
        raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "Windows purge volume identity is malformed")
    if (
        not isinstance(file_id, str)
        or len(file_id) != 32
        or any(character not in "0123456789abcdef" for character in file_id)
    ):
        raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "Windows purge file identity is malformed")
    return {"volume_serial_number": volume, "file_id": file_id}


def _same_identity(identity: WindowsFileIdentity, expected: Mapping[str, Any]) -> bool:
    return _identity_mapping(identity) == dict(expected)


def _survivor_mapping(snapshot: SurvivorSnapshot) -> dict[str, Any]:
    return {
        "adapter_count": snapshot.adapter_count,
        "adapter_digest": snapshot.adapter_digest,
        "native_count": snapshot.native_count,
        "native_digest": snapshot.native_digest,
        "vector_count": snapshot.vector_count,
        "vector_digest": snapshot.vector_digest,
        "graph_count": snapshot.graph_count,
        "graph_digest": snapshot.graph_digest,
        "lifecycle_event_count": snapshot.lifecycle_event_count,
        "lifecycle_event_digest": snapshot.lifecycle_event_digest,
        "lifecycle_operation_count": snapshot.lifecycle_operation_count,
        "lifecycle_operation_digest": snapshot.lifecycle_operation_digest,
    }


def _survivor_from_mapping(value: Any) -> SurvivorSnapshot:
    if not isinstance(value, dict):
        raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "persisted Windows purge survivor proof is missing")
    count_fields = (
        "adapter_count",
        "native_count",
        "vector_count",
        "graph_count",
        "lifecycle_event_count",
        "lifecycle_operation_count",
    )
    digest_fields = (
        "adapter_digest",
        "native_digest",
        "vector_digest",
        "graph_digest",
        "lifecycle_event_digest",
        "lifecycle_operation_digest",
    )
    expected_fields = set(count_fields) | set(digest_fields)
    if set(value) != expected_fields:
        raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "persisted Windows purge survivor proof is malformed")
    if any(
        isinstance(value[field], bool) or not isinstance(value[field], int) or value[field] < 0
        for field in count_fields
    ):
        raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "persisted Windows purge survivor counts are invalid")
    if any(
        not isinstance(value[field], str)
        or len(value[field]) != 64
        or any(character not in "0123456789abcdef" for character in value[field])
        for field in digest_fields
    ):
        raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "persisted Windows purge survivor digests are invalid")
    return SurvivorSnapshot(
        adapter_count=int(value["adapter_count"]),
        adapter_digest=str(value["adapter_digest"]),
        native_count=int(value["native_count"]),
        native_digest=str(value["native_digest"]),
        vector_count=int(value["vector_count"]),
        vector_digest=str(value["vector_digest"]),
        graph_count=int(value["graph_count"]),
        graph_digest=str(value["graph_digest"]),
        lifecycle_event_count=int(value["lifecycle_event_count"]),
        lifecycle_event_digest=str(value["lifecycle_event_digest"]),
        lifecycle_operation_count=int(value["lifecycle_operation_count"]),
        lifecycle_operation_digest=str(value["lifecycle_operation_digest"]),
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _state_exists(path: Path) -> bool:
    if os.name == "nt":
        try:
            return _windows_private_file_exists(path, code="PURGE_RECOVERY_STATE_MISMATCH")
        except OwnerDestructiveError as exc:
            raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", exc.safe_message) from exc
    return path.exists()


def _read_owner_private_json(path: Path) -> dict[str, Any]:
    try:
        return OwnerDestructiveOrchestrator._read_json_file(path, code="PURGE_RECOVERY_STATE_MISMATCH")
    except OwnerDestructiveError as exc:
        raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", exc.safe_message) from exc


class WindowsOwnerPurgeOperator:
    """Owner-panel-only permanent purge for native Windows.

    The general SensitiveMemoryRecoveryOperator intentionally remains POSIX-only.
    This class reuses its bounded SQLite mutation/proof helpers while a dedicated
    Windows in-place lease owns coordination and database-handle custody.
    """

    def __init__(self, *, maintenance_lease_factory: Callable[[str], Any] | None = None) -> None:
        self._lease_factory = maintenance_lease_factory
        self._semantic = SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _path: [])

    def _acquire_lease(self, db_path: str) -> _WindowsInPlacePurgeLease:
        if self._lease_factory is None:
            lease_type = getattr(_windows_maintenance, "WindowsInPlaceMaintenanceLease", None)
            acquire = getattr(lease_type, "acquire", None) if lease_type is not None else None
            if not callable(acquire):
                raise RecoveryHold(
                    "HOLD_WINDOWS_PURGE_MAINTENANCE_UNAVAILABLE",
                    "native Windows in-place purge maintenance is not available",
                )
            factory: Callable[[str], Any] = acquire
        else:
            factory = self._lease_factory
        try:
            lease = factory(db_path)
        except RecoveryHold:
            raise
        except WindowsMaintenanceError as exc:
            code = (
                "HOLD_NONQUIESCENT_NO_MUTATION"
                if exc.code in {"MAINTENANCE_IN_PROGRESS", "MAINTENANCE_HOLD_CONTENTION", "MAINTENANCE_HOLD_SQLITE_BUSY"}
                else "HOLD_WINDOWS_PURGE_MAINTENANCE_UNAVAILABLE"
            )
            raise RecoveryHold(code, exc.safe_message) from exc
        except Exception as exc:
            raise RecoveryHold(
                "HOLD_WINDOWS_PURGE_MAINTENANCE_UNAVAILABLE",
                "native Windows in-place purge maintenance could not be acquired",
            ) from exc
        connection = getattr(lease, "connection", None)
        if not isinstance(connection, sqlite3.Connection) and callable(connection):
            connection = connection()
        identity = getattr(lease, "identity", None)
        if not isinstance(identity, WindowsFileIdentity):
            identity = getattr(lease, "main_identity", None)
        if (
            not isinstance(identity, WindowsFileIdentity)
            or not isinstance(connection, sqlite3.Connection)
            or not callable(getattr(lease, "revalidate", None))
            or not callable(getattr(lease, "flush_file_buffers", None))
            or not callable(getattr(lease, "prepare_runtime_activation", None))
            or not callable(getattr(lease, "retain_fail_closed", None))
            or not callable(getattr(lease, "close", None))
        ):
            prepare = getattr(lease, "prepare_runtime_activation", None)
            if callable(prepare):
                try:
                    prepare()
                    lease.close()
                except Exception:
                    try:
                        lease.retain_fail_closed()
                    except Exception:
                        pass
            raise RecoveryHold(
                "HOLD_WINDOWS_PURGE_MAINTENANCE_UNAVAILABLE",
                "native Windows in-place purge lease does not expose the accepted panel contract",
            )
        lease.connection = connection
        connection.row_factory = sqlite3.Row
        register_storage_recovery_writer(connection)
        try:
            import sqlite_vec

            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
        except Exception as exc:
            try:
                connection.enable_load_extension(False)
            except Exception:
                pass
            try:
                lease.prepare_runtime_activation()
                setattr(lease, "_factlane_purge_runtime_prepared", True)
                lease.close()
            except Exception:
                try:
                    lease.retain_fail_closed()
                except Exception:
                    pass
            raise RecoveryHold(
                "HOLD_SQLITE_VEC_UNAVAILABLE_NO_MUTATION",
                "pinned sqlite-vec extension could not be loaded for Windows panel purge",
            ) from exc
        return lease

    @staticmethod
    def _runtime_activation_prepared(lease: _WindowsInPlacePurgeLease) -> bool:
        return bool(
            getattr(lease, "_factlane_purge_runtime_prepared", False)
            or getattr(lease, "_runtime_activation_prepared", False)
        )

    @classmethod
    def _prepare_runtime_activation(cls, lease: _WindowsInPlacePurgeLease) -> sqlite3.Connection:
        if not cls._runtime_activation_prepared(lease):
            try:
                lease.prepare_runtime_activation()
            except Exception as exc:
                try:
                    lease.retain_fail_closed()
                except Exception:
                    pass
                raise RecoveryHold(
                    _SEALING_INCOMPLETE,
                    "Windows purge runtime activation regime could not be restored safely",
                ) from exc
            setattr(lease, "_factlane_purge_runtime_prepared", True)
        connection = getattr(lease, "connection", None)
        if not isinstance(connection, sqlite3.Connection):
            raise RecoveryHold(
                _SEALING_INCOMPLETE,
                "Windows purge runtime activation connection is unavailable",
            )
        connection.row_factory = sqlite3.Row
        register_storage_recovery_writer(connection)
        return connection

    @classmethod
    def _release_after_failure(cls, lease: _WindowsInPlacePurgeLease) -> None:
        if bool(getattr(lease, "_retained", False)):
            return
        try:
            cls._prepare_runtime_activation(lease)
            lease.close()
        except Exception:
            try:
                lease.retain_fail_closed()
            except Exception:
                pass

    def _release_recoverable_postcommit(
        self,
        lease: _WindowsInPlacePurgeLease,
        conn: sqlite3.Connection,
        plan: RecoveryPlan,
        expected_identity: Mapping[str, Any],
    ) -> None:
        if bool(getattr(lease, "_retained", False)):
            return
        try:
            proof_conn = conn
            if self._runtime_activation_prepared(lease):
                runtime_conn = getattr(lease, "connection", None)
                if not isinstance(runtime_conn, sqlite3.Connection):
                    raise RecoveryHold(_SEALING_INCOMPLETE, "Windows purge recovery connection is unavailable")
                proof_conn = runtime_conn
            self._require_stable_identity(lease, expected_identity)
            if proof_conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RecoveryHold(_SEALING_INCOMPLETE, "Windows purge recovery database quick_check failed")
            if proof_conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RecoveryHold(_SEALING_INCOMPLETE, "Windows purge recovery database integrity_check failed")
            if self._semantic._recovery_interlock_operation(proof_conn) != plan.operation_id:
                raise RecoveryHold(_SEALING_INCOMPLETE, "Windows purge exact recovery interlock is not durable")
            for target in plan.targets:
                row = proof_conn.execute(
                    "SELECT * FROM adapter_records WHERE record_id=?",
                    (target.record_id,),
                ).fetchone()
                if row is None or not self._semantic._is_same_operation_tombstone(row, plan, target):
                    raise RecoveryHold(_SEALING_INCOMPLETE, "Windows purge recovery tombstones are not exact")
            self._flush_file(lease)
            self._prepare_runtime_activation(lease)
            lease.close()
        except Exception:
            try:
                lease.retain_fail_closed()
            except Exception:
                pass

    @staticmethod
    def _base_state(plan: RecoveryPlan, identity: WindowsFileIdentity) -> dict[str, Any]:
        return {
            "version": _STATE_VERSION,
            "kind": _STATE_KIND,
            "operation_id": plan.operation_id,
            "db_identity": _db_identity(plan.db_path),
            "plan_binding": _plan_binding(plan),
            "target_count": len(plan.targets),
            "phase": _PREPARED,
            "raw_content_logged": False,
            "database_identity": _identity_mapping(identity),
            "unrelated_lifecycle_snapshot": None,
            "survivor_snapshot": None,
            "receipt_payload": None,
        }

    @staticmethod
    def _validate_state(payload: dict[str, Any], plan: RecoveryPlan) -> dict[str, Any]:
        required = {
            "version",
            "kind",
            "operation_id",
            "db_identity",
            "plan_binding",
            "target_count",
            "phase",
            "raw_content_logged",
            "database_identity",
            "unrelated_lifecycle_snapshot",
            "survivor_snapshot",
            "receipt_payload",
        }
        if set(payload) != required or payload.get("version") != _STATE_VERSION or payload.get("kind") != _STATE_KIND:
            raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "persisted Windows purge state has an unsupported shape")
        expected = {
            "operation_id": plan.operation_id,
            "db_identity": _db_identity(plan.db_path),
            "plan_binding": _plan_binding(plan),
            "target_count": len(plan.targets),
            "raw_content_logged": False,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "persisted Windows purge state does not match the frozen plan")
        phase = payload.get("phase")
        if phase not in _PHASE_RANK:
            raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "persisted Windows purge phase is invalid")
        _validate_identity_mapping(payload.get("database_identity"))
        lifecycle = payload.get("unrelated_lifecycle_snapshot")
        survivor = payload.get("survivor_snapshot")
        receipt = payload.get("receipt_payload")
        if phase == _PREPARED:
            if lifecycle is not None or survivor is not None or receipt is not None:
                raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "pre-mutation Windows purge state carries post-mutation proof")
        elif phase == _LOGICAL_PURGE_IN_PROGRESS:
            LifecyclePreservationSnapshot.from_dict(lifecycle)
            if survivor is not None or receipt is not None:
                raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "logical Windows purge state carries sealing proof")
        else:
            LifecyclePreservationSnapshot.from_dict(lifecycle)
            if phase == _SEALING_INCOMPLETE:
                if survivor is not None:
                    _survivor_from_mapping(survivor)
            else:
                _survivor_from_mapping(survivor)
            if phase in {_ACTIVATION_READY, _LOCAL_PURGE_VERIFIED}:
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("operation_id") != plan.operation_id
                    or receipt.get("result") != "LOCAL_FACTLANE_PURGE_PASS"
                    or receipt.get("target_count") != len(plan.targets)
                ):
                    raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "persisted Windows purge receipt proof is invalid")
            elif receipt is not None:
                raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "sealing Windows purge state carries activation receipt")
        return payload

    def _read_state(self, path: Path, plan: RecoveryPlan) -> dict[str, Any] | None:
        if not _state_exists(path):
            return None
        return self._validate_state(_read_owner_private_json(path), plan)

    def _write_state(self, path: Path, plan: RecoveryPlan, payload: Mapping[str, Any]) -> dict[str, Any]:
        candidate = self._validate_state(dict(payload), plan)
        existing = self._read_state(path, plan)
        if existing is not None and _PHASE_RANK[str(candidate["phase"])] < _PHASE_RANK[str(existing["phase"])]:
            return existing
        try:
            _atomic_json(path, candidate)
        except OwnerDestructiveError as exc:
            raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", exc.safe_message) from exc
        return candidate

    @staticmethod
    def _lease_identity(lease: _WindowsInPlacePurgeLease) -> WindowsFileIdentity:
        observed = lease.revalidate()
        if isinstance(observed, WindowsFileIdentity):
            current = observed
        else:
            current = getattr(lease, "identity", None)
            if not isinstance(current, WindowsFileIdentity):
                current = getattr(lease, "main_identity", None)
        if not isinstance(current, WindowsFileIdentity):
            raise RecoveryHold(
                "HOLD_WINDOWS_PURGE_MAINTENANCE_UNAVAILABLE",
                "native Windows purge lease lost database identity",
            )
        return current

    @staticmethod
    def _flush_file(lease: _WindowsInPlacePurgeLease) -> None:
        try:
            lease.flush_file_buffers()
        except Exception as exc:
            lease.retain_fail_closed()
            raise RecoveryHold(_SEALING_INCOMPLETE, "Windows in-place purge database durability could not be proven") from exc

    def _require_stable_identity(self, lease: _WindowsInPlacePurgeLease, expected: Mapping[str, Any]) -> None:
        try:
            observed = self._lease_identity(lease)
        except RecoveryHold:
            lease.retain_fail_closed()
            raise
        except Exception as exc:
            lease.retain_fail_closed()
            raise RecoveryHold(_SEALING_INCOMPLETE, "Windows purge database identity could not be revalidated") from exc
        if not _same_identity(observed, expected):
            lease.retain_fail_closed()
            raise RecoveryHold(_SEALING_INCOMPLETE, "Windows in-place purge database file identity changed")

    def _preflight_in_place(self, conn: sqlite3.Connection, plan: RecoveryPlan) -> None:
        if conn.isolation_level is not None:
            raise RecoveryHold("HOLD_WINDOWS_PURGE_MODE_NO_MUTATION", "Windows panel purge requires autocommit maintenance connection")
        self._semantic._require_known_recovery_schema(conn)
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        synchronous = int(conn.execute("PRAGMA synchronous").fetchone()[0])
        locking_mode = str(conn.execute("PRAGMA locking_mode").fetchone()[0]).lower()
        if journal_mode != "delete":
            raise RecoveryHold("HOLD_WINDOWS_PURGE_MODE_NO_MUTATION", "Windows panel purge requires DELETE journal mode")
        if synchronous != 2:
            raise RecoveryHold("HOLD_WINDOWS_PURGE_MODE_NO_MUTATION", "Windows panel purge requires synchronous FULL")
        if locking_mode != "exclusive":
            raise RecoveryHold("HOLD_WINDOWS_PURGE_MODE_NO_MUTATION", "Windows panel purge requires EXCLUSIVE SQLite locking")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RecoveryHold("HOLD_DB_INTEGRITY_NO_MUTATION", "quick_check failed")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RecoveryHold("HOLD_DB_INTEGRITY_NO_MUTATION", "integrity_check failed")
        conn.execute("PRAGMA secure_delete=ON")
        if int(conn.execute("PRAGMA secure_delete").fetchone()[0]) != 1:
            raise RecoveryHold("HOLD_SECURE_DELETE_UNAVAILABLE_NO_MUTATION", "core secure_delete is unavailable")
        if not self._semantic._table_exists(conn, "memory_content_fts"):
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
            if conn.in_transaction:
                conn.rollback()
            raise
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.rollback()
            raise RecoveryHold(
                "HOLD_FTS_SECURE_DELETE_UNAVAILABLE_NO_MUTATION",
                "FTS5 secure-delete is unavailable",
            ) from exc
        _recovery_store_key(conn, plan)
        if tuple(PUBLIC_TOOL_NAMES) != _PUBLIC_TOOLS:
            raise RecoveryHold("HOLD_PUBLIC_CONTRACT_DRIFT_NO_MUTATION", "public MCP tool boundary drifted")

    @staticmethod
    def _clear_interlock(conn: sqlite3.Connection, plan: RecoveryPlan) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            operation = SensitiveMemoryRecoveryOperator._recovery_interlock_operation(conn)
            if operation not in {None, plan.operation_id}:
                raise RecoveryHold(_SEALING_INCOMPLETE, "Windows purge recovery interlock belongs to another operation")
            if operation == plan.operation_id:
                conn.execute(
                    "DELETE FROM adapter_meta WHERE key=? AND value=?",
                    (_RECOVERY_INTERLOCK_KEY, plan.operation_id),
                )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

    @staticmethod
    def _restore_interlock(conn: sqlite3.Connection, plan: RecoveryPlan) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            operation = SensitiveMemoryRecoveryOperator._recovery_interlock_operation(conn)
            if operation not in {None, plan.operation_id}:
                raise RecoveryHold(_SEALING_INCOMPLETE, "Windows purge recovery interlock changed during activation recovery")
            SensitiveMemoryRecoveryOperator._ensure_recovery_interlock(conn, plan)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

    def _verify_post_vacuum(
        self,
        conn: sqlite3.Connection,
        plan: RecoveryPlan,
        survivor: SurvivorSnapshot,
        lifecycle: LifecyclePreservationSnapshot,
    ) -> list[Any]:
        self._semantic._verify_db_health_and_fts(conn)
        self._semantic._verify_survivors(conn, plan, survivor)
        inspections = self._semantic._inspect_targets(conn, plan)
        if not inspections or not all(item.already_purged for item in inspections):
            raise RecoveryHold(_SEALING_INCOMPLETE, "Windows in-place purge tombstone verification failed")
        self._semantic._verify_logical_absence(conn, inspections)
        self._semantic._verify_recovery_audit_state(conn, plan)
        self._semantic._verify_unrelated_lifecycle(conn, plan, lifecycle)
        return inspections

    @staticmethod
    def _receipt(plan: RecoveryPlan, inspections: list[Any]) -> dict[str, Any]:
        return {
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
            "journal_mode_during_mutation": "DELETE",
            "synchronous_during_mutation": "FULL",
            "locking_mode_during_mutation": "EXCLUSIVE",
            "in_place_vacuum": "PASS",
            "database_file_identity_preserved": True,
            "post_vacuum_db_fts_verification": "PASS",
            "public_mcp_tool_count": 5,
            "plaintext_pre_mutation_backup": False,
            "raw_content_logged": False,
            "content_derived_confirmation_oracle": False,
            "external_residual_copy_status": "UNKNOWN",
            "physical_erasure_claim": False,
            "windows_panel_scope": "OWNER_CONTROL_PANEL_ONLY",
        }

    async def _activate(
        self,
        *,
        conn: sqlite3.Connection,
        lease: _WindowsInPlacePurgeLease,
        plan: RecoveryPlan,
        receipt: Path,
        state_path: Path,
        state: dict[str, Any],
        activation_hooks: RecoveryActivationHooks,
    ) -> RecoveryResult:
        lifecycle = LifecyclePreservationSnapshot.from_dict(state["unrelated_lifecycle_snapshot"])
        survivor = _survivor_from_mapping(state["survivor_snapshot"])
        inspections = self._verify_post_vacuum(conn, plan, survivor, lifecycle)
        self._require_stable_identity(lease, state["database_identity"])
        receipt_payload = self._receipt(plan, inspections)
        result = RecoveryResult(
            operation_id=plan.operation_id,
            state=_LOCAL_PURGE_VERIFIED,
            logical_purge_committed=True,
            promoted=True,
            receipt_path=str(receipt),
            state_path=str(state_path),
            target_count=len(plan.targets),
        )
        await _maybe_await(activation_hooks.activation_ready(result, lease))
        state = dict(state)
        state["phase"] = _ACTIVATION_READY
        state["receipt_payload"] = receipt_payload
        state = self._write_state(state_path, plan, state)
        try:
            _atomic_json(receipt, receipt_payload)
        except OwnerDestructiveError as exc:
            raise RecoveryHold(_SEALING_INCOMPLETE, "Windows purge receipt could not be persisted safely") from exc

        runtime_conn = self._prepare_runtime_activation(lease)
        self._clear_interlock(runtime_conn, plan)
        self._flush_file(lease)
        try:
            await _maybe_await(activation_hooks.publish_running(result, lease))
        except BaseException as exc:
            try:
                self._restore_interlock(runtime_conn, plan)
                self._flush_file(lease)
            except BaseException:
                lease.retain_fail_closed()
                raise RecoveryHold(
                    _SEALING_INCOMPLETE,
                    "Windows purge activation failed and the recovery interlock could not be restored",
                ) from exc
            raise RecoveryHold(_SEALING_INCOMPLETE, "Windows purge activation publication did not complete") from exc

        state = dict(state)
        state["phase"] = _LOCAL_PURGE_VERIFIED
        self._write_state(state_path, plan, state)
        return result

    async def execute(
        self,
        plan: RecoveryPlan,
        *,
        receipt_path: str,
        state_path: str | None = None,
        activation_hooks: RecoveryActivationHooks | None = None,
    ) -> RecoveryResult:
        if not isinstance(plan, RecoveryPlan):
            raise TypeError("plan must be a RecoveryPlan")
        if activation_hooks is None:
            raise RecoveryHold(
                "HOLD_WINDOWS_PURGE_PANEL_HOOKS_REQUIRED",
                "native Windows permanent purge is available only through the governed Owner panel lifecycle",
            )
        if activation_hooks.operation_id != plan.operation_id:
            raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "Windows purge activation hooks do not match the exact operation")

        receipt = Path(receipt_path).absolute()
        state_path_obj = Path(state_path).absolute() if state_path else receipt.with_suffix(receipt.suffix + ".state.json")
        if os.path.realpath(receipt) == os.path.realpath(plan.db_path) or os.path.realpath(state_path_obj) == os.path.realpath(plan.db_path):
            raise ValueError("receipt/state must be outside the primary memory DB")
        _ensure_private_directory(receipt.parent)

        persisted = self._read_state(state_path_obj, plan)
        lease = self._acquire_lease(plan.db_path)
        conn = lease.connection
        logical_committed = persisted is not None and _PHASE_RANK[str(persisted["phase"])] >= _PHASE_RANK[_SEALING_INCOMPLETE]
        try:
            if persisted is None:
                persisted = self._base_state(plan, self._lease_identity(lease))
                persisted = self._write_state(state_path_obj, plan, persisted)
            self._require_stable_identity(lease, persisted["database_identity"])
            self._preflight_in_place(conn, plan)

            persisted_phase = str(persisted["phase"])
            self._semantic._validate_recovery_interlock(conn, plan, persisted_phase)
            inspections = self._semantic._inspect_targets(conn, plan)
            lifecycle: LifecyclePreservationSnapshot | None = None
            if persisted_phase != _PREPARED:
                lifecycle = LifecyclePreservationSnapshot.from_dict(persisted["unrelated_lifecycle_snapshot"])

            if persisted_phase == _LOCAL_PURGE_VERIFIED:
                if not _state_exists(receipt):
                    raise RecoveryHold("HOLD_RECOVERY_STATE_MISMATCH", "verified Windows purge receipt is missing")
                survivor = _survivor_from_mapping(persisted["survivor_snapshot"])
                self._verify_post_vacuum(conn, plan, survivor, lifecycle)  # type: ignore[arg-type]
                self._require_stable_identity(lease, persisted["database_identity"])
                result = RecoveryResult(
                    plan.operation_id,
                    _LOCAL_PURGE_VERIFIED,
                    True,
                    True,
                    str(receipt),
                    str(state_path_obj),
                    len(plan.targets),
                )
                self._prepare_runtime_activation(lease)
                lease.close()
                return result

            if all(item.already_purged for item in inspections):
                if persisted_phase not in {
                    _LOGICAL_PURGE_IN_PROGRESS,
                    _SEALING_INCOMPLETE,
                    _ACTIVATION_READY,
                }:
                    raise RecoveryHold(
                        "HOLD_RECOVERY_STATE_MISMATCH",
                        "Windows purge tombstones are not backed by resumable recovery state",
                    )
                assert lifecycle is not None
                self._semantic._ensure_recovery_interlock(conn, plan)
                self._semantic._verify_unrelated_lifecycle(conn, plan, lifecycle)
                logical_committed = True
                if persisted_phase == _LOGICAL_PURGE_IN_PROGRESS or persisted["survivor_snapshot"] is None:
                    survivor = self._semantic._snapshot_survivors(conn, plan)
                    persisted = dict(persisted)
                    persisted["phase"] = _SEALING_INCOMPLETE
                    persisted["survivor_snapshot"] = _survivor_mapping(survivor)
                    persisted = self._write_state(state_path_obj, plan, persisted)
                    persisted_phase = _SEALING_INCOMPLETE
            else:
                if any(item.already_purged for item in inspections):
                    raise RecoveryHold("HOLD_TARGET_SET_PARTIALLY_APPLIED", "Windows purge target set is partially applied")
                if persisted_phase in {_SEALING_INCOMPLETE, _ACTIVATION_READY}:
                    raise RecoveryHold(
                        "HOLD_RECOVERY_STATE_MISMATCH",
                        "persisted Windows purge state claims commit but exact tombstones are absent",
                    )
                if lifecycle is None:
                    lifecycle = self._semantic._snapshot_unrelated_lifecycle(conn, plan)
                    persisted = dict(persisted)
                    persisted["phase"] = _LOGICAL_PURGE_IN_PROGRESS
                    persisted["unrelated_lifecycle_snapshot"] = lifecycle.to_dict()
                    persisted = self._write_state(state_path_obj, plan, persisted)
                    persisted_phase = _LOGICAL_PURGE_IN_PROGRESS
                else:
                    self._semantic._verify_unrelated_lifecycle(conn, plan, lifecycle)
                self._semantic._apply_logical_purge(
                    conn,
                    plan,
                    inspections,
                    lifecycle_preservation=lifecycle,
                )
                logical_committed = True
                survivor = self._semantic._snapshot_survivors(conn, plan)
                persisted = dict(persisted)
                persisted["phase"] = _SEALING_INCOMPLETE
                persisted["survivor_snapshot"] = _survivor_mapping(survivor)
                persisted = self._write_state(state_path_obj, plan, persisted)
                self._flush_file(lease)
                persisted_phase = _SEALING_INCOMPLETE

            assert lifecycle is not None
            survivor = _survivor_from_mapping(persisted["survivor_snapshot"])
            if persisted_phase == _SEALING_INCOMPLETE:
                self._require_stable_identity(lease, persisted["database_identity"])
                try:
                    conn.execute("VACUUM")
                except sqlite3.Error as exc:
                    raise RecoveryHold(_SEALING_INCOMPLETE, "Windows in-place purge VACUUM did not complete") from exc
                self._flush_file(lease)
                self._require_stable_identity(lease, persisted["database_identity"])
                self._preflight_in_place(conn, plan)
                self._verify_post_vacuum(conn, plan, survivor, lifecycle)

            result = await self._activate(
                conn=conn,
                lease=lease,
                plan=plan,
                receipt=receipt,
                state_path=state_path_obj,
                state=persisted,
                activation_hooks=activation_hooks,
            )
            lease.close()
            return result
        except RecoveryHold as exc:
            if logical_committed:
                try:
                    current = self._read_state(state_path_obj, plan)
                    if current is not None and _PHASE_RANK[str(current["phase"])] < _PHASE_RANK[_SEALING_INCOMPLETE]:
                        current = dict(current)
                        current["phase"] = _SEALING_INCOMPLETE
                        if current["survivor_snapshot"] is None:
                            try:
                                current["survivor_snapshot"] = _survivor_mapping(self._semantic._snapshot_survivors(conn, plan))
                            except BaseException:
                                current["survivor_snapshot"] = None
                        self._write_state(state_path_obj, plan, current)
                except BaseException:
                    lease.retain_fail_closed()
                current = self._read_state(state_path_obj, plan)
                if current is not None:
                    self._release_recoverable_postcommit(
                        lease,
                        conn,
                        plan,
                        current["database_identity"],
                    )
                if exc.code == _SEALING_INCOMPLETE:
                    raise
                raise RecoveryHold(
                    _SEALING_INCOMPLETE,
                    "Windows logical purge committed but sealing or verification did not complete",
                ) from exc
            self._release_after_failure(lease)
            raise
        except BaseException as exc:
            if logical_committed:
                try:
                    current = self._read_state(state_path_obj, plan)
                    if current is not None and _PHASE_RANK[str(current["phase"])] < _PHASE_RANK[_SEALING_INCOMPLETE]:
                        current = dict(current)
                        current["phase"] = _SEALING_INCOMPLETE
                        if current["survivor_snapshot"] is None:
                            current["survivor_snapshot"] = _survivor_mapping(self._semantic._snapshot_survivors(conn, plan))
                        self._write_state(state_path_obj, plan, current)
                except BaseException:
                    lease.retain_fail_closed()
                current = self._read_state(state_path_obj, plan)
                if current is not None:
                    self._release_recoverable_postcommit(
                        lease,
                        conn,
                        plan,
                        current["database_identity"],
                    )
                raise RecoveryHold(_SEALING_INCOMPLETE, "Windows in-place purge sealing or verification did not complete") from exc
            self._release_after_failure(lease)
            raise


async def resolve_windows_permanent_purge_targets_exclusive(
    *,
    operation_id: str,
    db_path: str,
    profile: EmbeddingProfile,
    identities: Sequence[Mapping[str, Any]],
    maintenance_lease_factory: Callable[[str], Any] | None = None,
) -> tuple[RecoveryTarget, ...]:
    """Resolve exact panel purge targets while the Windows DB-scoped lease is exclusive."""

    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id is required")
    if isinstance(identities, (str, bytes, bytearray)) or not isinstance(identities, Sequence):
        raise ValueError("permanent purge identities must be a bounded sequence")
    if not 1 <= len(identities) <= MAX_PERMANENT_PURGE_TARGETS:
        raise ValueError(f"permanent purge requires 1 to {MAX_PERMANENT_PURGE_TARGETS} exact targets")

    operator = WindowsOwnerPurgeOperator(maintenance_lease_factory=maintenance_lease_factory)
    lease = operator._acquire_lease(db_path)
    conn = lease.connection
    try:
        initial_identity = _identity_mapping(operator._lease_identity(lease))
        operator._preflight_in_place(conn, RecoveryPlan(
            operation_id=operation_id,
            db_path=db_path,
            profile=profile,
            targets=(RecoveryTarget(
                record_id="resolver-placeholder-record",
                memory_id="resolver-placeholder-memory",
                revision=0,
                scope=RecoveryScope("PROJECT", project_id="resolver-placeholder-project"),
                expected_lifecycle="HISTORICAL",
                expected_materialization=PM0,
                expected_native_hash=None,
            ),),
            retained_identity_fields_classified_non_sensitive=True,
        ))
        targets: list[RecoveryTarget] = []
        seen: set[tuple[Any, ...]] = set()
        store_key: str | None = None
        for raw in identities:
            if not isinstance(raw, Mapping) or set(raw) != {
                "scope",
                "project_id",
                "worktree_id",
                "workflow_id",
                "agent_id",
                "memory_id",
                "record_id",
                "revision",
            }:
                raise RecoveryHold("HOLD_TARGET_DRIFT_NO_MUTATION", "Windows purge target identity is malformed")
            try:
                scope_context = validate_scope(
                    raw.get("scope"),
                    raw.get("project_id"),
                    raw.get("worktree_id"),
                    raw.get("workflow_id"),
                    raw.get("agent_id"),
                )
                memory_id = validate_identifier(raw.get("memory_id"), "memory_id", required=True)
                record_id = validate_identifier(raw.get("record_id"), "record_id", required=True)
            except AdapterError as exc:
                raise RecoveryHold("HOLD_TARGET_DRIFT_NO_MUTATION", "Windows purge target identity is invalid") from exc
            revision = raw.get("revision")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise RecoveryHold("HOLD_TARGET_DRIFT_NO_MUTATION", "Windows purge target revision is invalid")
            assert memory_id is not None and record_id is not None
            key = (
                scope_context.scope,
                scope_context.project_id,
                scope_context.worktree_id,
                scope_context.workflow_id,
                scope_context.agent_id,
                memory_id,
                record_id,
                revision,
            )
            if key in seen:
                raise RecoveryHold("HOLD_TARGET_DRIFT_NO_MUTATION", "Windows purge target set contains duplicates")
            seen.add(key)

            row = conn.execute("SELECT * FROM adapter_records WHERE record_id=?", (record_id,)).fetchone()
            if row is None:
                raise RecoveryHold("HOLD_TARGET_DRIFT_NO_MUTATION", "exact Windows purge target record is missing")
            actual_scope = (row["scope"], row["project_id"], row["worktree_id"], row["workflow_id"], row["agent_id"])
            expected_scope = (
                scope_context.scope,
                scope_context.project_id,
                scope_context.worktree_id,
                scope_context.workflow_id,
                scope_context.agent_id,
            )
            if row["memory_id"] != memory_id or int(row["revision"]) != revision or actual_scope != expected_scope:
                raise RecoveryHold("HOLD_TARGET_DRIFT_NO_MUTATION", "exact Windows purge target binding changed")
            lifecycle = str(row["lifecycle_state"])
            native_hash = str(row["native_content_hash"])
            scope = RecoveryScope(
                scope_context.scope,
                project_id=scope_context.project_id,
                worktree_id=scope_context.worktree_id,
                workflow_id=scope_context.workflow_id,
                agent_id=scope_context.agent_id,
            )
            if store_key is None:
                provisional = RecoveryTarget(
                    record_id=record_id,
                    memory_id=memory_id,
                    revision=revision,
                    scope=scope,
                    expected_lifecycle=lifecycle,
                    expected_materialization=PM0,
                    expected_native_hash=native_hash,
                )
                store_key = _recovery_store_key(
                    conn,
                    RecoveryPlan(
                        operation_id=operation_id,
                        db_path=db_path,
                        profile=profile,
                        targets=(provisional,),
                        retained_identity_fields_classified_non_sensitive=True,
                    ),
                )

            native_rows = conn.execute(
                "SELECT id,deleted_at FROM memories WHERE content_hash=?",
                (native_hash,),
            ).fetchall()
            if len(native_rows) > 1:
                raise RecoveryHold("HOLD_UNEXPECTED_DERIVED_STATE_NO_MUTATION", "Windows purge native materialization is ambiguous")
            if native_rows:
                native_rowid = int(native_rows[0]["id"])
                if native_rows[0]["deleted_at"] is not None:
                    raise RecoveryHold("HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION", "Windows purge target native row is already deleted")
                stores = [
                    str(value[0])
                    for value in conn.execute(
                        "SELECT store FROM memory_embeddings WHERE rowid=? ORDER BY store",
                        (native_rowid,),
                    ).fetchall()
                ]
                if stores != [store_key] or operator._semantic._belief_refs_hash(conn, native_hash):
                    raise RecoveryHold("HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION", "Windows purge target has unsupported derived materialization")
                materialization = PM1
            else:
                orphan_vectors = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM memory_embeddings e WHERE NOT EXISTS "
                        "(SELECT 1 FROM memories m WHERE m.id=e.rowid)"
                    ).fetchone()[0]
                )
                if orphan_vectors or lifecycle != "HISTORICAL" or operator._semantic._belief_refs_hash(conn, native_hash):
                    raise RecoveryHold("HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION", "Windows purge target has unsupported partial materialization")
                materialization = PM0
            targets.append(
                RecoveryTarget(
                    record_id=record_id,
                    memory_id=memory_id,
                    revision=revision,
                    scope=scope,
                    expected_lifecycle=lifecycle,
                    expected_materialization=materialization,
                    expected_native_hash=native_hash,
                )
            )
        operator._require_stable_identity(lease, initial_identity)
        result = tuple(targets)
    except BaseException:
        operator._release_after_failure(lease)
        raise
    operator._prepare_runtime_activation(lease)
    lease.close()
    return result


__all__ = ["WindowsOwnerPurgeOperator", "resolve_windows_permanent_purge_targets_exclusive"]
