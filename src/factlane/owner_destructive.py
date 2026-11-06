from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence
import uuid

from .embeddings import EmbeddingProfile
from .maintenance_coordinator import MaintenanceCoordinatorError, PreparedMaintenance
from .recovery import (
    RecoveryActivationHooks,
    RecoveryHold,
    RecoveryPlan,
    RecoveryResult,
    RecoveryScope,
    RecoveryTarget,
)
from .runtime_control import RuntimeIdentity


_PURGE_KIND = "FACTLANE_OWNER_PERMANENT_PURGE_V1"
_VERIFIED_STATE = "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
_SEALING_INCOMPLETE = "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
_ACTIVATION_READY = "S1_LOCAL_FACTLANE_ACTIVATION_READY"
_RESULT_VERSION = 1
_JOURNAL_VERSION = 1
_MAX_MAINTENANCE_PARTICIPANT_FAILURES = 8
_MAX_MAINTENANCE_PARTICIPANT_FAILURE_BYTES = 2048

BACKUP_RETENTION_WARNING = (
    "This operation removes the selected payload from the active FactLane database and its promoted "
    "recovery image. Retained backups may still contain earlier data and must be handled separately."
)


class OwnerDestructiveError(RuntimeError):
    """Fail-closed Owner destructive-operation error safe for an operator surface."""

    def __init__(self, code: str, safe_message: str, *, recovery_required: bool = False) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.recovery_required = recovery_required


@dataclass(frozen=True, slots=True)
class PurgeTargetIdentity:
    scope: RecoveryScope
    memory_id: str
    record_id: str
    revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.scope, RecoveryScope):
            raise TypeError("scope must be a RecoveryScope")
        if not isinstance(self.memory_id, str) or not self.memory_id:
            raise ValueError("memory_id is required")
        if not isinstance(self.record_id, str) or not self.record_id:
            raise ValueError("record_id is required")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "scope": self.scope.scope,
            "project_id": self.scope.project_id,
            "worktree_id": self.scope.worktree_id,
            "workflow_id": self.scope.workflow_id,
            "agent_id": self.scope.agent_id,
            "memory_id": self.memory_id,
            "record_id": self.record_id,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class PurgePreview:
    operation_id: str
    targets: tuple[PurgeTargetIdentity, ...]
    backup_retention_warning: str
    preview_digest: str
    typed_confirmation: str
    authorization_preview: str
    _target_state_digest: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "targets": [target.to_mapping() for target in self.targets],
            "backup_retention_warning": self.backup_retention_warning,
            "preview_digest": self.preview_digest,
            "typed_confirmation": self.typed_confirmation,
        }


@dataclass(frozen=True, slots=True)
class PurgeSuccess:
    operation_id: str
    preview_digest: str
    status: str
    receipt_path: str
    state_path: str
    target_count: int
    ready_instance_ids: tuple[str, ...]
    resume_failures: Mapping[str, str]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "preview_digest": self.preview_digest,
            "status": self.status,
            "receipt_path": self.receipt_path,
            "state_path": self.state_path,
            "target_count": self.target_count,
            "ready_instance_ids": list(self.ready_instance_ids),
            "resume_failures": dict(self.resume_failures),
        }


TargetResolver = Callable[
    [tuple[PurgeTargetIdentity, ...]],
    Sequence[RecoveryTarget] | Awaitable[Sequence[RecoveryTarget]],
]
PostCloseTargetResolver = Callable[
    [str, tuple[PurgeTargetIdentity, ...]],
    Sequence[RecoveryTarget] | Awaitable[Sequence[RecoveryTarget]],
]
FreshGrantVerifier = Callable[..., Any | Awaitable[Any]]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bounded_string_mapping(
    value: Any,
    *,
    field: str,
    max_entries: int,
    max_bytes: int,
    max_key_bytes: int = 128,
) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > max_entries:
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", f"persisted {field} is malformed")
    bounded: dict[str, str] = {}
    for key, code in value.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key.encode("utf-8")) > max_key_bytes
            or not isinstance(code, str)
            or not code
            or len(code.encode("utf-8")) > 128
        ):
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", f"persisted {field} is malformed")
        bounded[key] = code
    if len(_canonical_json(bounded).encode("utf-8")) > max_bytes:
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", f"persisted {field} is oversized")
    return bounded


def _bounded_ready_instance_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or len(value) > 64:
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted runtime READY evidence is malformed")
    result = tuple(value)
    if (
        any(not isinstance(item, str) or not item or len(item.encode("utf-8")) > 128 for item in result)
        or len(set(result)) != len(result)
    ):
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted runtime READY evidence is malformed")
    return result


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _canonical_operation_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise OwnerDestructiveError("INVALID_PURGE_OPERATION", "purge operation_id must be a UUID") from exc
    canonical = str(parsed)
    if not isinstance(value, str) or value.lower() != canonical:
        raise OwnerDestructiveError(
            "INVALID_PURGE_OPERATION",
            "purge operation_id must use canonical UUID form",
        )
    return canonical


def _identity_from_target(target: RecoveryTarget) -> PurgeTargetIdentity:
    return PurgeTargetIdentity(
        scope=target.scope,
        memory_id=target.memory_id,
        record_id=target.record_id,
        revision=target.revision,
    )


def _identity_key(identity: PurgeTargetIdentity) -> tuple[Any, ...]:
    return (
        identity.scope.scope,
        identity.scope.project_id,
        identity.scope.worktree_id,
        identity.scope.workflow_id,
        identity.scope.agent_id,
        identity.memory_id,
        identity.record_id,
        identity.revision,
    )


def _target_state_projection(target: RecoveryTarget) -> dict[str, Any]:
    """Internal recheck binding; never exposed in the Owner confirmation surface."""

    return {
        "identity": _identity_from_target(target).to_mapping(),
        "expected_lifecycle": target.expected_lifecycle,
        "expected_materialization": target.expected_materialization,
        "expected_native_hash": target.expected_native_hash,
    }


def _target_to_mapping(target: RecoveryTarget) -> dict[str, Any]:
    return {
        "record_id": target.record_id,
        "memory_id": target.memory_id,
        "revision": target.revision,
        "scope": target.scope.scope,
        "project_id": target.scope.project_id,
        "worktree_id": target.scope.worktree_id,
        "workflow_id": target.scope.workflow_id,
        "agent_id": target.scope.agent_id,
        "expected_lifecycle": target.expected_lifecycle,
        "expected_materialization": target.expected_materialization,
        "expected_native_hash": target.expected_native_hash,
    }


def _target_from_mapping(value: Mapping[str, Any]) -> RecoveryTarget:
    required = {
        "record_id",
        "memory_id",
        "revision",
        "scope",
        "project_id",
        "worktree_id",
        "workflow_id",
        "agent_id",
        "expected_lifecycle",
        "expected_materialization",
        "expected_native_hash",
    }
    if set(value) != required:
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge target has unexpected fields")
    for field in ("record_id", "memory_id", "scope", "expected_lifecycle", "expected_materialization"):
        if not isinstance(value[field], str) or not value[field]:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge target fields are malformed")
    for field in ("project_id", "worktree_id", "workflow_id", "agent_id"):
        item = value[field]
        if item is not None and (not isinstance(item, str) or not item):
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge target scope is malformed")
    revision = value["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge target revision is malformed")
    native_hash = value["expected_native_hash"]
    if native_hash is not None and (not isinstance(native_hash, str) or not native_hash):
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge target hash is malformed")
    try:
        scope = RecoveryScope(
            value["scope"],
            project_id=value["project_id"],
            worktree_id=value["worktree_id"],
            workflow_id=value["workflow_id"],
            agent_id=value["agent_id"],
        )
        return RecoveryTarget(
            record_id=value["record_id"],
            memory_id=value["memory_id"],
            revision=revision,
            scope=scope,
            expected_lifecycle=value["expected_lifecycle"],
            expected_materialization=value["expected_materialization"],
            expected_native_hash=native_hash,
        )
    except (TypeError, ValueError) as exc:
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge target is malformed") from exc


def _prepared_to_mapping(prepared: PreparedMaintenance) -> dict[str, Any]:
    return {
        "operation_id": prepared.operation_id,
        "original_generation": prepared.original_generation,
        "intent_revision": prepared.intent_revision,
        "initially_open_instance_ids": list(prepared.initially_open_instance_ids),
        "registrations": [identity.to_mapping() for identity in prepared.registrations],
    }


def _prepared_from_mapping(value: Mapping[str, Any]) -> PreparedMaintenance:
    required = {
        "operation_id",
        "original_generation",
        "intent_revision",
        "initially_open_instance_ids",
        "registrations",
    }
    if set(value) != required:
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted maintenance identity has unexpected fields")
    open_ids = value["initially_open_instance_ids"]
    registrations = value["registrations"]
    if not isinstance(open_ids, list) or not all(isinstance(item, str) for item in open_ids):
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted open-runtime identity is malformed")
    if not isinstance(registrations, list) or not all(isinstance(item, dict) for item in registrations):
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted runtime registrations are malformed")
    operation_id = value["operation_id"]
    original_generation = value["original_generation"]
    intent_revision = value["intent_revision"]
    if not isinstance(operation_id, str):
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted maintenance operation is malformed")
    _canonical_operation_id(operation_id)
    if (
        isinstance(original_generation, bool)
        or not isinstance(original_generation, int)
        or original_generation < 0
        or isinstance(intent_revision, bool)
        or not isinstance(intent_revision, int)
        or intent_revision < 0
    ):
        raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted maintenance generations are malformed")
    prepared = PreparedMaintenance(
        operation_id=operation_id,
        original_generation=original_generation,
        intent_revision=intent_revision,
        initially_open_instance_ids=tuple(open_ids),
        registrations=tuple(RuntimeIdentity.from_mapping(item) for item in registrations),
    )
    registered_ids = {identity.instance_id for identity in prepared.registrations}
    if not set(prepared.initially_open_instance_ids).issubset(registered_ids):
        raise OwnerDestructiveError(
            "PURGE_RECOVERY_STATE_MISMATCH",
            "persisted open-runtime identities are not present in the prepared registration set",
        )
    return prepared


def _service_snapshot_mapping(service_state: Any) -> dict[str, Any] | None:
    snapshotter = getattr(service_state, "snapshot", None)
    if not callable(snapshotter):
        return None
    snapshot = snapshotter()
    status = getattr(getattr(snapshot, "status", None), "value", getattr(snapshot, "status", None))
    intent_revision = getattr(snapshot, "intent_revision", None)
    stopped_boot_id = getattr(snapshot, "stopped_boot_id", None)
    if not isinstance(status, str) or isinstance(intent_revision, bool) or not isinstance(intent_revision, int):
        return None
    return {
        "status": status,
        "intent_revision": intent_revision,
        "stopped_boot_id": stopped_boot_id,
    }


def _ensure_private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise OwnerDestructiveError("PURGE_STATE_INVALID", "purge state root must be a real directory")
        if path.stat().st_uid != os.getuid():
            raise OwnerDestructiveError("PURGE_STATE_INVALID", "purge state root must be owned by the current user")
    else:
        path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


class OwnerDestructiveOrchestrator:
    """Owner-only permanent-purge lifecycle around the existing recovery operator.

    The orchestrator coordinates authorization and runtime exclusion. The recovery
    operator remains the authority for logical purge, pre-change preservation proof,
    sealing, promotion, and final verification. Success here describes verified local
    FactLane state only; it is not a claim about physical-media erasure or retained
    backups.
    """

    def __init__(
        self,
        *,
        db_path: str,
        profile: EmbeddingProfile,
        target_resolver: TargetResolver,
        post_close_target_resolver: PostCloseTargetResolver | None = None,
        require_post_close_target_resolver: bool = True,
        verify_fresh_grant: FreshGrantVerifier | None = None,
        coordinator: Any,
        recovery_operator: Any,
        service_state: Any,
        state_root: str | os.PathLike[str],
        retained_identity_fields_classified_non_sensitive: bool,
    ) -> None:
        if not isinstance(db_path, str) or not db_path:
            raise ValueError("db_path is required")
        if not isinstance(profile, EmbeddingProfile):
            raise TypeError("profile must be an EmbeddingProfile")
        if not callable(target_resolver):
            raise TypeError("target_resolver must be callable")
        if post_close_target_resolver is not None and not callable(post_close_target_resolver):
            raise TypeError("post-close target resolver must be callable")
        if require_post_close_target_resolver is True and post_close_target_resolver is None:
            raise TypeError("post-close target resolver is required for this composition")
        if not isinstance(require_post_close_target_resolver, bool):
            raise TypeError("require_post_close_target_resolver must be boolean")
        if verify_fresh_grant is not None and not callable(verify_fresh_grant):
            raise TypeError("verify_fresh_grant must be callable")
        if retained_identity_fields_classified_non_sensitive is not True:
            raise OwnerDestructiveError(
                "PURGE_IDENTITY_CLASSIFICATION_REQUIRED",
                "retained identity and scope fields must be classified non-sensitive before purge",
            )
        self._db_path = os.path.realpath(os.path.abspath(db_path))
        self._profile = profile
        self._target_resolver = target_resolver
        self._post_close_target_resolver = post_close_target_resolver
        self._require_post_close_target_resolver = require_post_close_target_resolver
        self._verify_fresh_grant = verify_fresh_grant
        self._coordinator = coordinator
        self._recovery_operator = recovery_operator
        self._service_state = service_state
        self._state_root = Path(state_root).absolute()
        _ensure_private_directory(self._state_root)

    async def _resolve_targets(
        self,
        identities: tuple[PurgeTargetIdentity, ...],
    ) -> tuple[RecoveryTarget, ...]:
        resolved_value = await _await_if_needed(self._target_resolver(identities))
        return self._normalize_resolved_targets(identities, resolved_value)

    @staticmethod
    def _normalize_resolved_targets(
        identities: tuple[PurgeTargetIdentity, ...],
        resolved_value: Any,
    ) -> tuple[RecoveryTarget, ...]:
        if not isinstance(resolved_value, Sequence) or isinstance(resolved_value, (str, bytes, bytearray)):
            raise OwnerDestructiveError("PURGE_TARGET_INVALID", "purge target resolver returned an invalid result")
        resolved = tuple(resolved_value)
        if not all(isinstance(target, RecoveryTarget) for target in resolved):
            raise OwnerDestructiveError("PURGE_TARGET_INVALID", "purge target resolver must return RecoveryTarget values")
        by_key: dict[tuple[Any, ...], RecoveryTarget] = {}
        for target in resolved:
            key = _identity_key(_identity_from_target(target))
            if key in by_key:
                raise OwnerDestructiveError("PURGE_TARGET_INVALID", "purge target resolver returned duplicate targets")
            by_key[key] = target
        requested_keys = tuple(_identity_key(identity) for identity in identities)
        if len(set(requested_keys)) != len(requested_keys):
            raise OwnerDestructiveError("PURGE_TARGET_INVALID", "purge target selection contains duplicates")
        if set(by_key) != set(requested_keys):
            raise OwnerDestructiveError(
                "PURGE_TARGET_MISMATCH",
                "purge target resolver did not return the exact requested target set",
            )
        return tuple(by_key[key] for key in requested_keys)

    async def _resolve_post_close_targets(
        self,
        operation_id: str,
        identities: tuple[PurgeTargetIdentity, ...],
    ) -> tuple[RecoveryTarget, ...]:
        resolver = self._post_close_target_resolver
        if resolver is None:
            if self._require_post_close_target_resolver:
                raise OwnerDestructiveError(
                    "PURGE_COORDINATION_FAILED",
                    "post-close purge target resolver is not configured",
                )
            return await self._resolve_targets(identities)
        try:
            resolved_value = await _await_if_needed(resolver(operation_id, identities))
        except RecoveryHold as exc:
            if exc.code in {
                "HOLD_TARGET_DRIFT_NO_MUTATION",
                "HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION",
                "HOLD_UNEXPECTED_DERIVED_STATE_NO_MUTATION",
                "HOLD_UNATTRIBUTABLE_VECTOR_NO_MUTATION",
            }:
                raise OwnerDestructiveError(
                    "PURGE_PREVIEW_STALE",
                    "purge target state changed after preview; review the exact target again",
                ) from exc
            raise OwnerDestructiveError(
                "PURGE_COORDINATION_FAILED",
                "exclusive post-close purge target resolution could not be proven",
            ) from exc
        return self._normalize_resolved_targets(identities, resolved_value)

    @staticmethod
    def _preview_payload(operation_id: str, targets: Iterable[PurgeTargetIdentity]) -> dict[str, Any]:
        return {
            "kind": _PURGE_KIND,
            "operation_id": operation_id,
            "targets": [target.to_mapping() for target in targets],
            "backup_retention_warning": BACKUP_RETENTION_WARNING,
        }

    @staticmethod
    def _target_state_digest(targets: Sequence[RecoveryTarget]) -> str:
        return _digest([_target_state_projection(target) for target in targets])

    async def preview(
        self,
        *,
        operation_id: str,
        targets: Sequence[PurgeTargetIdentity],
    ) -> PurgePreview:
        operation_id = _canonical_operation_id(operation_id)
        if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes, bytearray)) or not targets:
            raise OwnerDestructiveError("PURGE_TARGET_INVALID", "at least one exact purge target is required")
        identities = tuple(targets)
        if not all(isinstance(target, PurgeTargetIdentity) for target in identities):
            raise OwnerDestructiveError("PURGE_TARGET_INVALID", "purge targets must be exact PurgeTargetIdentity values")
        if len({_identity_key(target) for target in identities}) != len(identities):
            raise OwnerDestructiveError("PURGE_TARGET_INVALID", "purge target selection contains duplicates")
        resolved = await self._resolve_targets(identities)
        payload = self._preview_payload(operation_id, identities)
        preview_digest = _digest(payload)
        authorization_preview = _canonical_json({**payload, "preview_digest": preview_digest})
        return PurgePreview(
            operation_id=operation_id,
            targets=identities,
            backup_retention_warning=BACKUP_RETENTION_WARNING,
            preview_digest=preview_digest,
            typed_confirmation=f"PURGE {preview_digest}",
            authorization_preview=authorization_preview,
            _target_state_digest=self._target_state_digest(resolved),
        )

    def _paths(self, operation_id: str) -> tuple[Path, Path, Path]:
        return (
            self._state_root / f"{operation_id}.recovery.receipt.json",
            self._state_root / f"{operation_id}.recovery.state.json",
            self._state_root / f"{operation_id}.purge-result.json",
        )

    def _journal_path(self, operation_id: str) -> Path:
        return self._state_root / f"{operation_id}.purge-operation.json"

    def _read_journal(self, operation_id: str) -> dict[str, Any] | None:
        operation_id = _canonical_operation_id(operation_id)
        path = self._journal_path(operation_id)
        if not path.exists():
            return None
        payload = self._read_json_file(path, code="PURGE_RECOVERY_STATE_MISMATCH")
        allowed = {
            "version",
            "operation_id",
            "preview_digest",
            "target_state_digest",
            "phase",
            "prepared",
            "targets",
            "new_generation",
            "service_initial",
            "activation_device",
            "activation_inode",
            "recovery_result",
            "ready_instance_ids",
            "resume_failures",
            "maintenance_participant_failures",
        }
        required = {
            "version",
            "operation_id",
            "preview_digest",
            "target_state_digest",
            "phase",
            "prepared",
            "targets",
            "new_generation",
            "service_initial",
        }
        if not required.issubset(payload) or set(payload) - allowed or payload.get("version") != _JOURNAL_VERSION:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge operation journal is malformed")
        if payload.get("operation_id") != operation_id:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge operation identity is malformed")
        for field in ("preview_digest", "target_state_digest"):
            value = payload.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge digest is malformed")
        phase = payload.get("phase")
        if phase not in {
            "PREPARED",
            "PREMUTATION_RECOVERY_REQUIRED",
            "RECOVERY_REQUIRED",
            "ACTIVATION_READY",
            "VERIFIED",
        }:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge operation phase is unsupported")
        new_generation = payload.get("new_generation")
        if isinstance(new_generation, bool) or not isinstance(new_generation, int) or new_generation < 0:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge generation is malformed")
        service_initial = payload.get("service_initial")
        if service_initial is not None:
            if not isinstance(service_initial, dict) or set(service_initial) != {"status", "intent_revision", "stopped_boot_id"}:
                raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge service state is malformed")
            status = service_initial.get("status")
            intent_revision = service_initial.get("intent_revision")
            stopped_boot_id = service_initial.get("stopped_boot_id")
            if (
                not isinstance(status, str)
                or not status
                or isinstance(intent_revision, bool)
                or not isinstance(intent_revision, int)
                or intent_revision < 0
                or (stopped_boot_id is not None and not isinstance(stopped_boot_id, str))
            ):
                raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge service state is malformed")
        prepared, targets = self._journal_runtime_state(payload)
        if prepared.operation_id != operation_id or new_generation != prepared.original_generation + 1:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge maintenance identity is malformed")
        if not 1 <= len(targets) <= 50:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge target count is out of bounds")
        identities = tuple(_identity_from_target(target) for target in targets)
        if len({_identity_key(identity) for identity in identities}) != len(identities):
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge targets contain duplicates")
        activation_device = payload.get("activation_device")
        activation_inode = payload.get("activation_inode")
        if (activation_device is None) != (activation_inode is None):
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge activation identity is incomplete")
        for value in (activation_device, activation_inode):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge activation identity is malformed")
        if payload.get("recovery_result") is not None:
            result = self._journal_recovery_result(payload)
            if result.operation_id != operation_id or result.target_count != len(targets):
                raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge recovery result is mismatched")
            if activation_device is None:
                raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge activation identity is incomplete")
        if phase in {"ACTIVATION_READY", "VERIFIED"} and payload.get("recovery_result") is None:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge activation result is missing")
        if phase == "PREMUTATION_RECOVERY_REQUIRED" and (
            activation_device is not None
            or activation_inode is not None
            or payload.get("recovery_result") is not None
        ):
            raise OwnerDestructiveError(
                "PURGE_RECOVERY_STATE_MISMATCH",
                "pre-mutation purge recovery state cannot carry post-mutation activation evidence",
            )
        observation_fields = {
            "ready_instance_ids",
            "resume_failures",
            "maintenance_participant_failures",
        }
        present_observation_fields = observation_fields.intersection(payload)
        if present_observation_fields and present_observation_fields != observation_fields:
            raise OwnerDestructiveError(
                "PURGE_RECOVERY_STATE_MISMATCH",
                "persisted purge resume observation is incomplete",
            )
        if present_observation_fields:
            if phase not in {"ACTIVATION_READY", "VERIFIED"}:
                raise OwnerDestructiveError(
                    "PURGE_RECOVERY_STATE_MISMATCH",
                    "persisted purge resume observation is in an invalid phase",
                )
            _bounded_ready_instance_ids(payload["ready_instance_ids"])
            _bounded_string_mapping(
                payload["resume_failures"],
                field="runtime resume failures",
                max_entries=64,
                max_bytes=8192,
            )
            _bounded_string_mapping(
                payload["maintenance_participant_failures"],
                field="maintenance participant failures",
                max_entries=_MAX_MAINTENANCE_PARTICIPANT_FAILURES,
                max_bytes=_MAX_MAINTENANCE_PARTICIPANT_FAILURE_BYTES,
                max_key_bytes=64,
            )
        return payload

    def _load_journal(self, preview: PurgePreview) -> dict[str, Any] | None:
        payload = self._read_journal(preview.operation_id)
        if payload is None:
            return None
        if payload.get("preview_digest") != preview.preview_digest:
            raise OwnerDestructiveError(
                "PURGE_OPERATION_BINDING_MISMATCH",
                "purge operation identity is already bound to another exact preview",
            )
        if payload.get("target_state_digest") != preview._target_state_digest:
            raise OwnerDestructiveError(
                "PURGE_RECOVERY_STATE_MISMATCH",
                "persisted purge target state does not match the exact preview",
            )
        return payload

    def recoverable_preview(self, operation_id: str) -> PurgePreview | None:
        """Reconstruct the exact server preview from durable purge recovery state only."""

        operation_id = _canonical_operation_id(operation_id)
        journal = self._read_journal(operation_id)
        if journal is None:
            return None
        _prepared, targets = self._journal_runtime_state(journal)
        target_state_digest = self._target_state_digest(targets)
        persisted_state_digest = journal["target_state_digest"]
        if not hmac.compare_digest(target_state_digest, persisted_state_digest):
            raise OwnerDestructiveError(
                "PURGE_RECOVERY_STATE_MISMATCH",
                "persisted purge target state digest does not match durable target state",
            )
        identities = tuple(_identity_from_target(target) for target in targets)
        payload = self._preview_payload(operation_id, identities)
        preview_digest = _digest(payload)
        persisted_preview_digest = journal["preview_digest"]
        if not hmac.compare_digest(preview_digest, persisted_preview_digest):
            raise OwnerDestructiveError(
                "PURGE_RECOVERY_STATE_MISMATCH",
                "persisted purge preview digest does not match durable target identity",
            )
        preview = PurgePreview(
            operation_id=operation_id,
            targets=identities,
            backup_retention_warning=BACKUP_RETENTION_WARNING,
            preview_digest=preview_digest,
            typed_confirmation=f"PURGE {preview_digest}",
            authorization_preview=_canonical_json({**payload, "preview_digest": preview_digest}),
            _target_state_digest=target_state_digest,
        )
        self._validate_preview_integrity(preview)
        return preview

    def _write_journal(
        self,
        preview: PurgePreview,
        *,
        phase: str,
        prepared: PreparedMaintenance | None = None,
        targets: Sequence[RecoveryTarget] | None = None,
        service_initial: Mapping[str, Any] | None = None,
        activation_device: int | None = None,
        activation_inode: int | None = None,
        recovery_result: RecoveryResult | None = None,
        ready_instance_ids: Sequence[str] | None = None,
        resume_failures: Mapping[str, str] | None = None,
        maintenance_participant_failures: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        existing = self._load_journal(preview)
        if existing is None:
            if prepared is None or targets is None:
                raise OwnerDestructiveError(
                    "PURGE_RECOVERY_STATE_MISMATCH",
                    "initial purge journal requires exact prepared maintenance and targets",
                )
            payload: dict[str, Any] = {
                "version": _JOURNAL_VERSION,
                "operation_id": preview.operation_id,
                "preview_digest": preview.preview_digest,
                "target_state_digest": preview._target_state_digest,
                "phase": phase,
                "prepared": _prepared_to_mapping(prepared),
                "targets": [_target_to_mapping(target) for target in targets],
                "new_generation": prepared.original_generation + 1,
                "service_initial": dict(service_initial) if service_initial is not None else None,
            }
        else:
            payload = dict(existing)
            payload["phase"] = phase
        if activation_device is not None:
            payload["activation_device"] = activation_device
        if activation_inode is not None:
            payload["activation_inode"] = activation_inode
        if recovery_result is not None:
            payload["recovery_result"] = {
                "operation_id": recovery_result.operation_id,
                "status": recovery_result.state,
                "receipt_path": recovery_result.receipt_path,
                "state_path": recovery_result.state_path,
                "target_count": recovery_result.target_count,
            }
        observation_values = (ready_instance_ids, resume_failures, maintenance_participant_failures)
        if any(value is not None for value in observation_values):
            if any(value is None for value in observation_values):
                raise OwnerDestructiveError(
                    "PURGE_RECOVERY_STATE_MISMATCH",
                    "purge resume observation must be persisted atomically",
                )
            payload["ready_instance_ids"] = list(_bounded_ready_instance_ids(ready_instance_ids))
            payload["resume_failures"] = _bounded_string_mapping(
                resume_failures,
                field="runtime resume failures",
                max_entries=64,
                max_bytes=8192,
            )
            payload["maintenance_participant_failures"] = _bounded_string_mapping(
                maintenance_participant_failures,
                field="maintenance participant failures",
                max_entries=_MAX_MAINTENANCE_PARTICIPANT_FAILURES,
                max_bytes=_MAX_MAINTENANCE_PARTICIPANT_FAILURE_BYTES,
                max_key_bytes=64,
            )
        _atomic_json(self._journal_path(preview.operation_id), payload)
        return payload

    @staticmethod
    def _journal_runtime_state(journal: Mapping[str, Any]) -> tuple[PreparedMaintenance, tuple[RecoveryTarget, ...]]:
        prepared_raw = journal.get("prepared")
        targets_raw = journal.get("targets")
        if not isinstance(prepared_raw, dict) or not isinstance(targets_raw, list) or not targets_raw:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge runtime state is incomplete")
        if not all(isinstance(item, dict) for item in targets_raw):
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "persisted purge targets are malformed")
        prepared = _prepared_from_mapping(prepared_raw)
        targets = tuple(_target_from_mapping(item) for item in targets_raw)
        return prepared, targets

    @staticmethod
    def _journal_recovery_result(journal: Mapping[str, Any]) -> RecoveryResult:
        result = journal.get("recovery_result")
        if not isinstance(result, dict) or set(result) != {
            "operation_id",
            "status",
            "receipt_path",
            "state_path",
            "target_count",
        }:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "activation-ready purge receipt is incomplete")
        target_count = result["target_count"]
        if isinstance(target_count, bool) or not isinstance(target_count, int) or target_count < 1:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "activation-ready target count is invalid")
        if result["status"] != _VERIFIED_STATE:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "activation-ready recovery status is invalid")
        return RecoveryResult(
            operation_id=str(result["operation_id"]),
            state=str(result["status"]),
            logical_purge_committed=True,
            promoted=True,
            receipt_path=str(result["receipt_path"]),
            state_path=str(result["state_path"]),
            target_count=target_count,
        )

    def _running_activation_result(self, preview: PurgePreview) -> RecoveryResult | None:
        journal = self._load_journal(preview)
        if journal is None or journal.get("phase") != "ACTIVATION_READY":
            return None
        prepared, targets = self._journal_runtime_state(journal)
        new_generation = journal.get("new_generation")
        device = journal.get("activation_device")
        inode = journal.get("activation_inode")
        if (
            prepared.operation_id != preview.operation_id
            or self._target_state_digest(targets) != preview._target_state_digest
            or isinstance(new_generation, bool)
            or not isinstance(new_generation, int)
            or isinstance(device, bool)
            or not isinstance(device, int)
            or isinstance(inode, bool)
            or not isinstance(inode, int)
        ):
            return None
        try:
            snapshot = self._coordinator.activation_state(
                preview.operation_id,
                new_generation=new_generation,
                device=device,
                inode=inode,
            )
        except Exception:
            return None
        if getattr(snapshot, "state", None) != "RUNNING":
            return None
        result = self._journal_recovery_result(journal)
        if not Path(result.receipt_path).is_file() or not Path(result.state_path).is_file():
            return None
        return result

    def _validate_preview_integrity(self, preview: PurgePreview) -> None:
        payload = self._preview_payload(preview.operation_id, preview.targets)
        expected_digest = _digest(payload)
        expected_authorization_preview = _canonical_json({**payload, "preview_digest": expected_digest})
        if (
            preview.backup_retention_warning != BACKUP_RETENTION_WARNING
            or not hmac.compare_digest(preview.preview_digest, expected_digest)
            or not hmac.compare_digest(preview.typed_confirmation, f"PURGE {expected_digest}")
            or not hmac.compare_digest(preview.authorization_preview, expected_authorization_preview)
        ):
            raise OwnerDestructiveError(
                "PURGE_PREVIEW_INVALID",
                "purge preview integrity check failed; generate a fresh exact preview",
            )

    @staticmethod
    def _read_json_file(path: Path, *, code: str) -> dict[str, Any]:
        try:
            info = path.lstat()
            if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OwnerDestructiveError(code, "purge state file is not a trusted owner-private regular file")
            value = json.loads(path.read_text(encoding="utf-8"))
        except OwnerDestructiveError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OwnerDestructiveError(code, "purge state file could not be read safely") from exc
        if not isinstance(value, dict):
            raise OwnerDestructiveError(code, "purge state file is malformed")
        return value

    def _load_completed(self, preview: PurgePreview) -> PurgeSuccess | None:
        _, _, result_path = self._paths(preview.operation_id)
        if not result_path.exists():
            return None
        payload = self._read_json_file(result_path, code="PURGE_RESULT_INVALID")
        required = {
            "version",
            "operation_id",
            "preview_digest",
            "status",
            "receipt_path",
            "state_path",
            "target_count",
            "ready_instance_ids",
            "resume_failures",
        }
        if set(payload) != required or payload.get("version") != _RESULT_VERSION:
            raise OwnerDestructiveError("PURGE_RESULT_INVALID", "persisted purge result has an unsupported shape")
        if payload.get("operation_id") != preview.operation_id or payload.get("preview_digest") != preview.preview_digest:
            raise OwnerDestructiveError(
                "PURGE_OPERATION_BINDING_MISMATCH",
                "persisted purge result belongs to another exact preview",
            )
        if payload.get("status") != _VERIFIED_STATE:
            raise OwnerDestructiveError("PURGE_RESULT_INVALID", "persisted purge result is not final verified state")
        ready = payload.get("ready_instance_ids")
        failures = payload.get("resume_failures")
        target_count = payload.get("target_count")
        if (
            not isinstance(ready, list)
            or not all(isinstance(item, str) for item in ready)
            or not isinstance(failures, dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in failures.items())
            or isinstance(target_count, bool)
            or not isinstance(target_count, int)
            or target_count < 1
        ):
            raise OwnerDestructiveError("PURGE_RESULT_INVALID", "persisted purge result fields are malformed")
        receipt_path = payload.get("receipt_path")
        state_path = payload.get("state_path")
        if not isinstance(receipt_path, str) or not isinstance(state_path, str):
            raise OwnerDestructiveError("PURGE_RESULT_INVALID", "persisted purge result paths are malformed")
        if not Path(receipt_path).is_file() or not Path(state_path).is_file():
            raise OwnerDestructiveError("PURGE_RESULT_INVALID", "persisted purge result is missing recovery evidence")
        return PurgeSuccess(
            operation_id=preview.operation_id,
            preview_digest=preview.preview_digest,
            status=_VERIFIED_STATE,
            receipt_path=receipt_path,
            state_path=state_path,
            target_count=target_count,
            ready_instance_ids=tuple(ready),
            resume_failures=dict(failures),
        )

    def _persist_completed(self, result: PurgeSuccess) -> None:
        _, _, result_path = self._paths(result.operation_id)
        _atomic_json(result_path, {"version": _RESULT_VERSION, **result.to_mapping()})

    def _discard_journal(self, operation_id: str) -> None:
        path = self._journal_path(operation_id)
        if path.exists() and not path.is_symlink():
            path.unlink()

    @staticmethod
    def _recovery_state_is_postcommit(state_path: Path) -> bool:
        if not state_path.exists():
            return False
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and payload.get("phase") in {_SEALING_INCOMPLETE, _ACTIVATION_READY, _VERIFIED_STATE}

    async def _abort_precommit(self, prepared: Any, preview: PurgePreview) -> None:
        journal = self._load_journal(preview)
        if journal is not None:
            try:
                self._write_journal(preview, phase="PREMUTATION_RECOVERY_REQUIRED")
            except Exception as persist_exc:
                await _await_if_needed(self._service_state.mark_recovery_required())
                raise OwnerDestructiveError(
                    "PURGE_RECOVERY_STATE_MISMATCH",
                    "pre-mutation purge recovery state could not be persisted safely",
                    recovery_required=True,
                ) from persist_exc
        try:
            await self._coordinator.abort_pre_mutation(prepared)
        except Exception as exc:
            if getattr(exc, "code", None) != "MAINTENANCE_RECOVERY_REQUIRED":
                raise
            await _await_if_needed(self._service_state.mark_recovery_required())
            raise OwnerDestructiveError(
                "PURGE_RECOVERY_REQUIRED",
                "pre-mutation maintenance abort reopened stdio but Owner participant recovery is still required",
                recovery_required=True,
            ) from exc
        await _await_if_needed(self._service_state.finish_maintenance(preview.operation_id))

    async def _recover_aborted_pre_mutation(self, preview: PurgePreview, journal: Mapping[str, Any]) -> None:
        prepared, _targets = self._journal_runtime_state(journal)
        try:
            snapshot = await _await_if_needed(self._coordinator.recover_aborted_pre_mutation(prepared))
            if (
                getattr(snapshot, "state", None) != "RUNNING"
                or getattr(snapshot, "operation_id", None) != prepared.operation_id
                or getattr(snapshot, "generation", None) != prepared.original_generation
                or getattr(snapshot, "original_generation", None) != prepared.original_generation
                or getattr(snapshot, "intent_revision", None) != prepared.intent_revision
            ):
                raise OwnerDestructiveError(
                    "PURGE_RECOVERY_STATE_MISMATCH",
                    "pre-mutation purge recovery did not return the exact aborted RUNNING maintenance identity",
                )
            await self._finish_recovery_service(journal)
        except Exception as exc:
            await _await_if_needed(self._service_state.mark_recovery_required())
            raise OwnerDestructiveError(
                "PURGE_RECOVERY_REQUIRED",
                "pre-mutation purge recovery is still incomplete; destructive work remains blocked",
                recovery_required=True,
            ) from exc
        self._discard_journal(preview.operation_id)
        raise OwnerDestructiveError(
            "PURGE_REPREVIEW_REQUIRED",
            "pre-mutation maintenance recovery completed; generate a fresh exact purge preview before another attempt",
        )

    async def _mark_recovery_required(self, operation_id: str) -> None:
        await _await_if_needed(self._coordinator.mark_recovery_required(operation_id))
        await _await_if_needed(self._service_state.mark_recovery_required())

    async def _finish_recovery_service(self, journal: Mapping[str, Any]) -> None:
        snapshotter = getattr(self._service_state, "snapshot", None)
        current = snapshotter() if callable(snapshotter) else None
        initial = journal.get("service_initial")
        initial_status = initial.get("status") if isinstance(initial, dict) else None
        initial_revision = initial.get("intent_revision") if isinstance(initial, dict) else None
        current_revision = getattr(current, "intent_revision", None)
        current_stopped = getattr(current, "stopped_boot_id", None)
        newer_stop = (
            isinstance(initial_revision, int)
            and isinstance(current_revision, int)
            and current_revision > initial_revision
            and current_stopped is not None
        )
        if newer_stop or initial_status == "STOPPED_UNTIL_REBOOT":
            clear = getattr(self._service_state, "clear_recovery_required", None)
            if callable(clear):
                await _await_if_needed(clear())
            return
        if initial_status == "FAILED":
            mark_failed = getattr(self._service_state, "mark_failed", None)
            if callable(mark_failed):
                await _await_if_needed(mark_failed())
                return
        clear = getattr(self._service_state, "clear_recovery_required", None)
        if callable(clear):
            await _await_if_needed(clear())

    async def _finish_after_running(self, journal: Mapping[str, Any]) -> None:
        snapshotter = getattr(self._service_state, "snapshot", None)
        current = snapshotter() if callable(snapshotter) else None
        status = getattr(getattr(current, "status", None), "value", getattr(current, "status", None))
        if status == "RECOVERY_REQUIRED" or journal.get("phase") == "RECOVERY_REQUIRED":
            await self._finish_recovery_service(journal)
            return
        if status == "MAINTENANCE" or journal.get("phase") == "PREPARED":
            try:
                await _await_if_needed(self._service_state.finish_maintenance(str(journal["operation_id"])))
            except Exception:
                if status == "MAINTENANCE":
                    raise

    async def _resume_running_observation(
        self,
        prepared: PreparedMaintenance,
        *,
        new_generation: int,
    ) -> tuple[tuple[str, ...], dict[str, str], dict[str, str]]:
        try:
            resume = await self._coordinator.resume_running(prepared, new_generation=new_generation)
            ready = _bounded_ready_instance_ids(getattr(resume, "ready_instance_ids", ()))
            failures = _bounded_string_mapping(
                getattr(resume, "failures", {}),
                field="runtime resume failures",
                max_entries=64,
                max_bytes=8192,
            )
            if "owner_adapter" in ready or "owner_adapter" in failures:
                raise OwnerDestructiveError(
                    "PURGE_RECOVERY_STATE_MISMATCH",
                    "maintenance participant identity cannot be recorded as stdio runtime resume evidence",
                )
            try:
                participant_failures = _bounded_string_mapping(
                    getattr(resume, "participant_failures", {}),
                    field="maintenance participant failures",
                    max_entries=_MAX_MAINTENANCE_PARTICIPANT_FAILURES,
                    max_bytes=_MAX_MAINTENANCE_PARTICIPANT_FAILURE_BYTES,
                    max_key_bytes=64,
                )
            except OwnerDestructiveError:
                participant_failures = {"maintenance_coordinator": "PARTICIPANT_REPORT_INVALID"}
            return ready, failures, participant_failures
        except MaintenanceCoordinatorError as exc:
            if exc.code == "RUNTIME_RESUME_TIMEOUT":
                return (), {"coordinator": "RUNTIME_RESUME_TIMEOUT"}, {}
            return (), {"coordinator": "RUNTIME_RESUME_FAILED"}, {}
        except Exception:
            return (), {"coordinator": "RUNTIME_RESUME_FAILED"}, {}

    @staticmethod
    def _runtime_resume_timed_out(failures: Mapping[str, str]) -> bool:
        return dict(failures) == {"coordinator": "RUNTIME_RESUME_TIMEOUT"}

    async def _hold_for_runtime_resume_recovery(self) -> None:
        try:
            await _await_if_needed(self._service_state.mark_recovery_required())
        finally:
            raise OwnerDestructiveError(
                "PURGE_RECOVERY_REQUIRED",
                "runtime resume timed out and requires authenticated recovery before purge finalization",
                recovery_required=True,
            )

    async def _hold_for_participant_recovery(self) -> None:
        try:
            await _await_if_needed(self._service_state.mark_recovery_required())
        finally:
            raise OwnerDestructiveError(
                "PURGE_RECOVERY_REQUIRED",
                "maintenance participant recovery is required before purge finalization",
                recovery_required=True,
            )

    async def execute(
        self,
        preview: PurgePreview,
        *,
        typed_confirmation: str,
        grant_secret: str,
        drain_deadline: float,
        verify_fresh_grant: FreshGrantVerifier | None = None,
    ) -> PurgeSuccess:
        if not isinstance(preview, PurgePreview):
            raise TypeError("preview must be a PurgePreview")
        _canonical_operation_id(preview.operation_id)
        self._validate_preview_integrity(preview)
        completed = self._load_completed(preview)
        if completed is not None:
            return completed
        if isinstance(drain_deadline, bool) or not isinstance(drain_deadline, (int, float)) or drain_deadline <= 0:
            raise OwnerDestructiveError("PURGE_DRAIN_DEADLINE_INVALID", "drain_deadline must be a positive absolute timestamp")

        receipt_path, recovery_state_path, _ = self._paths(preview.operation_id)
        journal = self._load_journal(preview)

        if journal is not None and journal.get("phase") == "PREMUTATION_RECOVERY_REQUIRED":
            await self._recover_aborted_pre_mutation(preview, journal)

        if journal is not None and journal.get("phase") == "ACTIVATION_READY":
            prepared, persisted_targets = self._journal_runtime_state(journal)
            new_generation = journal.get("new_generation")
            device = journal.get("activation_device")
            inode = journal.get("activation_inode")
            if (
                prepared.operation_id != preview.operation_id
                or isinstance(new_generation, bool)
                or not isinstance(new_generation, int)
                or isinstance(device, bool)
                or not isinstance(device, int)
                or isinstance(inode, bool)
                or not isinstance(inode, int)
                or self._target_state_digest(persisted_targets) != preview._target_state_digest
            ):
                raise OwnerDestructiveError(
                    "PURGE_RECOVERY_STATE_MISMATCH",
                    "activation-ready purge state does not match the exact operation",
                )
            snapshot = self._coordinator.activation_state(
                preview.operation_id,
                new_generation=new_generation,
                device=device,
                inode=inode,
            )
            if getattr(snapshot, "state", None) == "RUNNING":
                recovery_result = self._journal_recovery_result(journal)
                if not Path(recovery_result.receipt_path).is_file() or not Path(recovery_result.state_path).is_file():
                    raise OwnerDestructiveError(
                        "PURGE_RECOVERY_STATE_MISMATCH",
                        "activation-ready purge evidence is missing",
                    )
                had_participant_recovery = bool(journal.get("maintenance_participant_failures"))
                had_runtime_resume_recovery = self._runtime_resume_timed_out(journal.get("resume_failures", {}))
                ready, failures, participant_failures = await self._resume_running_observation(
                    prepared,
                    new_generation=new_generation,
                )
                journal = self._write_journal(
                    preview,
                    phase="ACTIVATION_READY",
                    ready_instance_ids=ready,
                    resume_failures=failures,
                    maintenance_participant_failures=participant_failures,
                )
                if self._runtime_resume_timed_out(failures):
                    await self._hold_for_runtime_resume_recovery()
                if participant_failures:
                    await self._hold_for_participant_recovery()
                if had_participant_recovery or had_runtime_resume_recovery:
                    await self._finish_recovery_service(journal)
                else:
                    await self._finish_after_running(journal)
                result = PurgeSuccess(
                    operation_id=preview.operation_id,
                    preview_digest=preview.preview_digest,
                    status=_VERIFIED_STATE,
                    receipt_path=recovery_result.receipt_path,
                    state_path=recovery_result.state_path,
                    target_count=recovery_result.target_count,
                    ready_instance_ids=ready,
                    resume_failures=failures,
                )
                self._persist_completed(result)
                self._write_journal(preview, phase="VERIFIED")
                return result

        if not isinstance(typed_confirmation, str) or not hmac.compare_digest(
            typed_confirmation,
            preview.typed_confirmation,
        ):
            raise OwnerDestructiveError(
                "PURGE_CONFIRMATION_MISMATCH",
                "typed purge confirmation does not match the exact preview digest",
            )
        if not isinstance(grant_secret, str) or not grant_secret:
            raise OwnerDestructiveError("PURGE_GRANT_REQUIRED", "a fresh Owner sensitive-action grant is required")
        verifier = verify_fresh_grant if verify_fresh_grant is not None else self._verify_fresh_grant
        if not callable(verifier):
            raise OwnerDestructiveError(
                "PURGE_AUTHORIZATION_NOT_CONFIGURED",
                "purge authorization verifier is not configured for this request",
            )

        service_initial: dict[str, Any] | None = None
        if journal is None:
            service_initial = _service_snapshot_mapping(self._service_state)
            await _await_if_needed(self._service_state.begin_maintenance(preview.operation_id))
            try:
                prepared = await self._coordinator.prepare_close(
                    preview.operation_id,
                    drain_deadline=float(drain_deadline),
                )
            except BaseException as exc:
                if getattr(exc, "code", None) == "MAINTENANCE_RECOVERY_REQUIRED":
                    await _await_if_needed(self._service_state.mark_recovery_required())
                    raise OwnerDestructiveError(
                        "PURGE_RECOVERY_REQUIRED",
                        "purge maintenance preparation requires recovery before any destructive mutation",
                        recovery_required=True,
                    ) from exc
                await _await_if_needed(self._service_state.finish_maintenance(preview.operation_id))
                raise
            if prepared.operation_id != preview.operation_id:
                await self._abort_precommit(prepared, preview)
                raise OwnerDestructiveError(
                    "PURGE_COORDINATION_FAILED",
                    "maintenance coordinator returned another operation identity",
                )
            try:
                resolved = await self._resolve_post_close_targets(preview.operation_id, preview.targets)
                if not hmac.compare_digest(
                    self._target_state_digest(resolved),
                    preview._target_state_digest,
                ):
                    raise OwnerDestructiveError(
                        "PURGE_PREVIEW_STALE",
                        "purge target state changed after preview; review the exact target again",
                    )
                journal = self._write_journal(
                    preview,
                    phase="PREPARED",
                    prepared=prepared,
                    targets=resolved,
                    service_initial=service_initial,
                )
            except BaseException:
                await self._abort_precommit(prepared, preview)
                self._discard_journal(preview.operation_id)
                raise
        else:
            prepared, resolved = self._journal_runtime_state(journal)
            if prepared.operation_id != preview.operation_id:
                raise OwnerDestructiveError(
                    "PURGE_RECOVERY_STATE_MISMATCH",
                    "persisted prepared maintenance belongs to another operation",
                )
            if self._target_state_digest(resolved) != preview._target_state_digest:
                raise OwnerDestructiveError(
                    "PURGE_RECOVERY_STATE_MISMATCH",
                    "persisted purge targets do not match the exact preview state",
                )
            if tuple(_identity_from_target(target) for target in resolved) != preview.targets:
                raise OwnerDestructiveError(
                    "PURGE_RECOVERY_STATE_MISMATCH",
                    "persisted purge target identities do not match the exact preview",
                )

        prior_phase = str(journal["phase"])
        recovery_resume = prior_phase == "RECOVERY_REQUIRED"
        operator_started = False
        try:
            await _await_if_needed(
                verifier(
                    grant_secret=grant_secret,
                    preview=preview.authorization_preview,
                )
            )
            plan = RecoveryPlan(
                operation_id=preview.operation_id,
                db_path=self._db_path,
                profile=self._profile,
                targets=resolved,
                incident_class="S1",
                propagation_state="EXPLICIT_TARGET_SET_COMPLETE",
                retained_identity_fields_classified_non_sensitive=True,
                safe_ids_for_receipt=False,
            )

            async def activation_ready(result: RecoveryResult, _maintenance_lease: Any) -> None:
                if recovery_resume:
                    self._coordinator.rearm_recovery(prepared)
                activation = self._coordinator.begin_activation(
                    prepared,
                    new_generation=prepared.original_generation + 1,
                )
                generation = getattr(activation, "generation", None)
                device = getattr(activation, "device", None)
                inode = getattr(activation, "inode", None)
                if (
                    generation != prepared.original_generation + 1
                    or isinstance(device, bool)
                    or not isinstance(device, int)
                    or isinstance(inode, bool)
                    or not isinstance(inode, int)
                ):
                    raise OwnerDestructiveError(
                        "PURGE_ACTIVATION_FAILED",
                        "activation barrier does not match the exact recovered database identity",
                        recovery_required=True,
                    )
                self._write_journal(
                    preview,
                    phase="ACTIVATION_READY",
                    activation_device=device,
                    activation_inode=inode,
                    recovery_result=result,
                )

            async def publish_running(_result: RecoveryResult, _maintenance_lease: Any) -> None:
                active_journal = self._load_journal(preview)
                if active_journal is None:
                    raise OwnerDestructiveError(
                        "PURGE_RECOVERY_STATE_MISMATCH",
                        "activation-ready purge journal disappeared before RUNNING publication",
                    )
                new_generation = int(active_journal["new_generation"])
                device = active_journal.get("activation_device")
                inode = active_journal.get("activation_inode")
                try:
                    self._coordinator.publish_running(prepared, new_generation=new_generation)
                except BaseException as exc:
                    try:
                        published = self._coordinator.activation_state(
                            preview.operation_id,
                            new_generation=new_generation,
                            device=device,
                            inode=inode,
                        )
                    except BaseException:
                        raise exc
                    if getattr(published, "state", None) != "RUNNING":
                        raise exc

            hooks = RecoveryActivationHooks(
                operation_id=preview.operation_id,
                activation_ready=activation_ready,
                publish_running=publish_running,
            )
            operator_started = True
            recovery_result = await self._recovery_operator.execute(
                plan,
                receipt_path=str(receipt_path),
                state_path=str(recovery_state_path),
                activation_hooks=hooks,
            )
        except BaseException as exc:
            postcommit = (
                isinstance(exc, RecoveryHold) and exc.code == _SEALING_INCOMPLETE
            ) or (operator_started and self._recovery_state_is_postcommit(recovery_state_path))
            if postcommit:
                running_result = self._running_activation_result(preview)
                if running_result is not None:
                    recovery_result = running_result
                else:
                    self._write_journal(preview, phase="RECOVERY_REQUIRED")
                    await self._mark_recovery_required(preview.operation_id)
                    raise OwnerDestructiveError(
                        "PURGE_RECOVERY_REQUIRED",
                        "logical purge committed but sealing or verification is incomplete; normal access remains blocked",
                        recovery_required=True,
                    ) from exc
            else:
                if prior_phase == "PREPARED":
                    await self._abort_precommit(prepared, preview)
                    self._discard_journal(preview.operation_id)
                raise

        if not isinstance(recovery_result, RecoveryResult) or (
            recovery_result.operation_id != preview.operation_id
            or recovery_result.state != _VERIFIED_STATE
            or recovery_result.logical_purge_committed is not True
            or recovery_result.promoted is not True
            or recovery_result.target_count != len(resolved)
        ):
            self._write_journal(preview, phase="RECOVERY_REQUIRED")
            await self._mark_recovery_required(preview.operation_id)
            raise OwnerDestructiveError(
                "PURGE_RECOVERY_REQUIRED",
                "purge operator did not return a final verified receipt; normal access remains blocked",
                recovery_required=True,
            )

        ready, failures, participant_failures = await self._resume_running_observation(
            prepared,
            new_generation=prepared.original_generation + 1,
        )
        active_journal = self._load_journal(preview)
        if active_journal is None or active_journal.get("phase") != "ACTIVATION_READY":
            raise OwnerDestructiveError(
                "PURGE_RECOVERY_STATE_MISMATCH",
                "verified purge is missing durable activation-ready state",
                recovery_required=True,
            )
        active_journal = self._write_journal(
            preview,
            phase="ACTIVATION_READY",
            ready_instance_ids=ready,
            resume_failures=failures,
            maintenance_participant_failures=participant_failures,
        )
        if self._runtime_resume_timed_out(failures):
            await self._hold_for_runtime_resume_recovery()
        if participant_failures:
            await self._hold_for_participant_recovery()
        if recovery_resume:
            await self._finish_recovery_service(active_journal)
        else:
            await _await_if_needed(self._service_state.finish_maintenance(preview.operation_id))
        result = PurgeSuccess(
            operation_id=preview.operation_id,
            preview_digest=preview.preview_digest,
            status=_VERIFIED_STATE,
            receipt_path=str(receipt_path),
            state_path=str(recovery_state_path),
            target_count=len(resolved),
            ready_instance_ids=ready,
            resume_failures=failures,
        )
        self._persist_completed(result)
        self._write_journal(preview, phase="VERIFIED")
        return result
