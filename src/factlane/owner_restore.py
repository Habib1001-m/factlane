from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time
from typing import Any, Awaitable, Callable
import uuid

from .contract import AdapterError, canonical_json, iso_now
from .maintenance_coordinator import MaintenanceCoordinatorError, PreparedMaintenance
from .owner_database import ArtifactRegistry, create_consistent_backup
from .recovery import MaintenanceLease, RecoveryHold
from .runtime_control import RuntimeIdentity
from .storage import STORAGE_CONTRACT_VERSION


_RECOVERY_INTERLOCK_KEY = "sensitive_recovery_interlock"
_POST_ACTIVATION_VERIFY_TIMEOUT_SECONDS = 5.0
_MAX_MAINTENANCE_PARTICIPANT_FAILURES = 8
_MAX_MAINTENANCE_PARTICIPANT_FAILURE_BYTES = 2048
_MAX_SERVICE_STATUS_BYTES = 64
_MAX_SERVICE_STOPPED_BOOT_ID_BYTES = 128


class RestoreRecoveryRequired(RuntimeError):
    """The replacement is active but final verification/reconciliation is incomplete."""

    def __init__(self, operation_id: str, message: str) -> None:
        super().__init__(message)
        self.operation_id = operation_id
        self.safe_message = message


AuthorizeCallback = Callable[[str], Any | Awaitable[Any]]
PostflightCallback = Callable[[str, Any], Any | Awaitable[Any]]
InvalidateSessionsCallback = Callable[[], Any | Awaitable[Any]]
PostActivationVerificationCallback = Callable[..., Any | Awaitable[Any]]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_operation_id(value: str) -> str:
    if not isinstance(value, str):
        raise AdapterError("INVALID_ENVELOPE", "restore operation_id must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise AdapterError("INVALID_ENVELOPE", "restore operation_id must be a UUID") from exc
    canonical = str(parsed)
    if canonical != value:
        raise AdapterError("INVALID_ENVELOPE", "restore operation_id must use canonical UUID spelling")
    return canonical


def _private_dir(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "restore state directory is not owner-controlled")
        os.chmod(path, 0o700)
        return
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _private_dir(path.parent)
    stage = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write((canonical_json(payload) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(stage, path)
        os.chmod(path, 0o600)
    finally:
        if stage.exists():
            stage.unlink()


def _validate_sqlite_image(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise AdapterError("BACKUP_INVALID", "registered restore artifact is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise AdapterError("BACKUP_INVALID", "restore artifact must be a regular file")
    uri = "file:" + str(path.resolve()).replace("?", "%3F") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise AdapterError("BACKUP_INVALID", "restore artifact is not a readable SQLite database") from exc
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise AdapterError("BACKUP_INVALID", "restore artifact integrity verification failed")
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adapter_meta'"
        ).fetchone()
        if table is None:
            raise AdapterError("BACKUP_INVALID", "restore artifact is missing FactLane storage metadata")
        row = conn.execute("SELECT value FROM adapter_meta WHERE key='contract_version'").fetchone()
        try:
            contract_version = int(row[0]) if row is not None else None
        except (TypeError, ValueError) as exc:
            raise AdapterError("BACKUP_INVALID", "restore artifact storage contract metadata is malformed") from exc
        if contract_version != STORAGE_CONTRACT_VERSION:
            raise AdapterError("SCHEMA_MISMATCH", "restore artifact storage contract is incompatible")
        recovery_row = conn.execute(
            "SELECT value FROM adapter_meta WHERE key=?", (_RECOVERY_INTERLOCK_KEY,)
        ).fetchone()
        if recovery_row is not None:
            raise AdapterError(
                "RECOVERY_REQUIRED",
                "restore artifact contains an incomplete recovery interlock",
            )
        profile_row = conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone()
        profile = None
        if profile_row is not None:
            try:
                profile = json.loads(profile_row[0])
            except (TypeError, json.JSONDecodeError) as exc:
                raise AdapterError("BACKUP_INVALID", "restore artifact embedding profile metadata is malformed") from exc
        return {
            "storage_contract_version": contract_version,
            "embedding_profile": profile,
        }
    except sqlite3.Error as exc:
        raise AdapterError("BACKUP_INVALID", "restore artifact SQLite validation failed") from exc
    finally:
        conn.close()


def _read_restored_metadata(path: Path) -> dict[str, Any]:
    """Read only the bounded metadata needed after RUNNING publication."""

    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise AdapterError("BACKUP_INVALID", "active restored database is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise AdapterError("BACKUP_INVALID", "active restored database must be a regular file")
    uri = "file:" + str(path.resolve()).replace("?", "%3F") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error as exc:
        raise AdapterError("BACKUP_INVALID", "active restored database is not readable") from exc
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT key,value FROM adapter_meta WHERE key IN ('contract_version','embedding_profile') LIMIT 2"
        ).fetchall()
        metadata = {str(key): value for key, value in rows}
        try:
            contract_version = int(metadata["contract_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterError("BACKUP_INVALID", "active restored storage contract metadata is malformed") from exc
        profile = None
        if "embedding_profile" in metadata:
            try:
                profile = json.loads(metadata["embedding_profile"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise AdapterError("BACKUP_INVALID", "active restored embedding profile metadata is malformed") from exc
        return {
            "storage_contract_version": contract_version,
            "embedding_profile": profile,
        }
    except sqlite3.Error as exc:
        raise AdapterError("BACKUP_INVALID", "active restored metadata read failed") from exc
    finally:
        conn.close()


def _bounded_participant_failures(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > _MAX_MAINTENANCE_PARTICIPANT_FAILURES:
        raise AdapterError(
            "POST_ACTIVATION_VERIFICATION_FAILED",
            "maintenance participant resume evidence is malformed",
        )
    bounded: dict[str, str] = {}
    for participant, code in value.items():
        if (
            not isinstance(participant, str)
            or not participant
            or len(participant.encode("utf-8")) > 64
            or not isinstance(code, str)
            or not code
            or len(code.encode("utf-8")) > 128
        ):
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_FAILED",
                "maintenance participant resume evidence is malformed",
            )
        bounded[participant] = code
    if len(canonical_json(bounded).encode("utf-8")) > _MAX_MAINTENANCE_PARTICIPANT_FAILURE_BYTES:
        raise AdapterError(
            "POST_ACTIVATION_VERIFICATION_FAILED",
            "maintenance participant resume evidence is oversized",
        )
    return bounded


def _write_interlock(path: Path, operation_id: str) -> None:
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adapter_meta'"
        ).fetchone()
        if table is None:
            raise AdapterError("BACKUP_INVALID", "replacement image is missing FactLane storage metadata")
        existing = conn.execute(
            "SELECT value FROM adapter_meta WHERE key=?", (_RECOVERY_INTERLOCK_KEY,)
        ).fetchone()
        if existing is not None and existing[0] != operation_id:
            raise AdapterError("RECOVERY_REQUIRED", "replacement image already carries another recovery interlock")
        conn.execute(
            "INSERT OR REPLACE INTO adapter_meta(key,value) VALUES (?,?)",
            (_RECOVERY_INTERLOCK_KEY, operation_id),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _clear_interlock(path: Path, operation_id: str) -> None:
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        row = conn.execute(
            "SELECT value FROM adapter_meta WHERE key=?", (_RECOVERY_INTERLOCK_KEY,)
        ).fetchone()
        if row is None or row[0] != operation_id:
            raise RestoreRecoveryRequired(operation_id, "restore recovery interlock does not match the active operation")
        conn.execute("DELETE FROM adapter_meta WHERE key=?", (_RECOVERY_INTERLOCK_KEY,))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _read_interlock(path: Path) -> str | None:
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        row = conn.execute(
            "SELECT value FROM adapter_meta WHERE key=?", (_RECOVERY_INTERLOCK_KEY,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    value = row[0]
    if not isinstance(value, str):
        raise AdapterError("RECOVERY_REQUIRED", "restore recovery interlock is malformed")
    return value


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class FullRestoreService:
    """Owner-only full-backup restore orchestration over the governed maintenance boundary."""

    def __init__(
        self,
        *,
        active_db: str | os.PathLike[str],
        registry: ArtifactRegistry,
        state_dir: str | os.PathLike[str],
        service_state: Any,
        coordinator: Any | None = None,
        authorize: AuthorizeCallback | None = None,
        postflight: PostflightCallback | None = None,
        invalidate_sessions: InvalidateSessionsCallback | None = None,
        post_activation_verify: PostActivationVerificationCallback | None = None,
    ) -> None:
        self.active_db = Path(active_db).resolve(strict=False)
        self.registry = registry
        self.state_dir = Path(state_dir).absolute()
        _private_dir(self.state_dir)
        self._service_state = service_state
        self.coordinator = coordinator
        self._authorize = authorize
        self._postflight = postflight
        self._invalidate_sessions = invalidate_sessions
        self._post_activation_verify = post_activation_verify

    def _require_executable_configuration(
        self,
        authorize: AuthorizeCallback | None = None,
    ) -> AuthorizeCallback:
        effective_authorize = authorize if authorize is not None else self._authorize
        service_methods = (
            getattr(self._service_state, "begin_maintenance", None),
            getattr(self._service_state, "finish_maintenance", None),
            getattr(self._service_state, "mark_recovery_required", None),
            getattr(self._service_state, "snapshot", None),
        )
        if (
            self._service_state is None
            or not all(callable(method) for method in service_methods)
            or self.coordinator is None
            or not callable(effective_authorize)
            or not callable(self._postflight)
            or not callable(self._invalidate_sessions)
            or not callable(self._post_activation_verify)
        ):
            raise AdapterError(
                "RESTORE_NOT_CONFIGURED",
                "full restore requires service state, runtime coordination, authorization, session invalidation, and post-activation verification",
            )
        return effective_authorize

    def _require_recovery_authorize(self, authorize: AuthorizeCallback | None) -> AuthorizeCallback:
        self._require_executable_configuration(authorize)
        if authorize is None:
            raise AdapterError(
                "RESTORE_RECOVERY_AUTH_REQUIRED",
                "restore recovery requires a fresh request-bound authorization callback",
            )
        return authorize

    async def _begin_service_maintenance(self, operation_id: str) -> None:
        await _maybe_await(self._service_state.begin_maintenance(operation_id))

    async def _finish_service_maintenance(self, operation_id: str) -> None:
        try:
            await _maybe_await(self._service_state.finish_maintenance(operation_id))
        except Exception:
            snapshot = getattr(self._service_state, "snapshot", None)
            if callable(snapshot):
                value = await _maybe_await(snapshot())
                if getattr(value, "maintenance_operation_id", object()) is None:
                    return
            raise

    async def _mark_service_recovery_required(self) -> None:
        await _maybe_await(self._service_state.mark_recovery_required())

    @staticmethod
    def _validated_service_initial(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"status", "intent_revision", "stopped_boot_id"}:
            raise AdapterError(
                "RESTORE_STATE_UNAVAILABLE",
                "restore service-state maintenance entry evidence is malformed",
            )
        status = value.get("status")
        intent_revision = value.get("intent_revision")
        stopped_boot_id = value.get("stopped_boot_id")
        if (
            not isinstance(status, str)
            or not status
            or len(status.encode("utf-8")) > _MAX_SERVICE_STATUS_BYTES
            or any(ord(character) < 32 for character in status)
            or isinstance(intent_revision, bool)
            or not isinstance(intent_revision, int)
            or intent_revision < 0
            or (
                stopped_boot_id is not None
                and (
                    not isinstance(stopped_boot_id, str)
                    or not stopped_boot_id
                    or len(stopped_boot_id.encode("utf-8")) > _MAX_SERVICE_STOPPED_BOOT_ID_BYTES
                    or any(ord(character) < 32 for character in stopped_boot_id)
                )
            )
        ):
            raise AdapterError(
                "RESTORE_STATE_UNAVAILABLE",
                "restore service-state maintenance entry evidence is malformed",
            )
        return {
            "status": status,
            "intent_revision": intent_revision,
            "stopped_boot_id": stopped_boot_id,
        }

    async def _service_snapshot_receipt(self) -> dict[str, Any]:
        snapshotter = getattr(self._service_state, "snapshot", None)
        if not callable(snapshotter):
            raise AdapterError(
                "RESTORE_NOT_CONFIGURED",
                "full restore requires readable WebUI service-state evidence",
            )
        value = await _maybe_await(snapshotter())
        status = getattr(value, "status", None)
        raw_status = getattr(status, "value", status)
        return self._validated_service_initial(
            {
                "status": raw_status,
                "intent_revision": getattr(value, "intent_revision", None),
                "stopped_boot_id": getattr(value, "stopped_boot_id", None),
            }
        )

    async def _service_status(self) -> str | None:
        snapshot = getattr(self._service_state, "snapshot", None)
        if callable(snapshot):
            value = await _maybe_await(snapshot())
            status = getattr(value, "status", None)
        else:
            status = getattr(self._service_state, "status", None)
        raw = getattr(status, "value", status)
        return raw if isinstance(raw, str) else None

    async def _finish_service_after_recovery(self, operation_id: str) -> None:
        if await self._service_status() != "RECOVERY_REQUIRED":
            await self._finish_service_maintenance(operation_id)
            return
        journal = self._read_journal_optional(operation_id)
        if journal is None:
            raise AdapterError(
                "RESTORE_STATE_UNAVAILABLE",
                "restore recovery lost durable service-state maintenance entry evidence",
            )
        initial = self._validated_service_initial(journal.get("service_initial"))
        current = await self._service_snapshot_receipt()
        if current["intent_revision"] < initial["intent_revision"]:
            raise AdapterError(
                "RESTORE_STATE_UNAVAILABLE",
                "restore service-state intent revision moved backwards during recovery",
            )
        newer_stop = (
            current["stopped_boot_id"] is not None
            and current["intent_revision"] > initial["intent_revision"]
        )
        if not newer_stop and initial["status"] == "FAILED":
            mark_failed = getattr(self._service_state, "mark_failed", None)
            if not callable(mark_failed):
                raise AdapterError(
                    "RESTORE_NOT_CONFIGURED",
                    "full restore recovery requires FAILED service-state restoration support",
                )
            await _maybe_await(mark_failed())
            return
        clear_recovery = getattr(self._service_state, "clear_recovery_required", None)
        if not callable(clear_recovery):
            raise AdapterError(
                "RESTORE_NOT_CONFIGURED",
                "full restore recovery requires service-state recovery finalization",
            )
        await _maybe_await(clear_recovery())

    @staticmethod
    def _prepared_receipt(prepared: Any) -> dict[str, Any]:
        operation_id = _canonical_operation_id(str(getattr(prepared, "operation_id", "")))
        original_generation = getattr(prepared, "original_generation", None)
        intent_revision = getattr(prepared, "intent_revision", None)
        initially_open = getattr(prepared, "initially_open_instance_ids", None)
        registrations = getattr(prepared, "registrations", None)
        if (
            isinstance(original_generation, bool)
            or not isinstance(original_generation, int)
            or original_generation < 0
            or isinstance(intent_revision, bool)
            or not isinstance(intent_revision, int)
            or intent_revision < 0
            or not isinstance(initially_open, tuple)
            or not all(isinstance(value, str) for value in initially_open)
            or not isinstance(registrations, tuple)
        ):
            raise AdapterError("RESTORE_COORDINATION_FAILED", "prepared maintenance receipt is incomplete")
        serialized: list[dict[str, Any]] = []
        for identity in registrations:
            to_mapping = getattr(identity, "to_mapping", None)
            if not callable(to_mapping):
                raise AdapterError("RESTORE_COORDINATION_FAILED", "prepared runtime identity is not serializable")
            value = to_mapping()
            if not isinstance(value, dict):
                raise AdapterError("RESTORE_COORDINATION_FAILED", "prepared runtime identity is invalid")
            serialized.append(value)
        return {
            "operation_id": operation_id,
            "original_generation": original_generation,
            "intent_revision": intent_revision,
            "initially_open_instance_ids": list(initially_open),
            "registrations": serialized,
        }

    @staticmethod
    def _prepared_from_receipt(value: Any) -> PreparedMaintenance:
        if not isinstance(value, dict) or set(value) != {
            "operation_id",
            "original_generation",
            "intent_revision",
            "initially_open_instance_ids",
            "registrations",
        }:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "prepared maintenance receipt is missing or malformed")
        operation_id = _canonical_operation_id(value.get("operation_id"))
        original_generation = value.get("original_generation")
        intent_revision = value.get("intent_revision")
        initially_open = value.get("initially_open_instance_ids")
        registrations = value.get("registrations")
        if (
            isinstance(original_generation, bool)
            or not isinstance(original_generation, int)
            or original_generation < 0
            or isinstance(intent_revision, bool)
            or not isinstance(intent_revision, int)
            or intent_revision < 0
            or not isinstance(initially_open, list)
            or not all(isinstance(item, str) for item in initially_open)
            or not isinstance(registrations, list)
        ):
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "prepared maintenance receipt fields are invalid")
        try:
            identities = tuple(RuntimeIdentity.from_mapping(item) for item in registrations)
        except Exception as exc:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "prepared runtime identity receipt is invalid") from exc
        return PreparedMaintenance(
            operation_id=operation_id,
            original_generation=original_generation,
            intent_revision=intent_revision,
            initially_open_instance_ids=tuple(initially_open),
            registrations=identities,
        )

    @staticmethod
    def _initially_open_runtime_identities(prepared: PreparedMaintenance) -> dict[str, RuntimeIdentity]:
        initially_open = tuple(prepared.initially_open_instance_ids)
        if len(set(initially_open)) != len(initially_open):
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_FAILED",
                "prepared maintenance contains duplicate initially-open runtime identities",
            )
        registrations: dict[str, RuntimeIdentity] = {}
        for identity in prepared.registrations:
            if identity.instance_id in registrations:
                raise AdapterError(
                    "POST_ACTIVATION_VERIFICATION_FAILED",
                    "prepared maintenance contains duplicate runtime registrations",
                )
            registrations[identity.instance_id] = identity
        missing = [instance_id for instance_id in initially_open if instance_id not in registrations]
        if missing:
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_FAILED",
                "prepared maintenance lost an initially-open runtime registration",
            )
        return {instance_id: registrations[instance_id] for instance_id in initially_open}

    def _validate_resume_truth(
        self,
        prepared: PreparedMaintenance,
        ready_instance_ids: list[str],
        reconnect_failures: dict[str, str],
    ) -> dict[str, RuntimeIdentity]:
        identities = self._initially_open_runtime_identities(prepared)
        expected_ids = set(identities)
        if (
            not isinstance(ready_instance_ids, list)
            or any(not isinstance(instance_id, str) or not instance_id for instance_id in ready_instance_ids)
            or len(set(ready_instance_ids)) != len(ready_instance_ids)
            or not set(ready_instance_ids).issubset(expected_ids)
        ):
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_FAILED",
                "runtime resume reported a READY identity outside the initially-open set",
            )
        if not isinstance(reconnect_failures, dict) or any(
            not isinstance(instance_id, str)
            or not instance_id
            or not isinstance(code, str)
            or not code
            or len(code.encode("utf-8")) > 128
            for instance_id, code in reconnect_failures.items()
        ):
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_FAILED",
                "runtime resume failure evidence is malformed",
            )
        failure_ids = set(reconnect_failures)
        ready_ids = set(ready_instance_ids)
        if ready_ids.intersection(failure_ids):
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_FAILED",
                "runtime resume reported the same runtime as READY and failed",
            )
        if "coordinator" in failure_ids:
            if failure_ids != {"coordinator"} or ready_ids:
                raise AdapterError(
                    "POST_ACTIVATION_VERIFICATION_FAILED",
                    "coordinator resume failure cannot claim per-runtime READY evidence",
                )
        else:
            if not failure_ids.issubset(expected_ids) or ready_ids.union(failure_ids) != expected_ids:
                raise AdapterError(
                    "POST_ACTIVATION_VERIFICATION_FAILED",
                    "runtime resume result does not account for every initially-open runtime",
                )
        return identities

    def _fresh_restored_metadata(
        self,
        preview: dict[str, Any],
        *,
        device: int,
        inode: int,
    ) -> dict[str, Any]:
        try:
            active_info = self.active_db.stat()
        except OSError as exc:
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_FAILED",
                "active database identity could not be observed during post-activation verification",
            ) from exc
        if (active_info.st_dev, active_info.st_ino) != (device, inode):
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_FAILED",
                "active database identity changed before post-activation verification",
            )
        metadata = _read_restored_metadata(self.active_db)
        if (
            metadata.get("storage_contract_version") != preview.get("storage_contract_version")
            or metadata.get("embedding_profile") != preview.get("embedding_profile")
        ):
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_FAILED",
                "fresh active database metadata does not match the restored preview",
            )
        return metadata

    def _validate_product_proof(
        self,
        receipt: Any,
        *,
        preview: dict[str, Any],
        prepared: PreparedMaintenance,
        operation_id: str,
        generation: int,
        device: int,
        inode: int,
        ready_instance_ids: list[str],
        reconnect_failures: dict[str, str],
    ) -> dict[str, Any]:
        required = {
            "status",
            "operation_id",
            "generation",
            "database_path",
            "database_device",
            "database_inode",
            "storage_contract_version",
            "embedding_profile",
            "bounded_read_verified",
            "ready_instance_ids",
            "reconnect_failures",
            "resumed_runtime_evidence",
        }
        if not isinstance(receipt, dict) or set(receipt) != required:
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_INVALID",
                "post-activation verification returned an unsupported receipt shape",
            )
        identities = self._validate_resume_truth(prepared, ready_instance_ids, reconnect_failures)
        if generation != prepared.original_generation + 1:
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_FAILED",
                "post-activation verification generation did not advance exactly once",
            )
        runtime_evidence = receipt.get("resumed_runtime_evidence")
        if (
            receipt.get("status") != "VERIFIED"
            or receipt.get("operation_id") != operation_id
            or receipt.get("generation") != generation
            or receipt.get("database_path") != str(self.active_db)
            or receipt.get("database_device") != device
            or receipt.get("database_inode") != inode
            or receipt.get("storage_contract_version") != preview.get("storage_contract_version")
            or receipt.get("embedding_profile") != preview.get("embedding_profile")
            or receipt.get("bounded_read_verified") is not True
            or receipt.get("ready_instance_ids") != ready_instance_ids
            or receipt.get("reconnect_failures") != reconnect_failures
            or not isinstance(runtime_evidence, dict)
            or set(runtime_evidence) != set(ready_instance_ids)
        ):
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_FAILED",
                "post-activation verification did not prove the active database and resumed runtime state",
            )
        evidence_fields = {
            "protocol_version",
            "instance_id",
            "owner_uid",
            "pid",
            "process_start_ticks",
            "boot_id",
            "database_path",
            "device",
            "inode",
            "generation",
            "control_socket_path",
            "lifecycle_state",
            "lease_open",
        }
        for instance_id in ready_instance_ids:
            evidence = runtime_evidence.get(instance_id)
            original = identities[instance_id]
            if not isinstance(evidence, dict) or set(evidence) != evidence_fields:
                raise AdapterError(
                    "POST_ACTIVATION_VERIFICATION_INVALID",
                    "resumed runtime evidence has an unsupported shape",
                )
            integer_fields = (
                "protocol_version",
                "owner_uid",
                "pid",
                "process_start_ticks",
                "device",
                "inode",
                "generation",
            )
            if any(
                isinstance(evidence.get(field), bool) or not isinstance(evidence.get(field), int)
                for field in integer_fields
            ):
                raise AdapterError(
                    "POST_ACTIVATION_VERIFICATION_FAILED",
                    "resumed runtime evidence contains an invalid integer scalar",
                )
            if (
                evidence.get("protocol_version") != original.protocol_version
                or evidence.get("instance_id") != instance_id
                or evidence.get("owner_uid") != original.owner_uid
                or evidence.get("pid") != original.pid
                or evidence.get("process_start_ticks") != original.process_start_ticks
                or evidence.get("boot_id") != original.boot_id
                or evidence.get("database_path") != str(self.active_db)
                or evidence.get("database_path") != original.database_path
                or evidence.get("device") != device
                or evidence.get("inode") != inode
                or evidence.get("generation") != generation
                or evidence.get("control_socket_path") != original.control_socket_path
                or evidence.get("lifecycle_state") != "OPEN"
                or evidence.get("lease_open") is not True
            ):
                raise AdapterError(
                    "POST_ACTIVATION_VERIFICATION_FAILED",
                    "resumed runtime evidence does not match its initially-open process identity and activated database",
                )
        if len(canonical_json(receipt).encode("utf-8")) > 8192:
            raise AdapterError("POST_ACTIVATION_VERIFICATION_INVALID", "post-activation verification receipt is too large")
        return dict(receipt)

    async def _post_activation_product_proof(
        self,
        *,
        preview: dict[str, Any],
        prepared: PreparedMaintenance,
        operation_id: str,
        generation: int,
        device: int,
        inode: int,
        ready_instance_ids: list[str],
        reconnect_failures: dict[str, str],
    ) -> dict[str, Any]:
        identities = self._validate_resume_truth(prepared, ready_instance_ids, reconnect_failures)
        if generation != prepared.original_generation + 1:
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_FAILED",
                "post-activation verification generation did not advance exactly once",
            )
        metadata = self._fresh_restored_metadata(preview, device=device, inode=inode)
        initially_open_runtime_identities = [
            identities[instance_id].to_mapping()
            for instance_id in prepared.initially_open_instance_ids
        ]
        proof_kwargs = {
            "operation_id": operation_id,
            "generation": generation,
            "database_path": str(self.active_db),
            "database_device": device,
            "database_inode": inode,
            "expected_storage_contract_version": metadata["storage_contract_version"],
            "expected_embedding_profile": metadata["embedding_profile"],
            "initially_open_runtime_identities": initially_open_runtime_identities,
            "ready_instance_ids": list(ready_instance_ids),
            "reconnect_failures": dict(reconnect_failures),
        }

        async def invoke() -> Any:
            callback = self._post_activation_verify
            if inspect.iscoroutinefunction(callback):
                return await callback(**proof_kwargs)
            value = await asyncio.to_thread(callback, **proof_kwargs)
            if inspect.isawaitable(value):
                return await value
            return value

        try:
            receipt = await asyncio.wait_for(invoke(), timeout=_POST_ACTIVATION_VERIFY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise AdapterError(
                "POST_ACTIVATION_VERIFICATION_TIMEOUT",
                "post-activation product verification exceeded its bounded timeout",
            ) from exc
        return self._validate_product_proof(
            receipt,
            preview=preview,
            prepared=prepared,
            operation_id=operation_id,
            generation=generation,
            device=device,
            inode=inode,
            ready_instance_ids=ready_instance_ids,
            reconnect_failures=reconnect_failures,
        )

    def _artifact_path(self, receipt: dict[str, Any]) -> Path:
        relative = receipt.get("relative_path")
        if not isinstance(relative, str):
            raise AdapterError("ARTIFACT_REGISTRY_INVALID", "restore artifact receipt path is invalid")
        return self.registry.managed_path(relative)

    @staticmethod
    def _capacity_device(path: Path) -> int:
        try:
            info = path.stat()
        except OSError as exc:
            raise AdapterError(
                "RESTORE_CAPACITY_UNAVAILABLE",
                "restore destination filesystem identity could not be observed",
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise AdapterError(
                "RESTORE_CAPACITY_UNAVAILABLE",
                "restore destination capacity path is not a directory",
            )
        return info.st_dev

    def _active_allocation_requirement(self) -> int:
        try:
            active_info = self.active_db.stat()
        except OSError as exc:
            raise AdapterError(
                "RESTORE_CAPACITY_UNAVAILABLE",
                "active database allocation could not be observed",
            ) from exc
        if not stat.S_ISREG(active_info.st_mode) or active_info.st_size < 0:
            raise AdapterError(
                "RESTORE_CAPACITY_UNAVAILABLE",
                "active database allocation is invalid",
            )

        wal_bytes = 0
        wal = Path(str(self.active_db) + "-wal")
        try:
            wal_info = wal.lstat()
        except FileNotFoundError:
            wal_info = None
        except OSError as exc:
            raise AdapterError(
                "RESTORE_CAPACITY_UNAVAILABLE",
                "active database WAL allocation could not be observed",
            ) from exc
        if wal_info is not None:
            if stat.S_ISLNK(wal_info.st_mode) or not stat.S_ISREG(wal_info.st_mode) or wal_info.st_size < 0:
                raise AdapterError(
                    "RESTORE_CAPACITY_UNAVAILABLE",
                    "active database WAL allocation is not a regular file",
                )
            wal_bytes = wal_info.st_size

        # SQLite's -shm file is a transient WAL-index coordination file. It is
        # neither copied by the consistent SQLite backup nor by the restore stage,
        # so DB + existing WAL bytes are the conservative source-content bound.
        return active_info.st_size + wal_bytes

    def _preflight_restore_capacity(
        self,
        preview: dict[str, Any],
        *,
        snapshot_required: bool,
    ) -> dict[int, dict[str, int | str]]:
        artifact_id = preview.get("artifact_id")
        if not isinstance(artifact_id, str):
            raise AdapterError("INVALID_ENVELOPE", "restore preview artifact identity is invalid")
        receipt = self.registry.get(artifact_id)
        artifact_size = receipt.get("size_bytes")
        if (
            isinstance(artifact_size, bool)
            or not isinstance(artifact_size, int)
            or artifact_size < 0
            or artifact_size != preview.get("artifact_size_bytes")
        ):
            raise AdapterError(
                "RESTORE_CAPACITY_UNAVAILABLE",
                "restore artifact allocation receipt is invalid",
            )

        snapshot_bytes = self._active_allocation_requirement() if snapshot_required else 0
        destinations = (
            (self.registry.root, snapshot_bytes),
            (self.active_db.parent, artifact_size),
        )
        grouped: dict[int, dict[str, Any]] = {}
        for destination, required_bytes in destinations:
            if required_bytes == 0:
                continue
            try:
                device = self._capacity_device(destination)
            except AdapterError:
                raise
            except OSError as exc:
                raise AdapterError(
                    "RESTORE_CAPACITY_UNAVAILABLE",
                    "restore destination filesystem identity could not be observed",
                ) from exc
            if isinstance(device, bool) or not isinstance(device, int) or device < 0:
                raise AdapterError(
                    "RESTORE_CAPACITY_UNAVAILABLE",
                    "restore destination filesystem identity is invalid",
                )
            group = grouped.setdefault(
                device,
                {"path": destination, "required_bytes": 0},
            )
            group["required_bytes"] = int(group["required_bytes"]) + required_bytes

        observation: dict[int, dict[str, int | str]] = {}
        for device, group in grouped.items():
            destination = group["path"]
            required_bytes = int(group["required_bytes"])
            try:
                usage = shutil.disk_usage(destination)
            except OSError as exc:
                raise AdapterError(
                    "RESTORE_CAPACITY_UNAVAILABLE",
                    "restore destination free space could not be observed",
                ) from exc
            free_bytes = getattr(usage, "free", None)
            if isinstance(free_bytes, bool) or not isinstance(free_bytes, int) or free_bytes < 0:
                raise AdapterError(
                    "RESTORE_CAPACITY_UNAVAILABLE",
                    "restore destination free-space observation is invalid",
                )
            if free_bytes < required_bytes:
                raise AdapterError(
                    "RESTORE_INSUFFICIENT_SPACE",
                    "restore requires more free space for retained snapshot and staging allocations",
                )
            observation[device] = {
                "path": str(destination),
                "required_bytes": required_bytes,
                "free_bytes": free_bytes,
            }
        return observation

    def preview(self, *, artifact_id: str, trusted_origin_attested: bool) -> dict[str, Any]:
        if trusted_origin_attested is not True:
            raise AdapterError(
                "TRUSTED_ORIGIN_REQUIRED",
                "full restore requires explicit trusted origin attestation; checksum integrity is not origin trust",
            )
        receipt = self.registry.get(artifact_id)
        if receipt.get("kind") != "SQLITE_BACKUP":
            raise AdapterError("BACKUP_INVALID", "full restore requires a registered SQLite backup artifact")
        artifact = self._artifact_path(receipt)
        metadata = _validate_sqlite_image(artifact)
        try:
            active_info = self.active_db.stat()
        except FileNotFoundError as exc:
            raise AdapterError("BACKEND_UNAVAILABLE", "active database was not found") from exc
        if not stat.S_ISREG(active_info.st_mode):
            raise AdapterError("BACKEND_UNAVAILABLE", "active database is not a regular file")
        authorization_payload = {
            "operation": "FULL_RESTORE",
            "artifact_id": receipt["artifact_id"],
            "artifact_sha256": receipt["sha256"],
            "artifact_size_bytes": receipt["size_bytes"],
            "active_database_device": active_info.st_dev,
            "active_database_inode": active_info.st_ino,
            "trusted_origin_attested": True,
        }
        return {
            "status": "READY",
            "operation": "FULL_RESTORE",
            "artifact_id": receipt["artifact_id"],
            "artifact_sha256": receipt["sha256"],
            "artifact_size_bytes": receipt["size_bytes"],
            "storage_contract_version": metadata["storage_contract_version"],
            "embedding_profile": metadata["embedding_profile"],
            "trusted_origin_attested": True,
            "active_database_device": active_info.st_dev,
            "active_database_inode": active_info.st_ino,
            "authorization_preview": canonical_json(authorization_payload),
            "warnings": [
                "This replaces the active database after creating a retained pre-restore snapshot.",
                "A checksum proves integrity, not trusted origin; the Owner attestation above is part of this exact operation.",
            ],
        }

    def _assert_preview_current(self, preview: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(preview, dict) or preview.get("operation") != "FULL_RESTORE":
            raise AdapterError("INVALID_ENVELOPE", "restore preview is invalid")
        current = self.preview(
            artifact_id=preview.get("artifact_id"),
            trusted_origin_attested=preview.get("trusted_origin_attested") is True,
        )
        bound_fields = (
            "artifact_id",
            "artifact_sha256",
            "artifact_size_bytes",
            "storage_contract_version",
            "embedding_profile",
            "trusted_origin_attested",
            "active_database_device",
            "active_database_inode",
            "authorization_preview",
        )
        if any(current.get(field) != preview.get(field) for field in bound_fields):
            raise AdapterError("RESTORE_PREVIEW_STALE", "restore preview changed before activation")
        return current

    def _journal_path(self, operation_id: str) -> Path:
        return self.state_dir / f"restore-{operation_id}.json"

    def _read_journal_optional(self, operation_id: str) -> dict[str, Any] | None:
        path = self._journal_path(operation_id)
        if not path.exists():
            return None
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "restore journal is not owner-controlled")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "restore journal permissions are too broad")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "restore journal is unreadable") from exc
        if not isinstance(raw, dict) or raw.get("operation_id") != operation_id:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "restore journal identity is invalid")
        if "service_initial" in raw:
            self._validated_service_initial(raw["service_initial"])
        return raw

    def read_journal(self, operation_id: str) -> dict[str, Any]:
        operation_id = _canonical_operation_id(operation_id)
        value = self._read_journal_optional(operation_id)
        if value is None:
            raise AdapterError("RESTORE_STATE_NOT_FOUND", "restore journal was not found")
        return dict(value)

    def _journal(self, operation_id: str, **payload: Any) -> dict[str, Any]:
        previous = self._read_journal_optional(operation_id) or {"operation_id": operation_id}
        merged = dict(previous)
        merged.update(payload)
        merged["operation_id"] = operation_id
        merged["updated_at"] = iso_now()
        _atomic_json(self._journal_path(operation_id), merged)
        return merged

    @staticmethod
    def _journal_matches_preview(journal: dict[str, Any], preview: dict[str, Any]) -> bool:
        return (
            journal.get("artifact_id") == preview.get("artifact_id")
            and journal.get("artifact_sha256") == preview.get("artifact_sha256")
            and journal.get("authorization_preview") == preview.get("authorization_preview")
            and journal.get("trusted_origin_attested") is True
            and preview.get("trusted_origin_attested") is True
        )

    @staticmethod
    def _recovery_from_phase(journal: dict[str, Any]) -> str:
        phase = journal.get("phase")
        if phase == "RECOVERY_REQUIRED":
            phase = journal.get("recovery_from_phase")
        if phase not in {"ACTIVATED_UNVERIFIED", "POSTFLIGHT_VERIFIED", "ACTIVATION_READY"}:
            raise AdapterError(
                "RESTORE_STATE_UNAVAILABLE",
                "restore recovery journal lost the post-activation phase that requires recovery",
            )
        return str(phase)

    def _record_recovery_required(
        self,
        operation_id: str,
        *,
        fallback_from_phase: str,
        activation_device: int | None = None,
        activation_inode: int | None = None,
    ) -> dict[str, Any]:
        current = self._read_journal_optional(operation_id) or {"operation_id": operation_id}
        current_phase = current.get("phase")
        if current_phase == "RECOVERY_REQUIRED":
            recovery_from_phase = current.get("recovery_from_phase")
        elif current_phase in {"ACTIVATED_UNVERIFIED", "POSTFLIGHT_VERIFIED", "ACTIVATION_READY"}:
            recovery_from_phase = current_phase
        else:
            recovery_from_phase = fallback_from_phase
        if recovery_from_phase not in {"ACTIVATED_UNVERIFIED", "POSTFLIGHT_VERIFIED", "ACTIVATION_READY"}:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "restore recovery phase is invalid")
        payload: dict[str, Any] = {
            "phase": "RECOVERY_REQUIRED",
            "recovery_from_phase": recovery_from_phase,
            "data_postflight_verified": bool(
                current.get("data_postflight_verified") is True
                or recovery_from_phase in {"POSTFLIGHT_VERIFIED", "ACTIVATION_READY"}
            ),
        }
        if activation_device is not None:
            payload["activation_device"] = activation_device
        if activation_inode is not None:
            payload["activation_inode"] = activation_inode
        return self._journal(operation_id, **payload)

    def _validated_recovery_state(
        self,
        preview: dict[str, Any],
        journal: dict[str, Any],
    ) -> tuple[PreparedMaintenance, int, int, int]:
        operation_id = _canonical_operation_id(str(journal.get("operation_id")))
        if not self._journal_matches_preview(journal, preview):
            raise AdapterError("IDEMPOTENCY_CONFLICT", "restore operation identity is bound to another preview")
        artifact = self.registry.get(str(journal.get("artifact_id")))
        if artifact.get("kind") != "SQLITE_BACKUP" or artifact.get("sha256") != journal.get("artifact_sha256"):
            raise AdapterError("ARTIFACT_CHECKSUM_MISMATCH", "restore artifact no longer matches recovery state")
        prepared = self._prepared_from_receipt(journal.get("prepared_maintenance"))
        if prepared.operation_id != operation_id:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "prepared maintenance belongs to another restore operation")
        generation = prepared.original_generation + 1
        phase = journal.get("phase")
        if phase in {"ACTIVATION_VERIFICATION_FAILED", "SERVICE_STATE_RECOVERY_REQUIRED", "POST_ACTIVATION_VERIFIED"}:
            recovery_from_phase = str(phase)
        else:
            recovery_from_phase = self._recovery_from_phase(journal)
        journal_generation = journal.get("generation")
        if recovery_from_phase != "ACTIVATED_UNVERIFIED" and journal_generation != generation:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "restore recovery generation does not match prepared maintenance")
        device = journal.get("activation_device")
        inode = journal.get("activation_inode")
        if (
            isinstance(device, bool)
            or not isinstance(device, int)
            or isinstance(inode, bool)
            or not isinstance(inode, int)
        ):
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "restore recovery lost activated database identity")
        try:
            active_info = self.active_db.stat()
        except FileNotFoundError as exc:
            raise AdapterError("BACKEND_UNAVAILABLE", "activated restore database is missing") from exc
        if (active_info.st_dev, active_info.st_ino) != (device, inode):
            raise AdapterError(
                "RESTORE_STATE_UNAVAILABLE",
                "active database identity no longer matches the activated restore journal",
            )
        return prepared, generation, device, inode

    def _verified_replay(self, preview: dict[str, Any], journal: dict[str, Any]) -> dict[str, Any]:
        if not self._journal_matches_preview(journal, preview):
            raise AdapterError("IDEMPOTENCY_CONFLICT", "restore operation identity is bound to another preview")
        artifact = self.registry.get(str(journal["artifact_id"]))
        if artifact.get("sha256") != journal.get("artifact_sha256"):
            raise AdapterError("ARTIFACT_CHECKSUM_MISMATCH", "restore artifact no longer matches the completed operation")
        result = journal.get("result")
        if not isinstance(result, dict) or result.get("status") != "OK":
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "completed restore journal is missing its result receipt")
        proof = journal.get("post_activation_verification")
        generation = journal.get("generation")
        device = journal.get("activation_device")
        inode = journal.get("activation_inode")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or isinstance(device, bool)
            or not isinstance(device, int)
            or isinstance(inode, bool)
            or not isinstance(inode, int)
        ):
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "completed restore journal lost activation identity")
        self._fresh_restored_metadata(preview, device=device, inode=inode)
        prepared = self._prepared_from_receipt(journal.get("prepared_maintenance"))
        ready_instance_ids = result.get("ready_instance_ids")
        reconnect_failures = result.get("reconnect_failures")
        if not isinstance(ready_instance_ids, list) or not isinstance(reconnect_failures, dict):
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "completed restore result lost runtime reconnection evidence")
        validated_proof = self._validate_product_proof(
            proof,
            preview=preview,
            prepared=prepared,
            operation_id=str(journal["operation_id"]),
            generation=generation,
            device=device,
            inode=inode,
            ready_instance_ids=ready_instance_ids,
            reconnect_failures=reconnect_failures,
        )
        if result.get("post_activation_verification") != validated_proof:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "completed restore result lost its verification receipt")
        replay = dict(result)
        replay["idempotent_replay"] = True
        return replay

    async def _finish_verified_journal(
        self,
        preview: dict[str, Any],
        journal: dict[str, Any],
        *,
        recovery_service: bool = False,
    ) -> dict[str, Any]:
        operation_id = _canonical_operation_id(str(journal.get("operation_id")))
        if not self._journal_matches_preview(journal, preview):
            raise AdapterError("IDEMPOTENCY_CONFLICT", "restore operation identity is bound to another preview")
        result = journal.get("result")
        generation = journal.get("generation")
        device = journal.get("activation_device")
        inode = journal.get("activation_inode")
        if (
            not isinstance(result, dict)
            or result.get("status") != "OK"
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or isinstance(device, bool)
            or not isinstance(device, int)
            or isinstance(inode, bool)
            or not isinstance(inode, int)
        ):
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "post-activation verified journal is incomplete")
        self._fresh_restored_metadata(preview, device=device, inode=inode)
        prepared = self._prepared_from_receipt(journal.get("prepared_maintenance"))
        ready_instance_ids = result.get("ready_instance_ids")
        reconnect_failures = result.get("reconnect_failures")
        if not isinstance(ready_instance_ids, list) or not isinstance(reconnect_failures, dict):
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "post-activation result lost runtime reconnection evidence")
        proof = self._validate_product_proof(
            journal.get("post_activation_verification"),
            preview=preview,
            prepared=prepared,
            operation_id=operation_id,
            generation=generation,
            device=device,
            inode=inode,
            ready_instance_ids=ready_instance_ids,
            reconnect_failures=reconnect_failures,
        )
        if result.get("post_activation_verification") != proof:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "post-activation result does not match its verification receipt")
        try:
            if recovery_service:
                await self._finish_service_after_recovery(operation_id)
            else:
                await self._finish_service_maintenance(operation_id)
        except Exception as exc:
            await self._mark_service_recovery_required()
            self._journal(
                operation_id,
                phase="SERVICE_STATE_RECOVERY_REQUIRED",
                verification_error={
                    "code": "SERVICE_STATE_FINALIZATION_FAILED",
                    "message": "restore data activation is verified but WebUI service-state finalization failed",
                },
            )
            raise RestoreRecoveryRequired(
                operation_id,
                "restore data activation is verified but WebUI service-state recovery is required",
            ) from exc
        self._journal(
            operation_id,
            phase="VERIFIED",
            result=result,
            generation=generation,
            activation_device=device,
            activation_inode=inode,
            post_activation_verification=proof,
        )
        replay = dict(result)
        replay["idempotent_replay"] = True
        return replay

    def _activation_verification_failure(
        self,
        operation_id: str,
        exc: BaseException,
        *,
        generation: int,
        device: int,
        inode: int,
        ready_instance_ids: list[str] | None = None,
        reconnect_failures: dict[str, str] | None = None,
        maintenance_participant_failures: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if isinstance(exc, AdapterError):
            code = exc.code
            message = exc.safe_message
        else:
            code = "POST_ACTIVATION_VERIFICATION_FAILED"
            message = "post-activation product verification did not complete"
        current = self._read_journal_optional(operation_id) or {"operation_id": operation_id}
        result = current.get("result")
        completed_result = dict(result) if isinstance(result, dict) else None
        payload: dict[str, Any] = {}
        if ready_instance_ids is not None and reconnect_failures is not None:
            if completed_result is not None:
                completed_result["ready_instance_ids"] = list(ready_instance_ids)
                completed_result["reconnect_failures"] = dict(reconnect_failures)
                payload["result"] = completed_result
            payload["ready_instance_ids"] = list(ready_instance_ids)
            payload["reconnect_failures"] = dict(reconnect_failures)
        if maintenance_participant_failures is not None:
            payload["maintenance_participant_failures"] = _bounded_participant_failures(
                maintenance_participant_failures
            )
        return self._journal(
            operation_id,
            phase="ACTIVATION_VERIFICATION_FAILED",
            generation=generation,
            activation_device=device,
            activation_inode=inode,
            data_postflight_verified=True,
            barrier_state="RUNNING",
            verification_error={"code": code, "message": message},
            **payload,
        )

    async def _complete_running_activation(
        self,
        *,
        preview: dict[str, Any],
        prepared: PreparedMaintenance,
        result: dict[str, Any],
        generation: int,
        device: int,
        inode: int,
        replay: bool,
        recovery_service: bool = False,
    ) -> dict[str, Any]:
        operation_id = prepared.operation_id
        resume_timed_out = False
        try:
            resume = await self.coordinator.resume_running(
                prepared,
                new_generation=generation,
            )
            ready_instance_ids = list(getattr(resume, "ready_instance_ids", ()))
            reconnect_failures = dict(getattr(resume, "failures", {}))
            try:
                maintenance_participant_failures = _bounded_participant_failures(
                    getattr(resume, "participant_failures", {})
                )
            except AdapterError:
                maintenance_participant_failures = {
                    "maintenance_coordinator": "PARTICIPANT_REPORT_INVALID"
                }
        except MaintenanceCoordinatorError as exc:
            if exc.code == "RUNTIME_RESUME_TIMEOUT":
                ready_instance_ids = []
                reconnect_failures = {"coordinator": "RUNTIME_RESUME_TIMEOUT"}
                maintenance_participant_failures = {}
                resume_timed_out = True
            else:
                ready_instance_ids = []
                reconnect_failures = {"coordinator": "RUNTIME_RESUME_FAILED"}
                maintenance_participant_failures = {}
        except Exception:
            ready_instance_ids = []
            reconnect_failures = {"coordinator": "RUNTIME_RESUME_FAILED"}
            maintenance_participant_failures = {}

        completed_result = dict(result)
        completed_result["ready_instance_ids"] = ready_instance_ids
        completed_result["reconnect_failures"] = reconnect_failures
        if resume_timed_out or maintenance_participant_failures:
            if resume_timed_out:
                failure = AdapterError(
                    "RUNTIME_RESUME_TIMEOUT",
                    "runtime resume exceeded its bounded post-RUNNING timeout",
                )
                recovery_message = "restore runtime resume timed out and requires authenticated recovery"
            else:
                failure = AdapterError(
                    "MAINTENANCE_PARTICIPANT_RECOVERY_REQUIRED",
                    "maintenance participant reopen requires recovery",
                )
                recovery_message = "restore maintenance participant recovery is required before product verification"
            self._activation_verification_failure(
                operation_id,
                failure,
                generation=generation,
                device=device,
                inode=inode,
                ready_instance_ids=ready_instance_ids,
                reconnect_failures=reconnect_failures,
                maintenance_participant_failures=maintenance_participant_failures,
            )
            try:
                await self._mark_service_recovery_required()
            except Exception:
                pass
            raise RestoreRecoveryRequired(operation_id, recovery_message)
        try:
            proof = await self._post_activation_product_proof(
                preview=preview,
                prepared=prepared,
                operation_id=operation_id,
                generation=generation,
                device=device,
                inode=inode,
                ready_instance_ids=ready_instance_ids,
                reconnect_failures=reconnect_failures,
            )
        except Exception as exc:
            self._activation_verification_failure(
                operation_id,
                exc,
                generation=generation,
                device=device,
                inode=inode,
                ready_instance_ids=ready_instance_ids,
                reconnect_failures=reconnect_failures,
                maintenance_participant_failures=maintenance_participant_failures,
            )
            try:
                await self._mark_service_recovery_required()
            except Exception:
                pass
            raise RestoreRecoveryRequired(
                operation_id,
                "restore data postflight passed, but post-activation product verification requires recovery",
            ) from exc

        completed_result["post_activation_verification"] = proof
        self._journal(
            operation_id,
            phase="POST_ACTIVATION_VERIFIED",
            result=completed_result,
            generation=generation,
            activation_device=device,
            activation_inode=inode,
            post_activation_verification=proof,
            data_postflight_verified=True,
            barrier_state="RUNNING",
            ready_instance_ids=ready_instance_ids,
            reconnect_failures=reconnect_failures,
            maintenance_participant_failures={},
        )
        try:
            if recovery_service:
                await self._finish_service_after_recovery(operation_id)
            else:
                await self._finish_service_maintenance(operation_id)
        except Exception as exc:
            try:
                await self._mark_service_recovery_required()
            finally:
                self._journal(
                    operation_id,
                    phase="SERVICE_STATE_RECOVERY_REQUIRED",
                    verification_error={
                        "code": "SERVICE_STATE_FINALIZATION_FAILED",
                        "message": "restore product verification passed but WebUI service-state finalization failed",
                    },
                )
            raise RestoreRecoveryRequired(
                operation_id,
                "restore product verification passed but WebUI service-state recovery is required",
            ) from exc
        self._journal(
            operation_id,
            phase="VERIFIED",
            result=completed_result,
            generation=generation,
            activation_device=device,
            activation_inode=inode,
            post_activation_verification=proof,
            ready_instance_ids=ready_instance_ids,
            reconnect_failures=reconnect_failures,
            maintenance_participant_failures={},
        )
        if replay:
            completed_result = dict(completed_result)
            completed_result["idempotent_replay"] = True
        return completed_result

    async def _reconcile_activation_ready(
        self,
        preview: dict[str, Any],
        journal: dict[str, Any],
        *,
        recovery_service: bool = False,
    ) -> dict[str, Any]:
        operation_id = _canonical_operation_id(str(journal.get("operation_id")))
        if self.coordinator is None:
            raise AdapterError("RESTORE_NOT_CONFIGURED", "full restore runtime coordination is not configured")
        prepared, generation, device, inode = self._validated_recovery_state(preview, journal)
        result = journal.get("result")
        if not isinstance(result, dict) or result.get("status") != "OK":
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "activation-ready restore journal is incomplete")

        snapshot = self.coordinator.activation_state(
            operation_id,
            new_generation=generation,
            device=device,
            inode=inode,
        )
        if snapshot.state == "ACTIVATING":
            with MaintenanceLease(str(self.active_db), f"restore-reconcile:{operation_id}"):
                interlock = _read_interlock(self.active_db)
                if interlock not in {None, operation_id}:
                    raise AdapterError("RECOVERY_REQUIRED", "restore recovery interlock belongs to another operation")
                if interlock == operation_id:
                    _clear_interlock(self.active_db, operation_id)
                _validate_sqlite_image(self.active_db)
                try:
                    self.coordinator.publish_running(
                        prepared,
                        new_generation=generation,
                    )
                except Exception:
                    try:
                        published = self.coordinator.activation_state(
                            operation_id,
                            new_generation=generation,
                            device=device,
                            inode=inode,
                        )
                    except Exception:
                        _write_interlock(self.active_db, operation_id)
                        raise
                    if getattr(published, "state", None) != "RUNNING":
                        _write_interlock(self.active_db, operation_id)
                        raise
        elif snapshot.state == "RUNNING":
            if _read_interlock(self.active_db) is not None:
                raise AdapterError("RECOVERY_REQUIRED", "RUNNING restore activation still carries a recovery interlock")
            self._fresh_restored_metadata(preview, device=device, inode=inode)
        else:  # activation_state must fail closed before returning another state.
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "activation-ready barrier state is invalid")

        return await self._complete_running_activation(
            preview=preview,
            prepared=prepared,
            result=result,
            generation=generation,
            device=device,
            inode=inode,
            replay=True,
            recovery_service=recovery_service,
        )

    def _recovery_result_candidate(
        self,
        preview: dict[str, Any],
        journal: dict[str, Any],
        *,
        generation: int,
    ) -> dict[str, Any]:
        operation_id = _canonical_operation_id(str(journal.get("operation_id")))
        existing = journal.get("result")
        if isinstance(existing, dict) and existing.get("status") == "OK":
            if (
                existing.get("operation") != "FULL_RESTORE"
                or existing.get("operation_id") != operation_id
                or existing.get("artifact_id") != preview.get("artifact_id")
                or existing.get("artifact_sha256") != preview.get("artifact_sha256")
                or existing.get("generation") != generation
            ):
                raise AdapterError("RESTORE_STATE_UNAVAILABLE", "restore recovery result identity is inconsistent")
            return dict(existing)
        pre_restore_artifact_id = journal.get("pre_restore_artifact_id")
        if not isinstance(pre_restore_artifact_id, str) or not pre_restore_artifact_id:
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "restore recovery lost its pre-restore snapshot identity")
        pre_restore = self.registry.get(pre_restore_artifact_id)
        if pre_restore.get("kind") != "SQLITE_BACKUP":
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "pre-restore recovery snapshot identity is invalid")
        return {
            "status": "OK",
            "operation": "FULL_RESTORE",
            "operation_id": operation_id,
            "artifact_id": preview["artifact_id"],
            "artifact_sha256": preview["artifact_sha256"],
            "pre_restore_artifact_id": pre_restore_artifact_id,
            "generation": generation,
            "ready_instance_ids": [],
            "reconnect_failures": {},
        }

    async def _resume_prepublication_recovery(
        self,
        *,
        preview: dict[str, Any],
        journal: dict[str, Any],
        prepared: PreparedMaintenance,
        generation: int,
        device: int,
        inode: int,
    ) -> dict[str, Any]:
        operation_id = prepared.operation_id
        recovery_from_phase = self._recovery_from_phase(journal)
        recovery_service = await self._service_status() == "RECOVERY_REQUIRED"
        progress_phase = recovery_from_phase
        running_published = False
        result_candidate = self._recovery_result_candidate(preview, journal, generation=generation)
        try:
            with MaintenanceLease(str(self.active_db), f"restore-recovery:{operation_id}") as lease:
                current_interlock = _read_interlock(self.active_db)
                if current_interlock not in {None, operation_id}:
                    raise AdapterError("RECOVERY_REQUIRED", "restore recovery interlock belongs to another operation")
                if current_interlock is None:
                    _write_interlock(self.active_db, operation_id)
                if journal.get("phase") == "RECOVERY_REQUIRED":
                    self.coordinator.rearm_recovery(prepared)

                postflight_verified = bool(
                    journal.get("data_postflight_verified") is True
                    or recovery_from_phase in {"POSTFLIGHT_VERIFIED", "ACTIVATION_READY"}
                )
                if not postflight_verified:
                    if lease.capability is None:
                        raise RestoreRecoveryRequired(operation_id, "restore maintenance capability is unavailable")
                    await _maybe_await(self._postflight(str(self.active_db), lease.capability))
                    postflight_verified = True
                    progress_phase = "POSTFLIGHT_VERIFIED"
                    self._journal(
                        operation_id,
                        phase="RECOVERY_REQUIRED" if recovery_service else "POSTFLIGHT_VERIFIED",
                        recovery_from_phase="POSTFLIGHT_VERIFIED" if recovery_service else journal.get("recovery_from_phase"),
                        data_postflight_verified=True,
                        generation=generation,
                        activation_device=device,
                        activation_inode=inode,
                    )

                activation = self.coordinator.begin_activation(
                    prepared,
                    new_generation=generation,
                )
                if (
                    getattr(activation, "generation", None) != generation
                    or getattr(activation, "device", None) != device
                    or getattr(activation, "inode", None) != inode
                ):
                    raise RestoreRecoveryRequired(
                        operation_id,
                        "durable activation barrier does not match the recovered database identity",
                    )
                await _maybe_await(self._invalidate_sessions())
                result_candidate = self._recovery_result_candidate(preview, journal, generation=generation)
                self._journal(
                    operation_id,
                    phase="ACTIVATION_READY",
                    result=result_candidate,
                    generation=generation,
                    activation_device=device,
                    activation_inode=inode,
                    prepared_maintenance=self._prepared_receipt(prepared),
                    data_postflight_verified=True,
                    recovery_from_phase=progress_phase,
                )
                progress_phase = "ACTIVATION_READY"
                _clear_interlock(self.active_db, operation_id)
                _validate_sqlite_image(self.active_db)
                try:
                    self.coordinator.publish_running(
                        prepared,
                        new_generation=generation,
                    )
                    running_published = True
                except Exception:
                    try:
                        published = self.coordinator.activation_state(
                            operation_id,
                            new_generation=generation,
                            device=device,
                            inode=inode,
                        )
                    except Exception:
                        _write_interlock(self.active_db, operation_id)
                        raise
                    if getattr(published, "state", None) != "RUNNING":
                        _write_interlock(self.active_db, operation_id)
                        raise
                    running_published = True
        except RestoreRecoveryRequired:
            if running_published:
                raise
            try:
                if _read_interlock(self.active_db) is None:
                    _write_interlock(self.active_db, operation_id)
            except Exception:
                pass
            try:
                self.coordinator.mark_recovery_required(operation_id)
            finally:
                await self._mark_service_recovery_required()
                self._record_recovery_required(
                    operation_id,
                    fallback_from_phase=progress_phase,
                    activation_device=device,
                    activation_inode=inode,
                )
            raise
        except Exception as exc:
            if running_published:
                raise
            try:
                if _read_interlock(self.active_db) is None:
                    _write_interlock(self.active_db, operation_id)
            except Exception:
                pass
            try:
                self.coordinator.mark_recovery_required(operation_id)
            finally:
                await self._mark_service_recovery_required()
                self._record_recovery_required(
                    operation_id,
                    fallback_from_phase=progress_phase,
                    activation_device=device,
                    activation_inode=inode,
                )
            raise RestoreRecoveryRequired(
                operation_id,
                "restore recovery did not reach safe RUNNING publication",
            ) from exc

        return await self._complete_running_activation(
            preview=preview,
            prepared=prepared,
            result=result_candidate,
            generation=generation,
            device=device,
            inode=inode,
            replay=True,
            recovery_service=recovery_service,
        )

    async def _resume_recovery(
        self,
        *,
        preview: dict[str, Any],
        journal: dict[str, Any],
        authorize: AuthorizeCallback | None,
    ) -> dict[str, Any]:
        operation_id = _canonical_operation_id(str(journal.get("operation_id")))
        effective_authorize = self._require_recovery_authorize(authorize)
        prepared, generation, device, inode = self._validated_recovery_state(preview, journal)
        phase = journal.get("phase")
        validated_activation = None
        if phase in {"ACTIVATION_READY", "ACTIVATION_VERIFICATION_FAILED", "SERVICE_STATE_RECOVERY_REQUIRED"}:
            validated_activation = self.coordinator.activation_state(
                operation_id,
                new_generation=generation,
                device=device,
                inode=inode,
            )
        exact_authorization_preview = journal.get("authorization_preview")
        if not isinstance(exact_authorization_preview, str):
            raise AdapterError("RESTORE_STATE_UNAVAILABLE", "restore recovery lost its authorization preview")
        await _maybe_await(effective_authorize(exact_authorization_preview))

        if phase == "SERVICE_STATE_RECOVERY_REQUIRED":
            snapshot = validated_activation
            if getattr(snapshot, "state", None) != "RUNNING" or _read_interlock(self.active_db) is not None:
                raise AdapterError("RESTORE_STATE_UNAVAILABLE", "verified restore is not durably RUNNING")
            return await self._finish_verified_journal(
                preview,
                journal,
                recovery_service=True,
            )

        if phase == "ACTIVATION_VERIFICATION_FAILED":
            snapshot = validated_activation
            if getattr(snapshot, "state", None) != "RUNNING" or _read_interlock(self.active_db) is not None:
                raise AdapterError("RESTORE_STATE_UNAVAILABLE", "post-activation verification retry requires exact RUNNING identity")
            result = self._recovery_result_candidate(preview, journal, generation=generation)
            return await self._complete_running_activation(
                preview=preview,
                prepared=prepared,
                result=result,
                generation=generation,
                device=device,
                inode=inode,
                replay=True,
                recovery_service=True,
            )

        if phase == "ACTIVATION_READY":
            recovery_service = await self._service_status() == "RECOVERY_REQUIRED"
            try:
                return await self._reconcile_activation_ready(
                    preview,
                    journal,
                    recovery_service=recovery_service,
                )
            except RestoreRecoveryRequired:
                raise
            except Exception as exc:
                try:
                    if _read_interlock(self.active_db) is None:
                        _write_interlock(self.active_db, operation_id)
                except Exception:
                    pass
                try:
                    self.coordinator.mark_recovery_required(operation_id)
                finally:
                    await self._mark_service_recovery_required()
                    self._record_recovery_required(
                        operation_id,
                        fallback_from_phase="ACTIVATION_READY",
                        activation_device=device,
                        activation_inode=inode,
                    )
                raise RestoreRecoveryRequired(
                    operation_id,
                    "restore activation-ready reconciliation requires another authenticated recovery retry",
                ) from exc

        return await self._resume_prepublication_recovery(
            preview=preview,
            journal=journal,
            prepared=prepared,
            generation=generation,
            device=device,
            inode=inode,
        )

    async def execute(
        self,
        *,
        preview: dict[str, Any],
        operation_id: str,
        now: float | None = None,
        authorize: AuthorizeCallback | None = None,
    ) -> dict[str, Any]:
        operation_id = _canonical_operation_id(operation_id)
        journal = self._read_journal_optional(operation_id)
        if journal is not None and journal.get("phase") == "VERIFIED":
            return self._verified_replay(preview, journal)
        if journal is not None and journal.get("phase") == "POST_ACTIVATION_VERIFIED":
            self._require_executable_configuration(authorize)
            return await self._finish_verified_journal(preview, journal)
        if journal is not None and journal.get("phase") in {
            "ACTIVATION_VERIFICATION_FAILED",
            "SERVICE_STATE_RECOVERY_REQUIRED",
            "ACTIVATION_READY",
            "ACTIVATED_UNVERIFIED",
            "POSTFLIGHT_VERIFIED",
            "RECOVERY_REQUIRED",
        }:
            return await self._resume_recovery(
                preview=preview,
                journal=journal,
                authorize=authorize,
            )
        effective_authorize = self._require_executable_configuration(authorize)
        current = self._assert_preview_current(preview)
        if journal is not None and not self._journal_matches_preview(journal, current):
            raise AdapterError("IDEMPOTENCY_CONFLICT", "restore operation identity is bound to another preview")
        snapshot_required = not (
            journal is not None and isinstance(journal.get("pre_restore_artifact_id"), str)
        )
        self._preflight_restore_capacity(current, snapshot_required=snapshot_required)
        drain_deadline = (time.time() if now is None else float(now)) + 30.0
        observed_service = await self._service_snapshot_receipt()
        if (
            journal is not None
            and journal.get("phase") == "MAINTENANCE_ENTERED"
            and observed_service["status"] == "MAINTENANCE"
        ):
            service_initial = self._validated_service_initial(journal.get("service_initial"))
        else:
            service_initial = observed_service
        await self._begin_service_maintenance(operation_id)
        try:
            journal = self._journal(
                operation_id,
                phase="MAINTENANCE_ENTERED",
                artifact_id=current["artifact_id"],
                artifact_sha256=current["artifact_sha256"],
                trusted_origin_attested=True,
                authorization_preview=current["authorization_preview"],
                original_database_device=current["active_database_device"],
                original_database_inode=current["active_database_inode"],
                service_initial=service_initial,
            )
        except BaseException:
            await self._finish_service_maintenance(operation_id)
            raise
        try:
            prepared = await self.coordinator.prepare_close(operation_id, drain_deadline=drain_deadline)
        except BaseException as exc:
            if getattr(exc, "code", None) == "MAINTENANCE_RECOVERY_REQUIRED":
                await self._mark_service_recovery_required()
                self._journal(
                    operation_id,
                    phase="MAINTENANCE_PREPARE_RECOVERY_REQUIRED",
                    verification_error={
                        "code": "MAINTENANCE_RECOVERY_REQUIRED",
                        "message": "restore maintenance preparation requires recovery before any database mutation",
                    },
                )
                raise RestoreRecoveryRequired(
                    operation_id,
                    "restore maintenance preparation requires recovery before any database mutation",
                ) from exc
            await self._finish_service_maintenance(operation_id)
            self._journal(operation_id, phase="ABORTED_BEFORE_PREPARE")
            raise
        if prepared.operation_id != operation_id:
            await self._finish_service_maintenance(operation_id)
            self._journal(operation_id, phase="ABORTED_BEFORE_PREPARE")
            raise AdapterError("RESTORE_COORDINATION_FAILED", "maintenance coordinator returned the wrong operation")

        promoted = False
        running_published = False
        stage: Path | None = None
        active_info: os.stat_result | None = None
        pre_restore_artifact_id = f"pre-restore-{operation_id}"
        try:
            # Closing runtimes can take time. Rebind every mutable input before the
            # fresh one-use authorization and before the first active-image mutation.
            current = self._assert_preview_current(current)
            self._preflight_restore_capacity(current, snapshot_required=snapshot_required)
            if journal is not None and isinstance(journal.get("pre_restore_artifact_id"), str):
                pre_restore = self.registry.get(journal["pre_restore_artifact_id"])
                if pre_restore.get("kind") != "SQLITE_BACKUP":
                    raise AdapterError("RESTORE_STATE_UNAVAILABLE", "pre-restore artifact identity is invalid")
            else:
                pre_restore = create_consistent_backup(
                    self.active_db,
                    self.registry,
                    artifact_id=pre_restore_artifact_id,
                )
            self._journal(
                operation_id,
                phase="SNAPSHOT_READY",
                artifact_id=current["artifact_id"],
                artifact_sha256=current["artifact_sha256"],
                trusted_origin_attested=True,
                authorization_preview=current["authorization_preview"],
                original_database_device=current["active_database_device"],
                original_database_inode=current["active_database_inode"],
                pre_restore_artifact_id=pre_restore["artifact_id"],
                generation=prepared.original_generation,
                prepared_maintenance=self._prepared_receipt(prepared),
            )
            await _maybe_await(effective_authorize(current["authorization_preview"]))
            current = self._assert_preview_current(current)
            artifact_receipt = self.registry.get(current["artifact_id"])
            artifact = self._artifact_path(artifact_receipt)

            stage = self.active_db.with_name(f".{self.active_db.name}.restore-{operation_id}.stage")
            if stage.exists() or stage.is_symlink():
                raise AdapterError("RESTORE_STAGE_COLLISION", "restore staging path already exists")
            fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            shutil.copyfile(artifact, stage)
            os.chmod(stage, 0o600)
            _validate_sqlite_image(stage)
            _write_interlock(stage, operation_id)
            self._journal(
                operation_id,
                phase="PREPARED",
                artifact_id=current["artifact_id"],
                artifact_sha256=current["artifact_sha256"],
                pre_restore_artifact_id=pre_restore["artifact_id"],
                generation=prepared.original_generation,
            )

            try:
                with MaintenanceLease(str(self.active_db), f"restore:{operation_id}") as lease:
                    lease.promote_sanitized_image(stage)
                    promoted = True
                    stage = None
                    active_info = self.active_db.stat()
                    self._journal(
                        operation_id,
                        phase="ACTIVATED_UNVERIFIED",
                        artifact_id=current["artifact_id"],
                        artifact_sha256=current["artifact_sha256"],
                        pre_restore_artifact_id=pre_restore["artifact_id"],
                        generation=prepared.original_generation,
                        activation_device=active_info.st_dev,
                        activation_inode=active_info.st_ino,
                        data_postflight_verified=False,
                    )
                    # The promoted inode is a complete replacement image. Any sidecars
                    # from the retired database must not be replayed against it. All
                    # normal engines are closed and the exclusive inode lease is held.
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(str(self.active_db) + suffix)
                        if sidecar.exists() and not sidecar.is_symlink():
                            sidecar.unlink()
                        elif sidecar.is_symlink():
                            raise RestoreRecoveryRequired(
                                operation_id,
                                "active database sidecar path became a symlink during restore",
                            )
                    if lease.capability is None:
                        raise RestoreRecoveryRequired(operation_id, "restore maintenance capability is unavailable")
                    await _maybe_await(self._postflight(str(self.active_db), lease.capability))
                    new_generation = prepared.original_generation + 1
                    active_info = self.active_db.stat()
                    self._journal(
                        operation_id,
                        phase="POSTFLIGHT_VERIFIED",
                        artifact_id=current["artifact_id"],
                        artifact_sha256=current["artifact_sha256"],
                        pre_restore_artifact_id=pre_restore["artifact_id"],
                        generation=new_generation,
                        activation_device=active_info.st_dev,
                        activation_inode=active_info.st_ino,
                        data_postflight_verified=True,
                    )
                    activation = self.coordinator.begin_activation(
                        prepared,
                        new_generation=new_generation,
                    )
                    if (
                        getattr(activation, "generation", None) != new_generation
                        or getattr(activation, "device", None) != active_info.st_dev
                        or getattr(activation, "inode", None) != active_info.st_ino
                    ):
                        raise RestoreRecoveryRequired(
                            operation_id,
                            "durable activation barrier does not match the promoted database identity",
                        )
                    await _maybe_await(self._invalidate_sessions())
                    result_candidate = {
                        "status": "OK",
                        "operation": "FULL_RESTORE",
                        "operation_id": operation_id,
                        "artifact_id": current["artifact_id"],
                        "artifact_sha256": current["artifact_sha256"],
                        "pre_restore_artifact_id": pre_restore_artifact_id,
                        "generation": new_generation,
                        "ready_instance_ids": [],
                        "reconnect_failures": {},
                    }
                    self._journal(
                        operation_id,
                        phase="ACTIVATION_READY",
                        result=result_candidate,
                        generation=new_generation,
                        activation_device=active_info.st_dev,
                        activation_inode=active_info.st_ino,
                        prepared_maintenance=self._prepared_receipt(prepared),
                    )
                    _clear_interlock(self.active_db, operation_id)
                    _validate_sqlite_image(self.active_db)
                    try:
                        self.coordinator.publish_running(
                            prepared,
                            new_generation=new_generation,
                        )
                        running_published = True
                    except Exception:
                        try:
                            published = self.coordinator.activation_state(
                                operation_id,
                                new_generation=new_generation,
                                device=active_info.st_dev,
                                inode=active_info.st_ino,
                            )
                        except Exception:
                            _write_interlock(self.active_db, operation_id)
                            raise
                        if getattr(published, "state", None) != "RUNNING":
                            _write_interlock(self.active_db, operation_id)
                            raise
                        running_published = True
            except RecoveryHold as exc:
                if promoted:
                    raise RestoreRecoveryRequired(operation_id, exc.safe_message) from exc
                raise AdapterError("RESTORE_HOLD", exc.safe_message) from exc

            return await self._complete_running_activation(
                preview=preview,
                prepared=PreparedMaintenance(
                    operation_id=prepared.operation_id,
                    original_generation=prepared.original_generation,
                    intent_revision=prepared.intent_revision,
                    initially_open_instance_ids=prepared.initially_open_instance_ids,
                    registrations=prepared.registrations,
                ),
                result=result_candidate,
                generation=new_generation,
                device=active_info.st_dev,
                inode=active_info.st_ino,
                replay=False,
            )
        except RestoreRecoveryRequired:
            if running_published:
                raise
            recovery_info = active_info if active_info is not None else self.active_db.stat()
            if promoted:
                try:
                    interlock = _read_interlock(self.active_db)
                    if interlock is None:
                        _write_interlock(self.active_db, operation_id)
                except Exception:
                    pass
            self.coordinator.mark_recovery_required(operation_id)
            await self._mark_service_recovery_required()
            self._record_recovery_required(
                operation_id,
                fallback_from_phase="ACTIVATED_UNVERIFIED",
                activation_device=recovery_info.st_dev,
                activation_inode=recovery_info.st_ino,
            )
            raise
        except Exception:
            if promoted:
                recovery_info = active_info if active_info is not None else self.active_db.stat()
                if running_published:
                    self._activation_verification_failure(
                        operation_id,
                        RuntimeError("post-activation completion failed"),
                        generation=new_generation,
                        device=recovery_info.st_dev,
                        inode=recovery_info.st_ino,
                    )
                    try:
                        await self._mark_service_recovery_required()
                    except Exception:
                        pass
                    raise RestoreRecoveryRequired(
                        operation_id,
                        "restore data activation is RUNNING but product verification or service-state finalization requires recovery",
                    )
                try:
                    interlock = _read_interlock(self.active_db)
                    if interlock is None:
                        _write_interlock(self.active_db, operation_id)
                except Exception:
                    pass
                self.coordinator.mark_recovery_required(operation_id)
                await self._mark_service_recovery_required()
                self._record_recovery_required(
                    operation_id,
                    fallback_from_phase="ACTIVATED_UNVERIFIED",
                    activation_device=recovery_info.st_dev,
                    activation_inode=recovery_info.st_ino,
                )
                raise RestoreRecoveryRequired(
                    operation_id,
                    "replacement database was activated but restore verification did not complete",
                )
            try:
                await self.coordinator.abort_pre_mutation(prepared)
            except BaseException:
                await self._mark_service_recovery_required()
                raise
            await self._finish_service_maintenance(operation_id)
            self._journal(
                operation_id,
                phase="ABORTED_BEFORE_ACTIVATION",
                artifact_id=current["artifact_id"],
                artifact_sha256=current["artifact_sha256"],
                trusted_origin_attested=True,
                authorization_preview=current["authorization_preview"],
            )
            raise
        finally:
            if stage is not None and stage.exists() and not stage.is_symlink():
                stage.unlink()
