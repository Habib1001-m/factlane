from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from importlib import metadata as importlib_metadata
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import quote, urlsplit

from .adapter import (
    MODEL_DIGESTS,
    PROFILE_DEFINITIONS,
    MemoryAdapter,
    TrustedWriteContext,
    trusted_write_context_for_profile,
)
from .contract import AdapterError, ScopeContext, validate_scope
from .embeddings import EmbeddingProfile
from .embedding_compatibility import parse_embedding_profile_metadata
from .maintenance_coordinator import (
    MaintenanceBarrierStore,
    MaintenanceCoordinator,
    MaintenanceCoordinatorError,
    ResumeReport,
    initialize_barrier_for_startup,
)
from .owner_auth import NativeConfirmation, NativeConfirmationRequest, OwnerAuthState
from .owner_control import OwnerAdapterMaintenanceGate, OwnerControlService
from .owner_database import (
    ArtifactRegistry,
    apply_logical_import_to_adapter,
    create_consistent_backup,
    probe_logical_import_destination,
)
from .owner_database_service import OwnerDatabaseService
from .owner_destructive import OwnerDestructiveOrchestrator, PurgeTargetIdentity
from .owner_http import OwnerHTTPServer, OwnerSetupError, WindowsOwnerHTTPServer
from .owner_native_control import OwnerNativeControlServer, WindowsOwnerNativeControlServer, ZenityNativeConfirmation
from .owner_operations import OwnerOperationsService
from .owner_overview import OwnerOverviewService
from .owner_restore import FullRestoreService, RestoreRecoveryRequired
from .recovery import (
    PM0,
    PM1,
    MaintenanceLease,
    RecoveryScope,
    RecoveryTarget,
    SensitiveMemoryRecoveryOperator,
    resolve_permanent_purge_targets_exclusive,
)
from .runtime_control import (
    PROTOCOL_VERSION,
    RuntimeControlClient,
    RuntimeControlRequest,
    RuntimeIdentity,
    RuntimeRegistry,
    WindowsRuntimeControlClient,
)
from .storage import (
    LOGICAL_EXPORT_LIFECYCLE_STATES,
    STORAGE_CONTRACT_VERSION,
    SQLiteVecEngine,
    inspect_storage_contract_version,
)
from .webui_runtime import (
    BackendLaunchSpec,
    BackendMode,
    BackendTransportKind,
    PlatformKind,
    RuntimeConfigError,
    WebUIRuntimeConfig,
    caddy_command,
    default_runtime_config_path,
    load_runtime_config,
)
from .webui_install import (
    WEBUI_SERVICE_NAME,
    ManagedServiceActivationStatus,
    _atomic_owner_file_write,
    probe_linux_managed_https,
)
from .webui_service import ServiceSnapshot, ServiceStatus, WebUIServiceState
from .webui_windows import (
    WindowsManagedConfig,
    WindowsWebUIError,
    load_windows_managed_config,
    prepare_windows_managed_restart,
    render_windows_managed_config,
    validate_windows_database_binding_path,
    windows_boot_id,
    windows_native_confirm,
    windows_owner_paths,
    write_owner_private_text_atomic,
)
from .windows_maintenance import WindowsInPlaceMaintenanceLease
from .windows_owner_purge import WindowsOwnerPurgeOperator, resolve_windows_permanent_purge_targets_exclusive
from .windows_owner_restore import WindowsOwnerRestoreOperator
from .windows_security import WindowsSecurityError, current_user_sid, process_identity as windows_process_identity
from .windows_tls import (
    build_windows_client_ssl_context,
    build_windows_server_ssl_context,
    load_windows_tls_material,
)


AdapterFactory = Callable[..., Awaitable[MemoryAdapter]]
_CADDY_READINESS_CONNECT_TIMEOUT_SECONDS = 0.2


class _WindowsNativeConfirmation:
    """Owner confirmation through the existing Win32 MessageBox safety boundary."""

    def confirm(self, request: NativeConfirmationRequest) -> bool:
        parts = [
            f"Action: {request.kind}",
            f"Verification phrase: {request.verification_phrase}",
        ]
        if request.device_label:
            parts.append(f"Device: {request.device_label}")
        if request.origin:
            parts.append(f"Origin: {request.origin}")
        if request.locator:
            parts.append(f"Request: {request.locator}")
        if request.preview:
            parts.append(f"Preview: {request.preview}")
        message = "\n".join(parts)
        if len(message) > 4096:
            return False
        try:
            return bool(windows_native_confirm(message))
        except Exception:
            return False


@dataclass(frozen=True)
class _OwnerAdapterLaunchConfig:
    database_path: str
    profile: str
    host_id: str
    trusted_write_context: TrustedWriteContext


class _OwnerAdapterMaintenanceParticipant:
    """Close/reopen the in-process Owner adapter without becoming a runtime identity."""

    def __init__(
        self,
        *,
        adapter: MemoryAdapter,
        adapter_factory: AdapterFactory,
        launch: _OwnerAdapterLaunchConfig,
        gate: OwnerAdapterMaintenanceGate,
    ) -> None:
        self._adapter: MemoryAdapter | None = adapter
        self._adapter_factory = adapter_factory
        self._launch = launch
        self._gate = gate
        self._loop: asyncio.AbstractEventLoop | None = None
        self._control: OwnerControlService | None = None
        self._overview: OwnerOverviewService | None = None
        self._operations: OwnerOperationsService | None = None

    @property
    def adapter(self) -> MemoryAdapter | None:
        return self._adapter

    @property
    def state(self) -> str:
        return self._gate.state

    async def initialize(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("Owner adapter participant is bound to another async loop")
        self._loop = loop

    def bind_services(
        self,
        control: OwnerControlService,
        overview: OwnerOverviewService,
        operations: OwnerOperationsService,
    ) -> None:
        if self._control is not None or self._overview is not None or self._operations is not None:
            raise RuntimeError("Owner adapter participant services are already bound")
        if (
            control._maintenance_gate is not self._gate
            or overview._maintenance_gate is not self._gate
            or operations._maintenance_gate is not self._gate
        ):
            raise RuntimeError("Owner services must share the exact maintenance gate")
        if control._adapter is not self._adapter or overview._adapter is not self._adapter:
            raise RuntimeError("Owner services must start on the participant adapter")
        self._control = control
        self._overview = overview
        self._operations = operations

    def _require_loop(self) -> None:
        if self._loop is None or asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Owner adapter maintenance must run on the Owner HTTP async loop")

    async def prepare_close(self, *, drain_deadline: float) -> None:
        self._require_loop()
        remaining = drain_deadline - time.time()
        if remaining <= 0:
            raise MaintenanceCoordinatorError(
                "CLIENT_DRAIN_TIMEOUT",
                "Owner adapter maintenance drain deadline expired",
            )
        try:
            await asyncio.wait_for(self._gate.begin_maintenance(), timeout=remaining)
        except TimeoutError as exc:
            if self._gate.state == OwnerAdapterMaintenanceGate.DRAINING:
                self._gate.cancel_drain()
            raise MaintenanceCoordinatorError(
                "CLIENT_DRAIN_TIMEOUT",
                "Owner adapter maintenance drain deadline expired",
            ) from exc
        adapter = self._adapter
        if adapter is None:
            self._gate.mark_recovery_required()
            raise AdapterError("BACKEND_UNAVAILABLE", "Owner adapter is unavailable for governed maintenance")
        try:
            remaining = drain_deadline - time.time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(adapter.close(), timeout=remaining)
        except TimeoutError as exc:
            self._gate.mark_recovery_required()
            raise MaintenanceCoordinatorError(
                "CLIENT_DRAIN_TIMEOUT",
                "Owner adapter close did not complete within the maintenance deadline",
            ) from exc
        except BaseException:
            self._gate.mark_recovery_required()
            raise
        self._adapter = None
        self._gate.mark_closed()

    async def reopen(self) -> MemoryAdapter:
        self._require_loop()
        if self._adapter is not None:
            if self._gate.state == OwnerAdapterMaintenanceGate.RUNNING:
                return self._adapter
            self._gate.mark_recovery_required()
            raise AdapterError(
                "BACKEND_UNAVAILABLE",
                "Owner adapter cannot reopen while a prior adapter remains bound",
            )
        if self._control is None or self._overview is None or self._operations is None:
            self._gate.mark_recovery_required()
            raise RuntimeError("Owner adapter participant services are not bound")
        self._gate.mark_reopening()
        candidate: MemoryAdapter | None = None
        try:
            candidate = await self._adapter_factory(
                self._launch.database_path,
                self._launch.profile,
                runtime_agent_id=self._launch.host_id,
                trusted_write_context=self._launch.trusted_write_context,
            )
            if not isinstance(candidate, MemoryAdapter):
                raise TypeError("adapter_factory must return a governed MemoryAdapter")
            if getattr(candidate, "_trusted_write_context", None) is not self._launch.trusted_write_context:
                raise AdapterError(
                    "WRITE_AUTHORIZATION_DENIED",
                    "reopened Owner adapter did not preserve the exact trusted launch context",
                )
            self._control._rebind_adapter(candidate)
            self._overview._rebind_owner_adapter()
            self._operations._rebind_owner_adapter()
        except BaseException:
            if candidate is not None:
                try:
                    await candidate.close()
                except BaseException:
                    pass
            self._adapter = None
            self._gate.mark_recovery_required()
            raise
        self._adapter = candidate
        self._gate.mark_running()
        return candidate

    async def close_current(self) -> None:
        self._require_loop()
        adapter = self._adapter
        self._adapter = None
        if adapter is not None:
            await adapter.close()

    def mark_recovery_required(self) -> None:
        self._gate.mark_recovery_required()


class _OwnerAdapterExecutor:
    """Run one database operation against the current reopened Owner adapter under the shared gate."""

    def __init__(
        self,
        participant: _OwnerAdapterMaintenanceParticipant,
        gate: OwnerAdapterMaintenanceGate,
    ) -> None:
        self._participant = participant
        self._gate = gate

    async def run(self, callback: Callable[[MemoryAdapter], Any | Awaitable[Any]]) -> Any:
        if not callable(callback):
            raise TypeError("Owner adapter executor callback must be callable")
        await self._gate.enter()
        try:
            adapter = self._participant.adapter
            if adapter is None:
                raise AdapterError(
                    "BACKEND_UNAVAILABLE",
                    "Owner adapter is unavailable while governed maintenance is active",
                )
            value = callback(adapter)
            if inspect.isawaitable(value):
                value = await value
            return value
        finally:
            await self._gate.exit()


class _OwnerAdapterMaintenanceCoordinator:
    """MaintenanceCoordinator facade adding the in-process Owner adapter participant."""

    _OWNER_ADAPTER_REOPEN_TIMEOUT_SECONDS = 1.0
    _RECOVERY_REQUIRED_MESSAGE = (
        "maintenance preparation requires recovery before normal access can resume"
    )

    def __init__(
        self,
        delegate: MaintenanceCoordinator,
        participant: _OwnerAdapterMaintenanceParticipant,
    ) -> None:
        self._delegate = delegate
        self._participant = participant
        self.barrier = delegate.barrier
        self.registry = delegate.registry

    async def prepare_close(self, operation_id: str, *, drain_deadline: float):
        try:
            await self._participant.prepare_close(drain_deadline=drain_deadline)
        except BaseException as participant_error:
            if self._participant.state == OwnerAdapterMaintenanceGate.RECOVERY_REQUIRED:
                raise MaintenanceCoordinatorError(
                    "MAINTENANCE_RECOVERY_REQUIRED",
                    self._RECOVERY_REQUIRED_MESSAGE,
                ) from participant_error
            raise
        try:
            return await self._delegate.prepare_close(operation_id, drain_deadline=drain_deadline)
        except BaseException as coordinator_error:
            barrier_state: str | None = None
            try:
                snapshot = self.barrier.read()
                barrier_state = getattr(snapshot, "state", None)
                if barrier_state == "RUNNING":
                    await self._participant.reopen()
                else:
                    self._participant.mark_recovery_required()
            except BaseException:
                self._participant.mark_recovery_required()
            try:
                barrier_state = getattr(self.barrier.read(), "state", None)
            except BaseException:
                barrier_state = None
                self._participant.mark_recovery_required()
            if (
                barrier_state != "RUNNING"
                or self._participant.state != OwnerAdapterMaintenanceGate.RUNNING
            ):
                raise MaintenanceCoordinatorError(
                    "MAINTENANCE_RECOVERY_REQUIRED",
                    self._RECOVERY_REQUIRED_MESSAGE,
                ) from coordinator_error
            raise coordinator_error

    async def abort_pre_mutation(self, prepared) -> None:
        await self._delegate.abort_pre_mutation(prepared)
        failure_code, reopen_error = await self._reopen_participant_bounded()
        if failure_code is not None:
            assert reopen_error is not None
            raise MaintenanceCoordinatorError(
                "MAINTENANCE_RECOVERY_REQUIRED",
                self._RECOVERY_REQUIRED_MESSAGE,
            ) from reopen_error

    async def recover_aborted_pre_mutation(self, prepared):
        snapshot = await self._delegate.recover_aborted_pre_mutation(prepared)
        failure_code, reopen_error = await self._reopen_participant_bounded()
        if failure_code is not None:
            assert reopen_error is not None
            raise MaintenanceCoordinatorError(
                "MAINTENANCE_RECOVERY_REQUIRED",
                self._RECOVERY_REQUIRED_MESSAGE,
            ) from reopen_error
        return snapshot

    def begin_activation(self, prepared, *, new_generation: int):
        return self._delegate.begin_activation(prepared, new_generation=new_generation)

    def publish_running(self, prepared, *, new_generation: int):
        return self._delegate.publish_running(prepared, new_generation=new_generation)

    async def _reopen_participant_bounded(self) -> tuple[str | None, BaseException | None]:
        try:
            await asyncio.wait_for(
                self._participant.reopen(),
                timeout=self._OWNER_ADAPTER_REOPEN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            self._participant.mark_recovery_required()
            return "OWNER_ADAPTER_REOPEN_TIMEOUT", exc
        except asyncio.CancelledError:
            self._participant.mark_recovery_required()
            raise
        except Exception as exc:
            self._participant.mark_recovery_required()
            return "OWNER_ADAPTER_REOPEN_FAILED", exc
        return None, None

    async def resume_running(self, prepared, *, new_generation: int) -> ResumeReport:
        report = await self._delegate.resume_running(prepared, new_generation=new_generation)
        failure_code, _reopen_error = await self._reopen_participant_bounded()
        participant_failures = dict(report.participant_failures)
        if failure_code is not None:
            participant_failures["owner_adapter"] = failure_code
        return ResumeReport(
            ready_instance_ids=report.ready_instance_ids,
            failures=report.failures,
            participant_failures=participant_failures,
        )

    async def publish_activation_and_resume(self, prepared, *, new_generation: int) -> ResumeReport:
        self.begin_activation(prepared, new_generation=new_generation)
        self.publish_running(prepared, new_generation=new_generation)
        return await self.resume_running(prepared, new_generation=new_generation)

    async def resume_activating(self, prepared, *, new_generation: int) -> ResumeReport:
        report = await self._delegate.resume_activating(prepared, new_generation=new_generation)
        failure_code, _reopen_error = await self._reopen_participant_bounded()
        participant_failures = dict(report.participant_failures)
        if failure_code is not None:
            participant_failures["owner_adapter"] = failure_code
        return ResumeReport(
            ready_instance_ids=report.ready_instance_ids,
            failures=report.failures,
            participant_failures=participant_failures,
        )

    def rearm_recovery(self, prepared):
        return self._delegate.rearm_recovery(prepared)

    def mark_recovery_required(self, operation_id: str):
        snapshot = self._delegate.mark_recovery_required(operation_id)
        self._participant.mark_recovery_required()
        return snapshot

    def activation_state(
        self,
        operation_id: str,
        *,
        new_generation: int,
        device: int | None = None,
        inode: int | None = None,
    ):
        return self._delegate.activation_state(
            operation_id,
            new_generation=new_generation,
            device=device,
            inode=inode,
        )


def _database_state_roots(
    spec: BackendLaunchSpec,
    database_path: str,
) -> tuple[Path, Path, Path]:
    if spec.db is None:
        raise ValueError("database state roots require configured database authority")
    canonical = os.path.realpath(os.path.abspath(database_path))
    expected = os.path.realpath(os.path.abspath(spec.db))
    if canonical != expected:
        raise ValueError("database state roots must derive from the exact configured database")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    base = Path(spec.state_dir).absolute() / "database" / digest
    return base / "artifacts", base / "restore", base / "purge"


def _configured_embedding_profile(profile_name: str | None) -> EmbeddingProfile:
    if not isinstance(profile_name, str) or not profile_name:
        raise AdapterError("INVALID_ENVELOPE", "configured embedding profile is required")
    try:
        definition = PROFILE_DEFINITIONS[profile_name]
        model = definition["model"]
        model_digest = MODEL_DIGESTS[model]
    except KeyError as exc:
        raise AdapterError("INVALID_ENVELOPE", "configured embedding profile is unsupported") from exc
    return EmbeddingProfile(
        profile_id=profile_name,
        provider_kind="OLLAMA_LOCAL",
        base_model_identity=model,
        model_digest=model_digest,
        source_dimension=int(definition["source_dimension"]),
        output_dimension=int(definition["output_dimension"]),
        normalization_policy="OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
        distance_metric="cosine",
        projection_version="ollama-dimensions-v1",
        document_prefix=str(definition["document_prefix"]),
        query_prefix=str(definition["query_prefix"]),
        semantic_family=str(definition["semantic_family"]),
        minimum_context_window=int(definition["minimum_context_window"]),
    )


def _safe_logical_import_preview(value: Mapping[str, Any]) -> dict[str, Any]:
    destination_results = value.get("destination_results", [])
    if not isinstance(destination_results, list):
        raise AdapterError("DESTINATION_PROBE_INVALID", "logical import destination results are invalid")
    missing_lineage = value.get("missing_lineage", [])
    if not isinstance(missing_lineage, list):
        raise AdapterError("INVALID_ENVELOPE", "logical import missing-lineage summary is invalid")
    return {
        "format": value.get("format"),
        "file_digest": value.get("file_digest"),
        "record_count": value.get("record_count"),
        "candidate_admission_count": value.get("candidate_admission_count"),
        "external_history_count": value.get("external_history_count"),
        "missing_lineage_count": len(missing_lineage),
        "exact_scopes": value.get("exact_scopes", []),
        "scope_counts": value.get("scope_counts", {}),
        "scope_identity_digest": value.get("scope_identity_digest"),
        "source_approval_policy": value.get("source_approval_policy"),
        "destination_analysis": value.get("destination_analysis"),
        "destination_summary": value.get("destination_summary", {}),
        "destination_results": destination_results,
        "destination_duplicates": value.get("destination_duplicates", []),
        "destination_conflicts": value.get("destination_conflicts", []),
    }


def _safe_logical_import_apply(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "status",
        "action",
        "operation_id",
        "request_id",
        "idempotency_key",
        "idempotent_replay",
        "evidence_policy",
        "admitted",
        "duplicates",
        "contradictions",
        "active_source_evidence_count",
        "external_history_count",
        "event_ids",
        "committed_at",
        "file_digest",
        "scope_identity_digest",
    )
    return {key: value[key] for key in allowed if key in value}


def _governed_purge_targets(
    identities: tuple[PurgeTargetIdentity, ...],
    rows: Any,
) -> tuple[RecoveryTarget, ...]:
    if not isinstance(rows, list) or len(rows) != len(identities):
        raise AdapterError("PURGE_TARGET_INVALID", "purge target resolver returned an incomplete governed result")
    expected_fields = {
        "scope",
        "project_id",
        "worktree_id",
        "workflow_id",
        "agent_id",
        "memory_id",
        "record_id",
        "revision",
        "lifecycle_state",
        "materialization_class",
        "native_materialized",
        "native_content_hash",
    }
    targets: list[RecoveryTarget] = []
    for identity, row in zip(identities, rows, strict=True):
        if not isinstance(row, Mapping) or set(row) != expected_fields:
            raise AdapterError("PURGE_TARGET_INVALID", "purge target resolver returned an invalid governed mapping")
        exact = identity.to_mapping()
        if any(row.get(field) != exact.get(field) for field in exact):
            raise AdapterError("PURGE_TARGET_DRIFT", "purge target resolver changed exact target identity or order")
        materialization = row.get("materialization_class")
        native_materialized = row.get("native_materialized")
        if (
            materialization == PM1
            and native_materialized is not True
        ) or (
            materialization == PM0
            and native_materialized is not False
        ) or materialization not in {PM0, PM1}:
            raise AdapterError("PURGE_TARGET_INVALID", "purge target materialization is unsupported")
        lifecycle = row.get("lifecycle_state")
        native_hash = row.get("native_content_hash")
        if not isinstance(lifecycle, str) or not lifecycle or not isinstance(native_hash, str) or not native_hash:
            raise AdapterError("PURGE_TARGET_INVALID", "purge target governed state is malformed")
        targets.append(
            RecoveryTarget(
                record_id=identity.record_id,
                memory_id=identity.memory_id,
                revision=identity.revision,
                scope=identity.scope,
                expected_lifecycle=lifecycle,
                expected_materialization=materialization,
                expected_native_hash=native_hash,
            )
        )
    return tuple(targets)


def _make_restore_postflight(
    database_path: str,
    profile: EmbeddingProfile,
) -> Callable[[str, Any], Awaitable[None]]:
    canonical = os.path.realpath(os.path.abspath(database_path))

    async def postflight(db_path: str, capability: Any) -> None:
        if os.path.realpath(os.path.abspath(db_path)) != canonical:
            raise AdapterError("POST_ACTIVATION_VERIFICATION_FAILED", "restore postflight database identity changed")
        engine = SQLiteVecEngine._for_maintenance(canonical, profile, capability)
        try:
            await engine.open()
            if engine.conn is None:
                raise AdapterError("POST_ACTIVATION_VERIFICATION_FAILED", "restore maintenance engine did not open")
            row = engine.conn.execute("PRAGMA quick_check").fetchone()
            if row is None or row[0] != "ok":
                raise AdapterError("POST_ACTIVATION_VERIFICATION_FAILED", "restored database quick_check failed")
        finally:
            await engine.close()

    return postflight


def _make_post_activation_verifier(
    database_path: str,
    registry: Any,
    runtime_client: Any,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    canonical = os.path.realpath(os.path.abspath(database_path))

    async def verify(**kwargs: Any) -> dict[str, Any]:
        operation_id = kwargs["operation_id"]
        generation = kwargs["generation"]
        database_device = kwargs["database_device"]
        database_inode = kwargs["database_inode"]
        expected_contract = kwargs["expected_storage_contract_version"]
        expected_profile = kwargs["expected_embedding_profile"]
        ready_ids = list(kwargs["ready_instance_ids"])
        reconnect_failures = dict(kwargs["reconnect_failures"])
        if kwargs["database_path"] != canonical:
            raise AdapterError("POST_ACTIVATION_VERIFICATION_FAILED", "post-activation database path changed")
        info = os.stat(canonical)
        if info.st_dev != database_device or info.st_ino != database_inode:
            raise AdapterError("POST_ACTIVATION_VERIFICATION_FAILED", "post-activation database identity changed")

        uri = f"file:{quote(canonical, safe='/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(
                "SELECT key,value FROM adapter_meta WHERE key IN ('contract_version','embedding_profile') ORDER BY key LIMIT 2"
            ).fetchall()
        finally:
            conn.close()
        metadata = {str(key): value for key, value in rows}
        raw_profile = metadata.get("embedding_profile")
        if not isinstance(raw_profile, str) or len(raw_profile.encode("utf-8")) > 16_384:
            raise AdapterError("POST_ACTIVATION_VERIFICATION_FAILED", "post-activation embedding profile is invalid")
        try:
            observed_profile = json.loads(raw_profile)
            observed_contract = int(metadata.get("contract_version"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AdapterError("POST_ACTIVATION_VERIFICATION_FAILED", "post-activation metadata is invalid") from exc
        if observed_contract != expected_contract or observed_profile != expected_profile:
            raise AdapterError("POST_ACTIVATION_VERIFICATION_FAILED", "post-activation metadata changed")

        originals = {
            identity.instance_id: identity
            for identity in (
                RuntimeIdentity.from_mapping(item)
                for item in kwargs["initially_open_runtime_identities"]
            )
        }
        current = {
            identity.instance_id: identity
            for identity in registry.registrations(database_path=canonical, require_live=True)
        }
        evidence: dict[str, dict[str, Any]] = {}
        for instance_id in ready_ids:
            original = originals.get(instance_id)
            identity = current.get(instance_id)
            if original is None or identity is None:
                raise AdapterError("POST_ACTIVATION_VERIFICATION_FAILED", "READY runtime identity is missing")
            immutable_fields = (
                "protocol_version",
                "instance_id",
                "owner_uid",
                "pid",
                "process_start_ticks",
                "boot_id",
                "database_path",
                "control_socket_path",
            )
            if any(getattr(identity, field) != getattr(original, field) for field in immutable_fields):
                raise AdapterError("POST_ACTIVATION_VERIFICATION_FAILED", "READY runtime identity changed")
            if (
                identity.database_path != canonical
                or identity.generation != generation
                or identity.device != database_device
                or identity.inode != database_inode
                or identity.lifecycle_state != "OPEN"
            ):
                raise AdapterError("POST_ACTIVATION_VERIFICATION_FAILED", "READY runtime registration is stale")
            request = RuntimeControlRequest.from_mapping(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": str(uuid.uuid5(uuid.UUID(operation_id), f"{instance_id}:INSPECT")),
                    "operation_id": operation_id,
                    "instance_id": instance_id,
                    "expected_generation": generation,
                    "command": "INSPECT",
                }
            )
            response = await runtime_client.call(identity, request)
            if (
                response.get("status_code") != "OK"
                or response.get("state") != "OPEN"
                or response.get("actual_generation") != generation
                or response.get("lease_open") is not True
            ):
                raise AdapterError("POST_ACTIVATION_VERIFICATION_FAILED", "READY runtime INSPECT proof failed")
            evidence[instance_id] = {**identity.to_mapping(), "lease_open": True}

        return {
            "status": "VERIFIED",
            "operation_id": operation_id,
            "generation": generation,
            "database_path": canonical,
            "database_device": database_device,
            "database_inode": database_inode,
            "storage_contract_version": expected_contract,
            "embedding_profile": expected_profile,
            "bounded_read_verified": True,
            "ready_instance_ids": ready_ids,
            "reconnect_failures": reconnect_failures,
            "resumed_runtime_evidence": evidence,
        }

    return verify


class _CaddySubprocess:
    def __init__(
        self,
        process: Any,
        *,
        origin: str,
        root_cert_path: str | os.PathLike[str],
    ) -> None:
        self._process = process
        self._origin = origin
        self._root_cert_path = os.fspath(root_cert_path)

    def poll(self) -> int | None:
        return self._process.poll()

    def is_ready(self) -> bool:
        if self.poll() is not None:
            return False
        readiness = probe_linux_managed_https(
            self._origin,
            root_cert_path=self._root_cert_path,
            timeout_seconds=_CADDY_READINESS_CONNECT_TIMEOUT_SECONDS,
        )
        return (
            readiness.status is ManagedServiceActivationStatus.READY
            and self.poll() is None
        )

    def terminate(self) -> None:
        if self.poll() is None:
            self._process.terminate()

    def wait_stopped(self, timeout: float) -> bool:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
            raise ValueError("timeout must be a non-negative number")
        if self.poll() is not None:
            return True
        try:
            self._process.wait(timeout=float(timeout))
        except subprocess.TimeoutExpired:
            return False
        return True


def _validated_caddy_config(config: WebUIRuntimeConfig) -> WebUIRuntimeConfig:
    if not isinstance(config, WebUIRuntimeConfig):
        raise TypeError("config must be a WebUIRuntimeConfig")
    # Reconstruct the frozen config so a mutated/corrupted instance cannot bypass
    # the exact path/origin validation performed at load time.
    return WebUIRuntimeConfig(
        db=config.db,
        profile=config.profile,
        host_id=config.host_id,
        origin=config.origin,
        backend_socket=config.backend_socket,
        caddy_path=config.caddy_path,
        caddyfile=config.caddyfile,
        runtime_dir=config.runtime_dir,
        state_dir=config.state_dir,
        public_origins=config.public_origins,
        platform_kind=config.platform_kind,
        backend_transport=config.backend_transport,
    )


def create_caddy_process(
    config: WebUIRuntimeConfig,
    argv: tuple[str, ...],
) -> _CaddySubprocess:
    """Start exact Caddy and prove the managed TLS/backend path before readiness."""

    validated = _validated_caddy_config(config)
    expected_argv = caddy_command(validated)
    if not isinstance(argv, tuple) or argv != expected_argv:
        raise ValueError("Caddy argv does not match the validated runtime config")
    parsed = urlsplit(validated.origin)
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None or parsed.scheme != "https":
        raise ValueError("Caddy readiness origin is unsafe")
    process = subprocess.Popen(argv, shell=False)
    root_cert_path = (
        Path(validated.state_dir)
        / "caddy"
        / "pki"
        / "authorities"
        / "local"
        / "root.crt"
    )
    # A generic TCP connect is insufficient: another local process may already
    # own the candidate port while the just-started Caddy child is still exiting
    # its bind failure.  Reuse the installer's exact local CA/SNI/Host/health
    # proof so the runtime cannot claim an endpoint merely because *some*
    # loopback listener answered there.
    return _CaddySubprocess(
        process,
        origin=validated.origin,
        root_cert_path=root_cert_path,
    )


class _RegistryRuntimeObserver:
    """Expose only runtime facts the registry can actually prove.

    Runtime registrations do not carry FactLane semantic agent/host identifiers, so
    they cannot honestly be projected as OwnerOperations agent observations.  The
    registry is still useful as bounded support diagnostics and for maintenance
    coordination.
    """

    def __init__(self, registry: RuntimeRegistry, database_path: str) -> None:
        self._registry = registry
        self._database_path = os.path.realpath(os.path.abspath(database_path))

    async def observations(self) -> list[dict[str, Any]]:
        return []

    async def diagnostics(self) -> dict[str, Any]:
        try:
            registrations = self._registry.registrations(
                database_path=self._database_path,
                require_live=False,
            )
        except Exception:
            return {"runtime_registry": {"state": "UNAVAILABLE"}}
        return {
            "runtime_registry": {
                "state": "AVAILABLE",
                "registration_records": len(registrations),
            }
        }


class _UnconfiguredOperationsConfig:
    """An explicit empty Owner-editable config surface.

    No production config writer exists in the current contract.  Returning an empty
    allowlist keeps reads truthful and makes OwnerOperations reject every attempted
    browser setting change before any filesystem/config authority is reached.
    """

    async def configured_agents(self) -> list[dict[str, Any]]:
        return []

    async def setting_specs(self) -> dict[str, dict[str, Any]]:
        return {}

    async def configured_settings(self) -> dict[str, Any]:
        return {}

    async def effective_settings(self) -> dict[str, Any]:
        return {}

    async def validate_settings(self, _changes: Mapping[str, Any]) -> dict[str, Any]:
        raise AdapterError("SETTING_NOT_ALLOWED", "no Owner-editable production settings are configured")

    async def apply_settings(self, _changes: Mapping[str, Any]) -> dict[str, Any]:
        raise AdapterError("SETTING_NOT_ALLOWED", "no Owner-editable production settings are configured")


_WINDOWS_SETUP_HOST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_WINDOWS_SETUP_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_WINDOWS_SETUP_INSTALLATION_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


def _validate_existing_windows_setup_database(
    database_path: str,
    profile_name: str,
    *,
    platform_name: str = "Windows",
) -> None:
    if not os.path.exists(database_path):
        return
    if not os.path.isfile(database_path):
        raise OwnerSetupError(
            "SETUP_DATABASE_INVALID",
            "selected database is not a regular FactLane database file",
        )
    try:
        if os.path.getsize(database_path) <= 0:
            raise OwnerSetupError(
                "SETUP_DATABASE_INVALID",
                "existing database has no FactLane storage contract",
            )
    except OSError as exc:
        raise OwnerSetupError(
            "SETUP_DATABASE_INVALID",
            "existing database identity could not be inspected",
        ) from exc

    try:
        with tempfile.TemporaryDirectory(prefix="factlane-setup-db-preflight-") as directory:
            copied = os.path.join(directory, "factlane.db")
            shutil.copy2(database_path, copied)
            for suffix in ("-wal", "-shm"):
                source = database_path + suffix
                if os.path.exists(source):
                    shutil.copy2(source, copied + suffix)

            version = inspect_storage_contract_version(copied)
            if version == 2:
                raise OwnerSetupError(
                    "SETUP_DATABASE_MIGRATION_REQUIRED",
                    f"selected database uses storage v2 and must be migrated before {platform_name} setup can bind it",
                    status=409,
                )
            if version != STORAGE_CONTRACT_VERSION:
                raise OwnerSetupError(
                    "SETUP_DATABASE_INVALID",
                    "selected database is not a current FactLane storage database",
                )

            uri = "file:" + quote(copied, safe="/") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            try:
                import sqlite_vec

                conn.enable_load_extension(True)
                try:
                    sqlite_vec.load(conn)
                finally:
                    conn.enable_load_extension(False)
                row = conn.execute(
                    "SELECT value FROM adapter_meta WHERE key='embedding_profile'"
                ).fetchone()
                if row is None:
                    materialized = sum(
                        int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                        for table in ("adapter_records", "memories", "memory_embeddings")
                    )
                    if materialized:
                        raise OwnerSetupError(
                            "SETUP_DATABASE_PROFILE_UNPROVEN",
                            "existing FactLane records have no stored embedding profile binding",
                            status=409,
                        )
                    return
                try:
                    raw_profile = json.loads(row[0])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise OwnerSetupError(
                        "SETUP_DATABASE_PROFILE_INVALID",
                        "stored embedding profile metadata is invalid",
                        status=409,
                    ) from exc
                stored_profile = parse_embedding_profile_metadata(raw_profile)
                if stored_profile is None:
                    raise OwnerSetupError(
                        "SETUP_DATABASE_PROFILE_INVALID",
                        "stored embedding profile metadata is invalid",
                        status=409,
                    )
                selected = _configured_embedding_profile(profile_name)
                if stored_profile.get("semantic_identity") != selected.semantic_identity():
                    raise OwnerSetupError(
                        "SETUP_DATABASE_PROFILE_MISMATCH",
                        "selected embedding profile does not match the existing database binding",
                        status=409,
                    )
            finally:
                conn.close()
    except OwnerSetupError:
        raise
    except AdapterError as exc:
        raise OwnerSetupError(
            "SETUP_DATABASE_INVALID",
            "existing database failed FactLane storage validation",
        ) from exc
    except (ImportError, OSError, sqlite3.Error) as exc:
        raise OwnerSetupError(
            "SETUP_DATABASE_INVALID",
            "existing database could not be inspected safely",
        ) from exc


class _WindowsSetupBindingService:
    """Bind the missing Windows memory authority from the authenticated setup shell."""

    def __init__(
        self,
        *,
        config_path: str,
        baseline_config: WindowsManagedConfig,
        owner_sid: str,
        supported_profiles: set[str] | frozenset[str],
        config_loader: Callable[..., WindowsManagedConfig | None] = load_windows_managed_config,
        config_writer: Callable[..., None] = write_owner_private_text_atomic,
        database_validator: Callable[..., str] = validate_windows_database_binding_path,
        database_inspector: Callable[[str, str], None] = _validate_existing_windows_setup_database,
        restart_preparer: Callable[[], Any] = prepare_windows_managed_restart,
    ) -> None:
        if any(
            value is not None
            for value in (baseline_config.db, baseline_config.profile, baseline_config.host_id)
        ):
            raise ValueError("Windows setup binding requires an unconfigured managed config")
        self.config_path = config_path
        self.baseline_config = baseline_config
        self.owner_sid = owner_sid
        self.supported_profiles = frozenset(supported_profiles)
        if not self.supported_profiles:
            raise ValueError("Windows setup binding requires at least one supported profile")
        self._config_loader = config_loader
        self._config_writer = config_writer
        self._database_validator = database_validator
        self._database_inspector = database_inspector
        self._restart_preparer = restart_preparer
        self._lock = threading.Lock()
        self._published_config: WindowsManagedConfig | None = None
        self._restart_plan: Any | None = None
        self._restart_request_id: str | None = None
        self._restart_handed_off = False
        self._restart_error: str | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            current = self._load_current()
            if current == self.baseline_config:
                return self._setup_required_payload()
            if self._published_config is not None and current == self._published_config:
                payload: dict[str, Any] = {
                    "status": (
                        "CONFIG_PUBLISHED_RESTART_REQUIRED"
                        if self._restart_error is not None
                        else "CONFIG_PUBLISHED_RESTART_REQUESTED"
                    ),
                    "profiles": sorted(self.supported_profiles),
                    "suggested_host_id": self._suggested_host_id(),
                    "suggested_db": self._suggested_database_path(),
                    "binding": {
                        "db": current.db,
                        "profile": current.profile,
                        "host_id": current.host_id,
                    },
                }
                if self._restart_error is not None:
                    payload["restart_error"] = self._restart_error
                return payload
            raise OwnerSetupError(
                "SETUP_CONFIG_CHANGED",
                "managed Windows configuration changed while setup was active",
                status=409,
            )

    def bind(
        self,
        *,
        db: str,
        profile: str,
        host_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            self._validate_request_id(request_id)
            if profile not in self.supported_profiles:
                raise OwnerSetupError(
                    "SETUP_PROFILE_UNSUPPORTED",
                    "selected embedding profile is not supported",
                )
            if _WINDOWS_SETUP_HOST_ID_RE.fullmatch(host_id) is None:
                raise OwnerSetupError(
                    "SETUP_HOST_ID_INVALID",
                    "host ID must be a bounded non-secret identifier",
                )
            try:
                canonical_db = self._database_validator(db, owner_sid=self.owner_sid)
            except WindowsWebUIError as exc:
                raise OwnerSetupError(exc.code, exc.safe_message) from exc
            except Exception as exc:
                raise OwnerSetupError(
                    "WINDOWS_DATABASE_PATH_INVALID",
                    "database path could not be validated safely",
                ) from exc
            self._database_inspector(canonical_db, profile)

            current = self._load_current()
            if self._published_config is not None and current == self._published_config:
                if (
                    current.db != canonical_db
                    or current.profile != profile
                    or current.host_id != host_id
                ):
                    raise OwnerSetupError(
                        "SETUP_ALREADY_CONFIGURED",
                        "managed Windows memory binding is already published",
                        status=409,
                    )
                if self._restart_error is None:
                    return {
                        "status": "CONFIG_PUBLISHED",
                        "restart": "REQUESTED",
                        "request_id": self._restart_request_id or request_id,
                    }
                restart_plan = self._prepare_restart()
                self._restart_plan = restart_plan
                self._restart_request_id = request_id
                self._restart_handed_off = False
                self._restart_error = None
                return {
                    "status": "CONFIG_PUBLISHED",
                    "restart": "REQUESTED",
                    "request_id": request_id,
                }
            if current != self.baseline_config:
                raise OwnerSetupError(
                    "SETUP_CONFIG_CHANGED",
                    "managed Windows configuration changed while setup was active",
                    status=409,
                )

            restart_plan = self._prepare_restart()
            published = replace(
                current,
                db=canonical_db,
                profile=profile,
                host_id=host_id,
            )
            try:
                self._config_writer(
                    self.config_path,
                    render_windows_managed_config(published),
                    owner_sid=self.owner_sid,
                )
                observed = self._config_loader(self.config_path, owner_sid=self.owner_sid)
                if observed != published:
                    raise WindowsWebUIError(
                        "WINDOWS_CONFIG_PUBLICATION_UNVERIFIED",
                        "managed Windows configuration publication could not be verified",
                    )
            except Exception as exc:
                self._restore_baseline_after_publication_failure()
                if isinstance(exc, WindowsWebUIError):
                    raise OwnerSetupError(exc.code, exc.safe_message, status=500) from exc
                raise OwnerSetupError(
                    "WINDOWS_CONFIG_PUBLICATION_FAILED",
                    "managed Windows configuration could not be published safely",
                    status=500,
                ) from exc

            self._published_config = published
            self._restart_plan = restart_plan
            self._restart_request_id = request_id
            self._restart_handed_off = False
            self._restart_error = None
            return {
                "status": "CONFIG_PUBLISHED",
                "restart": "REQUESTED",
                "request_id": request_id,
            }

    def handoff_restart(self, request_id: str) -> bool:
        with self._lock:
            if (
                request_id != self._restart_request_id
                or self._restart_plan is None
                or self._restart_handed_off
            ):
                return False
            plan = self._restart_plan
            try:
                plan.handoff()
            except WindowsWebUIError as exc:
                self._restart_error = exc.code
                self._restart_plan = None
                return False
            except Exception:
                self._restart_error = "WINDOWS_RESTART_HANDOFF_FAILED"
                self._restart_plan = None
                return False
            self._restart_handed_off = True
            self._restart_plan = None
            return True

    def _prepare_restart(self) -> Any:
        try:
            return self._restart_preparer()
        except WindowsWebUIError as exc:
            raise OwnerSetupError(exc.code, exc.safe_message, status=503) from exc
        except Exception as exc:
            raise OwnerSetupError(
                "WINDOWS_RESTART_UNAVAILABLE",
                "managed Windows restart could not be proven before configuration publication",
                status=503,
            ) from exc

    def _load_current(self) -> WindowsManagedConfig:
        try:
            current = self._config_loader(self.config_path, owner_sid=self.owner_sid)
        except WindowsWebUIError as exc:
            raise OwnerSetupError(exc.code, exc.safe_message, status=500) from exc
        except Exception as exc:
            raise OwnerSetupError(
                "WINDOWS_CONFIG_INVALID",
                "managed Windows configuration could not be read safely",
                status=500,
            ) from exc
        if current is None:
            raise OwnerSetupError(
                "WINDOWS_MANAGED_CONFIG_UNAVAILABLE",
                "managed Windows configuration is unavailable",
                status=409,
            )
        return current

    def _restore_baseline_after_publication_failure(self) -> None:
        try:
            observed = self._config_loader(self.config_path, owner_sid=self.owner_sid)
            if observed == self.baseline_config:
                return
            self._config_writer(
                self.config_path,
                render_windows_managed_config(self.baseline_config),
                owner_sid=self.owner_sid,
            )
            restored = self._config_loader(self.config_path, owner_sid=self.owner_sid)
            if restored != self.baseline_config:
                raise RuntimeError("baseline verification failed")
        except Exception as exc:
            raise OwnerSetupError(
                "SETUP_CONFIG_RECOVERY_REQUIRED",
                "managed Windows configuration could not be restored after publication failure",
                status=500,
            ) from exc

    def _setup_required_payload(self) -> dict[str, Any]:
        return {
            "status": "SETUP_REQUIRED",
            "profiles": sorted(self.supported_profiles),
            "suggested_host_id": self._suggested_host_id(),
            "suggested_db": self._suggested_database_path(),
        }

    def _suggested_host_id(self) -> str:
        installation_id = self.baseline_config.installation_id
        if _WINDOWS_SETUP_INSTALLATION_ID_RE.fullmatch(installation_id) is None:
            return ""
        suggested = f"owner-{installation_id}"
        return suggested if _WINDOWS_SETUP_HOST_ID_RE.fullmatch(suggested) is not None else ""

    def _suggested_database_path(self) -> str:
        return str(PureWindowsPath(self.baseline_config.state_dir) / "memory.db")

    @staticmethod
    def _validate_request_id(request_id: str) -> None:
        if _WINDOWS_SETUP_REQUEST_ID_RE.fullmatch(request_id) is None:
            raise OwnerSetupError(
                "SETUP_REQUEST_ID_INVALID",
                "setup request ID is invalid",
            )


_LINUX_SETUP_SYSTEMCTL_TIMEOUT_SECONDS = 5.0


def _run_linux_setup_systemctl(argv: tuple[str, ...]) -> tuple[int, str]:
    completed = subprocess.run(
        argv,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=_LINUX_SETUP_SYSTEMCTL_TIMEOUT_SECONDS,
    )
    return completed.returncode, completed.stdout


class _LinuxManagedRestartPlan:
    def __init__(
        self,
        systemctl_path: str,
        *,
        runner: Callable[[tuple[str, ...]], tuple[int, str]] = _run_linux_setup_systemctl,
    ) -> None:
        self.systemctl_path = systemctl_path
        self._runner = runner

    def handoff(self) -> None:
        try:
            returncode, _stdout = self._runner(
                (
                    self.systemctl_path,
                    "--user",
                    "restart",
                    "--no-block",
                    WEBUI_SERVICE_NAME,
                )
            )
        except Exception as exc:
            raise RuntimeError("managed Linux restart handoff failed") from exc
        if returncode != 0:
            raise RuntimeError("managed Linux restart handoff failed")


def _prepare_linux_managed_restart(
    *,
    systemctl_resolver: Callable[[str], str | None] = shutil.which,
    runner: Callable[[tuple[str, ...]], tuple[int, str]] = _run_linux_setup_systemctl,
) -> _LinuxManagedRestartPlan:
    systemctl_path = systemctl_resolver("systemctl")
    if (
        not isinstance(systemctl_path, str)
        or not os.path.isabs(systemctl_path)
        or not os.path.isfile(systemctl_path)
        or not os.access(systemctl_path, os.X_OK)
    ):
        raise OwnerSetupError(
            "LINUX_RESTART_UNAVAILABLE",
            "managed Linux WebUI restart is unavailable",
            status=503,
        )
    try:
        active_rc, active_out = runner(
            (systemctl_path, "--user", "is-active", WEBUI_SERVICE_NAME)
        )
        enabled_rc, enabled_out = runner(
            (systemctl_path, "--user", "is-enabled", WEBUI_SERVICE_NAME)
        )
    except Exception as exc:
        raise OwnerSetupError(
            "LINUX_RESTART_UNAVAILABLE",
            "managed Linux WebUI restart could not be proven before configuration publication",
            status=503,
        ) from exc
    if active_rc != 0 or active_out.strip().casefold() != "active":
        raise OwnerSetupError(
            "LINUX_RESTART_UNAVAILABLE",
            "managed Linux WebUI user service is not active",
            status=503,
        )
    if enabled_rc != 0 or enabled_out.strip().casefold() not in {"enabled", "enabled-runtime"}:
        raise OwnerSetupError(
            "LINUX_RESTART_UNAVAILABLE",
            "managed Linux WebUI user service is not enabled",
            status=503,
        )
    return _LinuxManagedRestartPlan(systemctl_path, runner=runner)


def _validate_linux_setup_database_path(value: str, *, owner_uid: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(ch in value for ch in ("\n", "\r"))
        or not os.path.isabs(value)
    ):
        raise OwnerSetupError(
            "SETUP_DATABASE_PATH_INVALID",
            "database path must be one canonical absolute POSIX path",
        )
    canonical = os.path.normpath(value)
    if canonical != value:
        raise OwnerSetupError(
            "SETUP_DATABASE_PATH_INVALID",
            "database path must be one canonical absolute POSIX path",
        )
    target = Path(canonical)
    try:
        current = target.parent
        child_stat: os.stat_result | None = None
        while True:
            current_stat = current.lstat()
            if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
                raise OwnerSetupError(
                    "SETUP_DATABASE_PATH_INVALID",
                    "database path must not traverse symlink or non-directory components",
                )
            mode = stat.S_IMODE(current_stat.st_mode)
            if current == target.parent:
                if current_stat.st_uid != owner_uid or mode & 0o022:
                    raise OwnerSetupError(
                        "SETUP_DATABASE_PATH_INVALID",
                        "database parent must be Owner-owned and must not be writable by group or other users",
                    )
            else:
                if current_stat.st_uid not in {0, owner_uid}:
                    raise OwnerSetupError(
                        "SETUP_DATABASE_PATH_INVALID",
                        "database path ancestor is owned by an untrusted OS identity",
                    )
                if mode & 0o022:
                    sticky_protects_child = bool(current_stat.st_mode & stat.S_ISVTX) and child_stat is not None and (
                        child_stat.st_uid in {0, owner_uid}
                    )
                    if not sticky_protects_child:
                        raise OwnerSetupError(
                            "SETUP_DATABASE_PATH_INVALID",
                            "database path ancestor permits replacement by another OS identity",
                        )
            if current.parent == current:
                break
            child_stat = current_stat
            current = current.parent
        if target.is_symlink():
            raise OwnerSetupError(
                "SETUP_DATABASE_PATH_INVALID",
                "database path must not be a symlink",
            )
        if target.exists():
            target_stat = target.stat()
            if (
                not target.is_file()
                or target_stat.st_uid != owner_uid
                or stat.S_IMODE(target_stat.st_mode) & 0o022
            ):
                raise OwnerSetupError(
                    "SETUP_DATABASE_PATH_INVALID",
                    "existing database must be one Owner-owned regular file that is not writable by group or other users",
                )
        for suffix in ("-wal", "-shm"):
            sidecar = Path(canonical + suffix)
            if not sidecar.exists() and not sidecar.is_symlink():
                continue
            if sidecar.is_symlink():
                raise OwnerSetupError(
                    "SETUP_DATABASE_PATH_INVALID",
                    "database sidecars must be Owner-owned regular files that are not writable by group or other users",
                )
            sidecar_stat = sidecar.stat()
            if (
                not sidecar.is_file()
                or sidecar_stat.st_uid != owner_uid
                or stat.S_IMODE(sidecar_stat.st_mode) & 0o022
            ):
                raise OwnerSetupError(
                    "SETUP_DATABASE_PATH_INVALID",
                    "database sidecars must be Owner-owned regular files that are not writable by group or other users",
                )
    except OwnerSetupError:
        raise
    except OSError as exc:
        raise OwnerSetupError(
            "SETUP_DATABASE_PATH_INVALID",
            "database path could not be validated safely",
        ) from exc
    return canonical


def _render_linux_runtime_config(config: WebUIRuntimeConfig) -> str:
    if (
        config.platform_kind is not PlatformKind.LINUX
        or config.backend_transport is not BackendTransportKind.AF_UNIX
    ):
        raise ValueError("Linux setup can publish only the AF_UNIX Linux runtime config")
    payload = {
        "db": config.db,
        "profile": config.profile,
        "host_id": config.host_id,
        "origin": config.origin,
        "backend_socket": config.backend_socket,
        "caddy_path": config.caddy_path,
        "caddyfile": config.caddyfile,
        "runtime_dir": config.runtime_dir,
        "state_dir": config.state_dir,
    }
    if config.public_origins:
        payload["public_origins"] = list(config.public_origins)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def _write_linux_runtime_config_atomic(path: str, content: str) -> None:
    _atomic_owner_file_write(Path(path), content, private_parent=True)


class _LinuxSetupBindingService:
    """Bind missing Linux memory authority without changing transport or host setup."""

    def __init__(
        self,
        *,
        config_path: str,
        baseline_config: WebUIRuntimeConfig,
        owner_uid: int,
        supported_profiles: set[str] | frozenset[str],
        config_loader: Callable[..., WebUIRuntimeConfig] = load_runtime_config,
        config_writer: Callable[[str, str], None] = _write_linux_runtime_config_atomic,
        database_validator: Callable[..., str] = _validate_linux_setup_database_path,
        database_inspector: Callable[..., None] = _validate_existing_windows_setup_database,
        restart_preparer: Callable[[], Any] | None = None,
    ) -> None:
        if baseline_config.memory_configured:
            raise ValueError("Linux setup binding requires an unconfigured runtime config")
        if (
            baseline_config.platform_kind is not PlatformKind.LINUX
            or baseline_config.backend_transport is not BackendTransportKind.AF_UNIX
        ):
            raise ValueError("Linux setup binding requires the Linux AF_UNIX runtime config")
        self.config_path = config_path
        self.baseline_config = baseline_config
        self.owner_uid = owner_uid
        self.supported_profiles = frozenset(supported_profiles)
        if not self.supported_profiles:
            raise ValueError("Linux setup binding requires at least one supported profile")
        self._config_loader = config_loader
        self._config_writer = config_writer
        self._database_validator = database_validator
        self._database_inspector = database_inspector
        self._restart_preparer = restart_preparer or _prepare_linux_managed_restart
        self._lock = threading.Lock()
        self._published_config: WebUIRuntimeConfig | None = None
        self._restart_plan: Any | None = None
        self._restart_request_id: str | None = None
        self._restart_handed_off = False
        self._restart_error: str | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            current = self._load_current()
            if current == self.baseline_config:
                return self._setup_required_payload()
            if self._published_config is not None and current == self._published_config:
                payload: dict[str, Any] = {
                    "status": (
                        "CONFIG_PUBLISHED_RESTART_REQUIRED"
                        if self._restart_error is not None
                        else "CONFIG_PUBLISHED_RESTART_REQUESTED"
                    ),
                    "profiles": sorted(self.supported_profiles),
                    "suggested_host_id": self._suggested_host_id(),
                    "suggested_db": self._suggested_database_path(),
                    "binding": {
                        "db": current.db,
                        "profile": current.profile,
                        "host_id": current.host_id,
                    },
                }
                if self._restart_error is not None:
                    payload["restart_error"] = self._restart_error
                return payload
            raise OwnerSetupError(
                "SETUP_CONFIG_CHANGED",
                "managed Linux runtime configuration changed while setup was active",
                status=409,
            )

    def bind(
        self,
        *,
        db: str,
        profile: str,
        host_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            self._validate_request_id(request_id)
            if profile not in self.supported_profiles:
                raise OwnerSetupError(
                    "SETUP_PROFILE_UNSUPPORTED",
                    "selected embedding profile is not supported",
                )
            if _WINDOWS_SETUP_HOST_ID_RE.fullmatch(host_id) is None:
                raise OwnerSetupError(
                    "SETUP_HOST_ID_INVALID",
                    "host ID must be a bounded non-secret identifier",
                )
            canonical_db = self._database_validator(db, owner_uid=self.owner_uid)
            self._database_inspector(canonical_db, profile, platform_name="Linux")

            current = self._load_current()
            if self._published_config is not None and current == self._published_config:
                if (
                    current.db != canonical_db
                    or current.profile != profile
                    or current.host_id != host_id
                ):
                    raise OwnerSetupError(
                        "SETUP_ALREADY_CONFIGURED",
                        "managed Linux memory binding is already published",
                        status=409,
                    )
                if self._restart_error is None:
                    return {
                        "status": "CONFIG_PUBLISHED",
                        "restart": "REQUESTED",
                        "request_id": self._restart_request_id or request_id,
                    }
                self._restart_plan = self._prepare_restart()
                self._restart_request_id = request_id
                self._restart_handed_off = False
                self._restart_error = None
                return {
                    "status": "CONFIG_PUBLISHED",
                    "restart": "REQUESTED",
                    "request_id": request_id,
                }
            if current != self.baseline_config:
                raise OwnerSetupError(
                    "SETUP_CONFIG_CHANGED",
                    "managed Linux runtime configuration changed while setup was active",
                    status=409,
                )

            restart_plan = self._prepare_restart()
            published = replace(
                current,
                db=canonical_db,
                profile=profile,
                host_id=host_id,
            )
            try:
                self._config_writer(self.config_path, _render_linux_runtime_config(published))
                observed = self._config_loader(self.config_path, owner_uid=self.owner_uid)
                if observed != published:
                    raise RuntimeConfigError("runtime config publication could not be verified")
            except Exception as exc:
                self._restore_baseline_after_publication_failure()
                raise OwnerSetupError(
                    "LINUX_CONFIG_PUBLICATION_FAILED",
                    "managed Linux runtime configuration could not be published safely",
                    status=500,
                ) from exc

            self._published_config = published
            self._restart_plan = restart_plan
            self._restart_request_id = request_id
            self._restart_handed_off = False
            self._restart_error = None
            return {
                "status": "CONFIG_PUBLISHED",
                "restart": "REQUESTED",
                "request_id": request_id,
            }

    def handoff_restart(self, request_id: str) -> bool:
        with self._lock:
            if (
                request_id != self._restart_request_id
                or self._restart_plan is None
                or self._restart_handed_off
            ):
                return False
            plan = self._restart_plan
            try:
                plan.handoff()
            except Exception:
                self._restart_error = "LINUX_RESTART_HANDOFF_FAILED"
                self._restart_plan = None
                return False
            self._restart_handed_off = True
            self._restart_plan = None
            return True

    def _prepare_restart(self) -> Any:
        try:
            return self._restart_preparer()
        except OwnerSetupError:
            raise
        except Exception as exc:
            raise OwnerSetupError(
                "LINUX_RESTART_UNAVAILABLE",
                "managed Linux restart could not be proven before configuration publication",
                status=503,
            ) from exc

    def _load_current(self) -> WebUIRuntimeConfig:
        try:
            return self._config_loader(self.config_path, owner_uid=self.owner_uid)
        except RuntimeConfigError as exc:
            raise OwnerSetupError(
                "LINUX_CONFIG_INVALID",
                "managed Linux runtime configuration could not be read safely",
                status=500,
            ) from exc
        except Exception as exc:
            raise OwnerSetupError(
                "LINUX_CONFIG_INVALID",
                "managed Linux runtime configuration could not be read safely",
                status=500,
            ) from exc

    def _restore_baseline_after_publication_failure(self) -> None:
        try:
            observed = self._config_loader(self.config_path, owner_uid=self.owner_uid)
            if observed == self.baseline_config:
                return
            self._config_writer(self.config_path, _render_linux_runtime_config(self.baseline_config))
            restored = self._config_loader(self.config_path, owner_uid=self.owner_uid)
            if restored != self.baseline_config:
                raise RuntimeError("baseline verification failed")
        except Exception as exc:
            raise OwnerSetupError(
                "SETUP_CONFIG_RECOVERY_REQUIRED",
                "managed Linux runtime configuration could not be restored after publication failure",
                status=500,
            ) from exc

    def _setup_required_payload(self) -> dict[str, Any]:
        return {
            "status": "SETUP_REQUIRED",
            "profiles": sorted(self.supported_profiles),
            "suggested_host_id": self._suggested_host_id(),
            "suggested_db": self._suggested_database_path(),
        }

    def _suggested_host_id(self) -> str:
        hostname = urlsplit(self.baseline_config.origin).hostname
        if hostname is None or not hostname.startswith("factlane-") or not hostname.endswith(".test"):
            return ""
        installation_id = hostname[len("factlane-") : -len(".test")]
        if _WINDOWS_SETUP_INSTALLATION_ID_RE.fullmatch(installation_id) is None:
            return ""
        suggested = f"owner-{installation_id}"
        return suggested if _WINDOWS_SETUP_HOST_ID_RE.fullmatch(suggested) is not None else ""

    def _suggested_database_path(self) -> str:
        return str(Path(self.baseline_config.state_dir) / "memory.db")

    @staticmethod
    def _validate_request_id(request_id: str) -> None:
        if _WINDOWS_SETUP_REQUEST_ID_RE.fullmatch(request_id) is None:
            raise OwnerSetupError(
                "SETUP_REQUEST_ID_INVALID",
                "setup request ID is invalid",
            )


class _LiveServiceControl:
    """Bounded control for the native socket owned by a running WebUI instance."""

    def __init__(self, service_state: WebUIServiceState) -> None:
        self.service_state = service_state

    def status(self) -> ServiceSnapshot:
        return self.service_state.snapshot()

    def start(self, request_id: str) -> ServiceSnapshot:
        snapshot = self.service_state.snapshot()
        if snapshot.status not in {ServiceStatus.STARTING, ServiceStatus.READY}:
            raise RuntimeError("offline WebUI start is owned by the platform service controller")
        # The process is already running.  Do not claim a new process start or clear
        # a stop/recovery boundary from this in-process control socket.
        del request_id
        return snapshot

    def stop(self, request_id: str) -> ServiceSnapshot:
        return self.service_state.request_stop(request_id)

    def restart(self, request_id: str) -> ServiceSnapshot:
        del request_id
        raise RuntimeError("WebUI restart is owned by the platform service controller")


class OwnerRuntimeBackend:
    """One private Owner HTTP/native-control runtime behind the local TLS gateway."""

    def __init__(
        self,
        *,
        service_state: WebUIServiceState,
        auth_state: OwnerAuthState,
        http_server: OwnerHTTPServer,
        native_server: Any,
        adapter: MemoryAdapter | None,
        runtime_registry: RuntimeRegistry | None,
        maintenance_coordinator: Any | None,
        owner_adapter_participant: _OwnerAdapterMaintenanceParticipant | None = None,
        owner_adapter_executor: _OwnerAdapterExecutor | None = None,
        database_service: OwnerDatabaseService | None = None,
        full_restore_service: FullRestoreService | None = None,
        readiness_probe: Callable[[], bool] | None = None,
    ) -> None:
        self.service_state = service_state
        self.auth_state = auth_state
        self.http_server = http_server
        self.native_server = native_server
        self._adapter = adapter
        self.owner_adapter_participant = owner_adapter_participant
        self.owner_adapter_executor = owner_adapter_executor
        self.runtime_registry = runtime_registry
        self.maintenance_coordinator = maintenance_coordinator
        self.database_service = database_service
        self.full_restore_service = full_restore_service
        self._readiness_probe = readiness_probe
        self._http_thread: threading.Thread | None = None
        self._native_thread: threading.Thread | None = None
        self._draining = False
        self._closed = False

    @property
    def adapter(self) -> MemoryAdapter | None:
        if self.owner_adapter_participant is not None:
            return self.owner_adapter_participant.adapter
        return self._adapter

    def start(self) -> None:
        if self._closed or self._http_thread is not None or self._native_thread is not None:
            raise RuntimeError("Owner WebUI backend is already started or closed")
        self._native_thread = threading.Thread(
            target=self.native_server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="factlane-owner-native-control",
            daemon=False,
        )
        self._http_thread = threading.Thread(
            target=self.http_server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="factlane-owner-http",
            daemon=False,
        )
        self._native_thread.start()
        self._http_thread.start()

    def is_ready(self) -> bool:
        structurally_ready = (
            not self._draining
            and not self._closed
            and self._http_thread is not None
            and self._native_thread is not None
            and self._http_thread.is_alive()
            and self._native_thread.is_alive()
            and self.http_server.service_loop_thread_alive
        )
        if not structurally_ready:
            return False
        if self._readiness_probe is None:
            return True
        try:
            return bool(self._readiness_probe())
        except Exception:
            return False

    def is_alive(self) -> bool:
        return (
            not self._closed
            and self._http_thread is not None
            and self._native_thread is not None
            and self._http_thread.is_alive()
            and self._native_thread.is_alive()
            and self.http_server.service_loop_thread_alive
        )

    def begin_drain(self) -> None:
        if self._closed or self._draining:
            return
        self._draining = True
        if self._native_thread is not None and self._native_thread.is_alive():
            self.native_server.shutdown()
        if self._http_thread is not None and self._http_thread.is_alive():
            self.http_server.shutdown()

    def wait_stopped(self, timeout: float) -> bool:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
            raise ValueError("timeout must be a non-negative number")
        if self._closed:
            return True
        deadline = time.monotonic() + float(timeout)
        for thread in (self._native_thread, self._http_thread):
            if thread is None:
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread is not None and thread.is_alive() for thread in (self._native_thread, self._http_thread)):
            return False
        cleanup_error: BaseException | None = None
        try:
            self.native_server.server_close()
        except BaseException as exc:  # preserve HTTP cleanup attempt too
            cleanup_error = exc
        try:
            self.http_server.server_close()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        self._closed = True
        if cleanup_error is not None:
            raise cleanup_error
        return True


def _database_runtime_roots(spec: BackendLaunchSpec, database_path: str) -> tuple[Path, Path]:
    canonical = os.path.realpath(os.path.abspath(database_path))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return (
        Path(spec.runtime_dir) / digest[:16],
        Path(spec.state_dir) / "maintenance" / digest[:32],
    )


def _is_windows_backend_spec(spec: BackendLaunchSpec) -> bool:
    if spec.platform_kind is PlatformKind.WINDOWS:
        if spec.backend_transport is not BackendTransportKind.WINDOWS_HTTPS:
            raise ValueError("Windows Owner backend requires WINDOWS_HTTPS transport")
        return True
    if spec.platform_kind is not PlatformKind.LINUX or spec.backend_transport is not BackendTransportKind.AF_UNIX:
        raise ValueError("Linux Owner backend requires AF_UNIX transport")
    return False


def _prove_exclusion(database_path: str) -> bool:
    """Use the same exclusive maintenance lease as recovery; never synthesize proof."""

    with MaintenanceLease(database_path, f"webui-exclusion-{uuid.uuid4()}"):
        return True


def _prove_windows_exclusion(database_path: str) -> bool:
    lease = WindowsInPlaceMaintenanceLease.acquire(database_path)
    try:
        lease.revalidate()
        lease.release_unmutated()
        return True
    except BaseException:
        lease.retain_fail_closed()
        raise


def _windows_process_start_provider(pid: int) -> int | None:
    try:
        return windows_process_identity(pid).creation_time_us
    except WindowsSecurityError:
        return None


def _runtime_control_client_for_spec(
    spec: BackendLaunchSpec,
    registry: RuntimeRegistry,
    *,
    owner_sid: str | None = None,
) -> Any:
    if _is_windows_backend_spec(spec):
        sid = current_user_sid() if owner_sid is None else owner_sid
        return WindowsRuntimeControlClient(registry, owner_sid=sid)
    return RuntimeControlClient(registry)


def _build_maintenance(
    spec: BackendLaunchSpec,
    database_path: str,
    *,
    owner_uid: int,
) -> tuple[RuntimeRegistry, MaintenanceCoordinator]:
    runtime_root, barrier_root = _database_runtime_roots(spec, database_path)
    windows_backend = _is_windows_backend_spec(spec)
    owner_sid: str | None = None
    if windows_backend:
        owner_sid = current_user_sid()
        registry = RuntimeRegistry(
            runtime_root,
            owner_uid=0,
            boot_id_provider=windows_boot_id,
            process_start_provider=_windows_process_start_provider,
        )
    else:
        registry = RuntimeRegistry(runtime_root, owner_uid=owner_uid)
    barrier = MaintenanceBarrierStore(barrier_root, database_path)
    initialize_barrier_for_startup(barrier, generation=1)
    runtime_client = _runtime_control_client_for_spec(spec, registry, owner_sid=owner_sid)
    coordinator = MaintenanceCoordinator(
        barrier=barrier,
        registry=registry,
        call_runtime=runtime_client.call,
        exclusion_check=_prove_windows_exclusion if windows_backend else _prove_exclusion,
    )
    return registry, coordinator


def _validate_normal_spec(spec: BackendLaunchSpec) -> tuple[str, str, str]:
    if spec.mode is not BackendMode.NORMAL:
        raise ValueError("normal adapter authority is valid only in NORMAL backend mode")
    values = (spec.db, spec.profile, spec.host_id)
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("NORMAL backend requires database, profile and host identity from runtime config")
    assert spec.db is not None and spec.profile is not None and spec.host_id is not None
    if not os.path.isabs(spec.db):
        raise ValueError("runtime database path must be absolute")
    return os.path.realpath(os.path.abspath(spec.db)), spec.profile, spec.host_id


def _validate_recovery_spec(spec: BackendLaunchSpec) -> tuple[str, str, str]:
    if spec.mode is not BackendMode.RECOVERY_SHELL:
        raise ValueError("recovery database authority is valid only in RECOVERY_SHELL backend mode")
    values = (spec.db, spec.profile, spec.host_id)
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("RECOVERY_SHELL requires database, profile and host identity from runtime config")
    assert spec.db is not None and spec.profile is not None and spec.host_id is not None
    if not os.path.isabs(spec.db):
        raise ValueError("runtime database path must be absolute")
    return os.path.realpath(os.path.abspath(spec.db)), spec.profile, spec.host_id


def _owner_capabilities_for_spec(spec: BackendLaunchSpec) -> dict[str, bool] | None:
    if not _is_windows_backend_spec(spec):
        return None
    normal = spec.mode is BackendMode.NORMAL and spec.normal_adapter_required
    return {
        "database_management": normal,
        "full_restore": normal,
        "permanent_purge": normal,
    }


def _compose_database_authority(
    *,
    spec: BackendLaunchSpec,
    database_path: str,
    profile_name: str,
    auth_state: OwnerAuthState,
    service_state: WebUIServiceState,
    runtime_registry: RuntimeRegistry,
    coordinator: Any,
    adapter_executor: _OwnerAdapterExecutor | None,
) -> tuple[OwnerDatabaseService, FullRestoreService, OwnerDestructiveOrchestrator]:
    windows_backend = _is_windows_backend_spec(spec)
    artifacts_root, restore_root, purge_root = _database_state_roots(spec, database_path)
    artifact_registry = ArtifactRegistry(artifacts_root)
    embedding_profile = _configured_embedding_profile(profile_name)
    runtime_client = _runtime_control_client_for_spec(spec, runtime_registry)
    postflight = _make_restore_postflight(database_path, embedding_profile)
    post_activation_verify = _make_post_activation_verifier(
        database_path,
        runtime_registry,
        runtime_client,
    )
    restore = FullRestoreService(
        active_db=database_path,
        registry=artifact_registry,
        state_dir=restore_root,
        service_state=service_state,
        coordinator=coordinator,
        postflight=postflight,
        invalidate_sessions=auth_state.invalidate_all_sessions_after_maintenance,
        post_activation_verify=post_activation_verify,
        windows_restore_operator_factory=WindowsOwnerRestoreOperator if windows_backend else None,
    )

    async def restore_preview(binding: dict[str, Any]) -> dict[str, Any]:
        artifact_id = binding.get("artifact_id")
        trusted = binding.get("trusted_origin_attested")
        if not isinstance(artifact_id, str) or trusted is not True:
            raise AdapterError("RESTORE_PREVIEW_INVALID", "restore preview binding is invalid")
        preview = restore.preview(artifact_id=artifact_id, trusted_origin_attested=True)
        if (
            preview.get("artifact_sha256") != binding.get("artifact_sha256")
            or preview.get("artifact_size_bytes") != binding.get("artifact_size_bytes")
        ):
            raise AdapterError("JOB_BINDING_CHANGED", "restore artifact binding changed before preview")
        return preview

    async def restore_apply(
        binding: dict[str, Any],
        preview: dict[str, Any],
        authorize: Callable[[str], Any | Awaitable[Any]],
    ) -> dict[str, Any]:
        if (
            binding.get("artifact_id") != preview.get("artifact_id")
            or binding.get("artifact_sha256") != preview.get("artifact_sha256")
            or binding.get("artifact_size_bytes") != preview.get("artifact_size_bytes")
            or binding.get("trusted_origin_attested") is not True
            or binding.get("state_restoration_acknowledged") is not True
        ):
            raise AdapterError("RESTORE_PREVIEW_INVALID", "restore execution binding does not match its preview")
        try:
            return await restore.execute(
                preview=preview,
                operation_id=str(binding["operation_id"]),
                authorize=authorize,
            )
        except RestoreRecoveryRequired as exc:
            raise AdapterError("RESTORE_RECOVERY_REQUIRED", exc.safe_message) from exc

    producer = {
        "factlane_version": importlib_metadata.version("factlane"),
        "storage_contract_version": STORAGE_CONTRACT_VERSION,
        "embedding_profile_id": profile_name,
    }

    backup_create = None
    logical_export_selector = None
    logical_import_preview = None
    logical_import_apply = None
    if adapter_executor is not None:

        async def backup_create(active_db: Path, registry: ArtifactRegistry, artifact_id: str) -> dict[str, Any]:
            if active_db != Path(database_path) or registry is not artifact_registry:
                raise AdapterError("DATABASE_SERVICE_CONFIG_INVALID", "backup execution escaped trusted database binding")

            async def execute(_adapter: MemoryAdapter) -> dict[str, Any]:
                return await asyncio.to_thread(
                    create_consistent_backup,
                    active_db,
                    registry,
                    artifact_id=artifact_id,
                )

            return await adapter_executor.run(execute)

        async def logical_export_selector(selection: dict[str, Any]) -> list[dict[str, Any]]:
            required = {
                "scope",
                "project_id",
                "worktree_id",
                "workflow_id",
                "agent_id",
                "lifecycle_states",
                "max_records",
            }
            if not isinstance(selection, dict) or set(selection) != required:
                raise AdapterError("LOGICAL_EXPORT_SELECTION_INVALID", "logical export selection fields are not exact")
            scope = validate_scope(
                selection["scope"],
                selection["project_id"],
                selection["worktree_id"],
                selection["workflow_id"],
                selection["agent_id"],
            )
            states = selection["lifecycle_states"]
            maximum = selection["max_records"]
            if (
                not isinstance(states, list)
                or not states
                or len(set(states)) != len(states)
                or any(not isinstance(state, str) or state not in LOGICAL_EXPORT_LIFECYCLE_STATES for state in states)
                or isinstance(maximum, bool)
                or not isinstance(maximum, int)
                or not 1 <= maximum <= 1000
            ):
                raise AdapterError("LOGICAL_EXPORT_SELECTION_INVALID", "logical export selection is invalid")

            async def execute(adapter: MemoryAdapter) -> list[dict[str, Any]]:
                return await adapter.logical_export_records(
                    scope,
                    lifecycle_states=list(states),
                    max_records=maximum,
                )

            return await adapter_executor.run(execute)

        async def logical_import_preview(payload: bytes, binding: dict[str, Any]) -> dict[str, Any]:
            if hashlib.sha256(payload).hexdigest() != binding.get("artifact_sha256"):
                raise AdapterError("JOB_BINDING_CHANGED", "logical import payload digest changed before preview")

            async def execute(adapter: MemoryAdapter) -> dict[str, Any]:
                async def destination_probe(request: dict[str, Any]) -> dict[str, Any]:
                    raw_scope = request.get("scope")
                    if not isinstance(raw_scope, dict) or "scope" not in raw_scope:
                        raise AdapterError("DESTINATION_PROBE_INVALID", "logical import destination scope is invalid")
                    scope = validate_scope(
                        raw_scope["scope"],
                        raw_scope.get("project_id"),
                        raw_scope.get("worktree_id"),
                        raw_scope.get("workflow_id"),
                        raw_scope.get("agent_id"),
                    )
                    exact = await adapter.engine.find_exact(request["native_content_hash"])
                    contradictions = await adapter.engine.find_contradictions(request["contradiction_key"], scope)
                    return {"exact": exact, "contradictions": contradictions}

                preview = await probe_logical_import_destination(payload, destination_probe=destination_probe)
                return _safe_logical_import_preview(preview)

            return await adapter_executor.run(execute)

        async def logical_import_apply(payload: bytes, binding: dict[str, Any]) -> dict[str, Any]:
            if hashlib.sha256(payload).hexdigest() != binding.get("artifact_sha256"):
                raise AdapterError("JOB_BINDING_CHANGED", "logical import payload digest changed before apply")
            operation_id = binding.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                raise AdapterError("JOB_BINDING_INVALID", "logical import operation identity is invalid")

            async def execute(adapter: MemoryAdapter) -> dict[str, Any]:
                result = await apply_logical_import_to_adapter(
                    payload,
                    operation_id=operation_id,
                    adapter=adapter,
                )
                return _safe_logical_import_apply(result)

            return await adapter_executor.run(execute)

    database = OwnerDatabaseService(
        artifacts_root,
        artifact_registry=artifact_registry,
        active_db_path=database_path if adapter_executor is not None else None,
        backup_create=backup_create,
        logical_export_selector=logical_export_selector,
        logical_export_producer=producer if logical_export_selector is not None else None,
        logical_import_preview=logical_import_preview,
        logical_import_apply=logical_import_apply,
        restore_preview=restore_preview,
        restore_apply=restore_apply,
    )

    if adapter_executor is not None:

        async def target_resolver(identities: tuple[PurgeTargetIdentity, ...]) -> tuple[RecoveryTarget, ...]:
            storage_identities = [
                {
                    "scope": ScopeContext(
                        identity.scope.scope,
                        project_id=identity.scope.project_id,
                        worktree_id=identity.scope.worktree_id,
                        workflow_id=identity.scope.workflow_id,
                        agent_id=identity.scope.agent_id,
                    ),
                    "memory_id": identity.memory_id,
                    "record_id": identity.record_id,
                    "revision": identity.revision,
                }
                for identity in identities
            ]

            async def execute(adapter: MemoryAdapter) -> tuple[RecoveryTarget, ...]:
                rows = await adapter.resolve_permanent_purge_targets(storage_identities)
                return _governed_purge_targets(identities, rows)

            return await adapter_executor.run(execute)

    else:

        async def target_resolver(_identities: tuple[PurgeTargetIdentity, ...]) -> tuple[RecoveryTarget, ...]:
            raise AdapterError(
                "BACKEND_UNAVAILABLE",
                "purge preview requires a reopened governed Owner adapter",
            )

    async def post_close_target_resolver(
        operation_id: str,
        identities: tuple[PurgeTargetIdentity, ...],
    ) -> tuple[RecoveryTarget, ...]:
        resolver = (
            resolve_windows_permanent_purge_targets_exclusive
            if windows_backend
            else resolve_permanent_purge_targets_exclusive
        )
        return await resolver(
            operation_id=operation_id,
            db_path=database_path,
            profile=embedding_profile,
            identities=[identity.to_mapping() for identity in identities],
        )

    destructive = OwnerDestructiveOrchestrator(
        db_path=database_path,
        profile=embedding_profile,
        target_resolver=target_resolver,
        post_close_target_resolver=post_close_target_resolver,
        require_post_close_target_resolver=True,
        coordinator=coordinator,
        recovery_operator=WindowsOwnerPurgeOperator() if windows_backend else SensitiveMemoryRecoveryOperator(),
        service_state=service_state,
        state_root=purge_root,
        retained_identity_fields_classified_non_sensitive=True,
    )
    return database, restore, destructive


def create_owner_runtime_backend(
    spec: BackendLaunchSpec,
    service_state: WebUIServiceState,
    *,
    adapter_factory: AdapterFactory = MemoryAdapter.create,
    native_confirmation: NativeConfirmation | None = None,
    owner_uid: int | None = None,
) -> OwnerRuntimeBackend:
    """Compose the production Owner backend from trusted runtime configuration only."""

    if not isinstance(spec, BackendLaunchSpec):
        raise TypeError("spec must be a BackendLaunchSpec")
    if not isinstance(service_state, WebUIServiceState):
        raise TypeError("service_state must be the runtime WebUIServiceState")
    windows_backend = _is_windows_backend_spec(spec)
    uid = 0 if windows_backend else (os.getuid() if owner_uid is None else owner_uid)
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise ValueError("owner_uid must be a non-negative integer")
    owner_sid: str | None = None
    owner_paths: Any | None = None
    setup_service: _WindowsSetupBindingService | _LinuxSetupBindingService | None = None
    readiness_probe: Callable[[], bool] | None = None
    http_server: OwnerHTTPServer | None = None
    adapter: MemoryAdapter | None = None
    registry: RuntimeRegistry | None = None
    coordinator: Any | None = None
    owner_adapter_participant: _OwnerAdapterMaintenanceParticipant | None = None
    owner_adapter_executor: _OwnerAdapterExecutor | None = None
    database_service: OwnerDatabaseService | None = None
    full_restore_service: FullRestoreService | None = None
    native_server: Any | None = None
    try:
        if windows_backend:
            if spec.socket_path is not None:
                raise ValueError("Windows Owner backend must not configure an AF_UNIX socket path")
            owner_sid = current_user_sid()
            owner_paths = windows_owner_paths(owner_sid=owner_sid)
            if spec.mode is BackendMode.SETUP_SHELL:
                managed = load_windows_managed_config(
                    owner_paths.service_config_path,
                    owner_sid=owner_sid,
                )
                if managed is None:
                    raise ValueError("Windows SETUP_SHELL requires the managed configuration created by setup")
                if any(value is not None for value in (managed.db, managed.profile, managed.host_id)):
                    raise ValueError("Windows SETUP_SHELL managed configuration must remain unbound")
                if (
                    managed.origin != spec.expected_origin
                    or managed.runtime_dir != spec.runtime_dir
                    or managed.state_dir != spec.state_dir
                    or managed.native_control_pipe != owner_paths.native_control_socket
                    or managed.browser_profile_dir != owner_paths.browser_profile_dir
                ):
                    raise ValueError("Windows SETUP_SHELL managed configuration differs from runtime authority")
                setup_service = _WindowsSetupBindingService(
                    config_path=owner_paths.service_config_path,
                    baseline_config=managed,
                    owner_sid=owner_sid,
                    supported_profiles=set(PROFILE_DEFINITIONS),
                )
            parsed = urlsplit(spec.expected_origin)
            hostname = parsed.hostname
            if hostname is None:
                raise ValueError("Windows Owner backend origin must contain a hostname")
            tls_material = load_windows_tls_material(
                Path(spec.state_dir) / "tls",
                hostname,
                owner_sid=owner_sid,
            )
            server_tls_context = build_windows_server_ssl_context(tls_material)
            client_tls_context = build_windows_client_ssl_context(tls_material)
            confirmer = native_confirmation or _WindowsNativeConfirmation()
        else:
            if not isinstance(spec.socket_path, str) or not spec.socket_path:
                raise ValueError("Linux Owner backend requires an AF_UNIX socket path")
            if spec.mode is BackendMode.SETUP_SHELL:
                config_path = default_runtime_config_path()
                baseline = load_runtime_config(config_path, owner_uid=uid)
                if baseline.memory_configured:
                    raise ValueError("Linux SETUP_SHELL runtime configuration must remain unbound")
                if (
                    baseline.origin != spec.expected_origin
                    or baseline.backend_socket != spec.socket_path
                    or baseline.runtime_dir != spec.runtime_dir
                    or baseline.state_dir != spec.state_dir
                    or baseline.platform_kind is not PlatformKind.LINUX
                    or baseline.backend_transport is not BackendTransportKind.AF_UNIX
                ):
                    raise ValueError("Linux SETUP_SHELL runtime configuration differs from runtime authority")
                setup_service = _LinuxSetupBindingService(
                    config_path=str(config_path),
                    baseline_config=baseline,
                    owner_uid=uid,
                    supported_profiles=set(PROFILE_DEFINITIONS),
                )
            confirmer = native_confirmation or ZenityNativeConfirmation()

        auth_state = OwnerAuthState(owner_uid=uid, native_confirmation=confirmer)
        service_control = _LiveServiceControl(service_state)
        if windows_backend:
            http_server = WindowsOwnerHTTPServer(
                origin=spec.expected_origin,
                public_origins=spec.public_origins,
                tls_context=server_tls_context,
                auth_state=auth_state,
                overview_service=None,
                control_service=None,
                operations_service=None,
                service_control=service_control,
                destructive_service=None,
                database_service=None,
                setup_service=setup_service,
                capabilities=_owner_capabilities_for_spec(spec),
            )
            readiness_probe = lambda: http_server.probe_ready(
                trust_context=client_tls_context,
                timeout_seconds=1.0,
            )
        else:
            http_server = OwnerHTTPServer(
                socket_path=spec.socket_path,
                origin=spec.expected_origin,
                public_origins=spec.public_origins,
                auth_state=auth_state,
                overview_service=None,
                control_service=None,
                operations_service=None,
                service_control=service_control,
                destructive_service=None,
                database_service=None,
                setup_service=setup_service,
            )

        if spec.normal_adapter_required:
            database_path, profile, host_id = _validate_normal_spec(spec)
            owner_context = trusted_write_context_for_profile(
                "owner-current",
                contributor_ref=host_id,
                grant_ref=f"launcher:owner-current:{host_id}",
            )
            launch = _OwnerAdapterLaunchConfig(
                database_path=database_path,
                profile=profile,
                host_id=host_id,
                trusted_write_context=owner_context,
            )
            adapter = http_server.run_service_coroutine(
                lambda: adapter_factory(
                    launch.database_path,
                    launch.profile,
                    runtime_agent_id=launch.host_id,
                    trusted_write_context=launch.trusted_write_context,
                )
            )
            if not isinstance(adapter, MemoryAdapter):
                raise TypeError("adapter_factory must return a governed MemoryAdapter")
            gate = OwnerAdapterMaintenanceGate()
            owner_adapter_participant = _OwnerAdapterMaintenanceParticipant(
                adapter=adapter,
                adapter_factory=adapter_factory,
                launch=launch,
                gate=gate,
            )
            http_server.run_service_coroutine(owner_adapter_participant.initialize)
            # Cleanup always follows the current adapter instance, including a reopened
            # replacement, and executes on this same Owner HTTP service loop.
            http_server.register_async_cleanup(owner_adapter_participant.close_current)
            # MemoryAdapter.create owns first-open database creation.  Build inode-
            # bound maintenance state only after that exact database exists.
            registry, base_coordinator = _build_maintenance(spec, database_path, owner_uid=uid)
            control = OwnerControlService(adapter, maintenance_gate=gate)
            overview = OwnerOverviewService(control)
            operations = OwnerOperationsService(
                control,
                _RegistryRuntimeObserver(registry, database_path),
                _UnconfiguredOperationsConfig(),
            )
            owner_adapter_participant.bind_services(control, overview, operations)
            coordinator = _OwnerAdapterMaintenanceCoordinator(base_coordinator, owner_adapter_participant)
            owner_adapter_executor = _OwnerAdapterExecutor(owner_adapter_participant, gate)
            database_service, full_restore_service, destructive = _compose_database_authority(
                spec=spec,
                database_path=database_path,
                profile_name=profile,
                auth_state=auth_state,
                service_state=service_state,
                runtime_registry=registry,
                coordinator=coordinator,
                adapter_executor=owner_adapter_executor,
            )
            http_server.configure_services(
                overview_service=overview,
                control_service=control,
                operations_service=operations,
                destructive_service=destructive,
                database_service=database_service,
            )
        elif spec.mode is BackendMode.RECOVERY_SHELL:
            database_path, profile, _host_id = _validate_recovery_spec(spec)
            if not os.path.isfile(database_path):
                raise AdapterError("BACKEND_UNAVAILABLE", "RECOVERY_SHELL configured database is unavailable")
            registry, coordinator = _build_maintenance(spec, database_path, owner_uid=uid)
            database_service, full_restore_service, destructive = _compose_database_authority(
                spec=spec,
                database_path=database_path,
                profile_name=profile,
                auth_state=auth_state,
                service_state=service_state,
                runtime_registry=registry,
                coordinator=coordinator,
                adapter_executor=None,
            )
            http_server.configure_services(
                overview_service=None,
                control_service=None,
                operations_service=None,
                destructive_service=destructive,
                database_service=database_service,
            )

        if windows_backend:
            assert owner_sid is not None
            assert owner_paths is not None
            native_server = WindowsOwnerNativeControlServer(
                pipe_name=owner_paths.native_control_socket,
                owner_uid=0,
                auth_state=auth_state,
                service_control=service_control,
                owner_sid=owner_sid,
            )
        else:
            native_server = OwnerNativeControlServer(
                socket_path=Path(spec.runtime_dir) / "owner-control.sock",
                owner_uid=uid,
                auth_state=auth_state,
                service_control=service_control,
            )
        return OwnerRuntimeBackend(
            service_state=service_state,
            auth_state=auth_state,
            http_server=http_server,
            native_server=native_server,
            adapter=adapter,
            runtime_registry=registry,
            maintenance_coordinator=coordinator,
            owner_adapter_participant=owner_adapter_participant,
            owner_adapter_executor=owner_adapter_executor,
            database_service=database_service,
            full_restore_service=full_restore_service,
            readiness_probe=readiness_probe,
        )
    except BaseException:
        if native_server is not None:
            native_server.server_close()
        if http_server is not None:
            http_server.server_close()
        raise
