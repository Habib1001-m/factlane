from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
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
from .maintenance_coordinator import (
    MaintenanceBarrierStore,
    MaintenanceCoordinator,
    MaintenanceCoordinatorError,
    ResumeReport,
)
from .owner_auth import NativeConfirmation, OwnerAuthState
from .owner_control import OwnerAdapterMaintenanceGate, OwnerControlService
from .owner_database import (
    ArtifactRegistry,
    apply_logical_import_to_adapter,
    create_consistent_backup,
    probe_logical_import_destination,
)
from .owner_database_service import OwnerDatabaseService
from .owner_destructive import OwnerDestructiveOrchestrator, PurgeTargetIdentity
from .owner_http import OwnerHTTPServer
from .owner_native_control import OwnerNativeControlServer, ZenityNativeConfirmation
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
)
from .storage import LOGICAL_EXPORT_LIFECYCLE_STATES, STORAGE_CONTRACT_VERSION, SQLiteVecEngine
from .webui_runtime import BackendLaunchSpec, BackendMode, WebUIRuntimeConfig, caddy_command
from .webui_service import ServiceSnapshot, ServiceStatus, WebUIServiceState


AdapterFactory = Callable[..., Awaitable[MemoryAdapter]]
_CADDY_READINESS_CONNECT_TIMEOUT_SECONDS = 0.2


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
        if failure_code is not None:
            return ResumeReport(
                ready_instance_ids=report.ready_instance_ids,
                failures=report.failures,
                participant_failures={"owner_adapter": failure_code},
            )
        return ResumeReport(
            ready_instance_ids=report.ready_instance_ids,
            failures=report.failures,
            participant_failures={},
        )

    async def publish_activation_and_resume(self, prepared, *, new_generation: int) -> ResumeReport:
        self.begin_activation(prepared, new_generation=new_generation)
        self.publish_running(prepared, new_generation=new_generation)
        return await self.resume_running(prepared, new_generation=new_generation)

    async def resume_activating(self, prepared, *, new_generation: int):
        return await self._delegate.resume_activating(prepared, new_generation=new_generation)

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
    def __init__(self, process: Any, *, host: str, port: int) -> None:
        self._process = process
        self._host = host
        self._port = port

    def poll(self) -> int | None:
        return self._process.poll()

    def is_ready(self) -> bool:
        if self.poll() is not None:
            return False
        try:
            connection = socket.create_connection(
                (self._host, self._port),
                timeout=_CADDY_READINESS_CONNECT_TIMEOUT_SECONDS,
            )
        except OSError:
            return False
        try:
            return self.poll() is None
        finally:
            try:
                connection.close()
            except OSError:
                pass

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
    )


def create_caddy_process(
    config: WebUIRuntimeConfig,
    argv: tuple[str, ...],
) -> _CaddySubprocess:
    """Start the exact configured Caddy argv and prove its saved TCP listener before readiness."""

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
    return _CaddySubprocess(process, host=host, port=port)


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
        native_server: OwnerNativeControlServer,
        adapter: MemoryAdapter | None,
        runtime_registry: RuntimeRegistry | None,
        maintenance_coordinator: Any | None,
        owner_adapter_participant: _OwnerAdapterMaintenanceParticipant | None = None,
        owner_adapter_executor: _OwnerAdapterExecutor | None = None,
        database_service: OwnerDatabaseService | None = None,
        full_restore_service: FullRestoreService | None = None,
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
        return (
            not self._draining
            and not self._closed
            and self._http_thread is not None
            and self._native_thread is not None
            and self._http_thread.is_alive()
            and self._native_thread.is_alive()
            and self.http_server.service_loop_thread_alive
        )

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


def _prove_exclusion(database_path: str) -> bool:
    """Use the same exclusive maintenance lease as recovery; never synthesize proof."""

    with MaintenanceLease(database_path, f"webui-exclusion-{uuid.uuid4()}"):
        return True


def _build_maintenance(
    spec: BackendLaunchSpec,
    database_path: str,
    *,
    owner_uid: int,
) -> tuple[RuntimeRegistry, MaintenanceCoordinator]:
    runtime_root, barrier_root = _database_runtime_roots(spec, database_path)
    registry = RuntimeRegistry(runtime_root, owner_uid=owner_uid)
    barrier = MaintenanceBarrierStore(barrier_root, database_path)
    barrier.initialize(generation=1)
    coordinator = MaintenanceCoordinator(
        barrier=barrier,
        registry=registry,
        call_runtime=RuntimeControlClient(registry).call,
        exclusion_check=_prove_exclusion,
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
    artifacts_root, restore_root, purge_root = _database_state_roots(spec, database_path)
    artifact_registry = ArtifactRegistry(artifacts_root)
    embedding_profile = _configured_embedding_profile(profile_name)
    runtime_client = RuntimeControlClient(runtime_registry)
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
        return await resolve_permanent_purge_targets_exclusive(
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
        recovery_operator=SensitiveMemoryRecoveryOperator(),
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
    uid = os.getuid() if owner_uid is None else owner_uid
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise ValueError("owner_uid must be a non-negative integer")

    auth_state = OwnerAuthState(
        owner_uid=uid,
        native_confirmation=native_confirmation or ZenityNativeConfirmation(),
    )
    http_server = OwnerHTTPServer(
        socket_path=spec.socket_path,
        origin=spec.expected_origin,
        auth_state=auth_state,
        overview_service=None,
        control_service=None,
        operations_service=None,
        destructive_service=None,
        database_service=None,
    )
    adapter: MemoryAdapter | None = None
    registry: RuntimeRegistry | None = None
    coordinator: Any | None = None
    owner_adapter_participant: _OwnerAdapterMaintenanceParticipant | None = None
    owner_adapter_executor: _OwnerAdapterExecutor | None = None
    database_service: OwnerDatabaseService | None = None
    full_restore_service: FullRestoreService | None = None
    native_server: OwnerNativeControlServer | None = None
    try:
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

        native_server = OwnerNativeControlServer(
            socket_path=Path(spec.runtime_dir) / "owner-control.sock",
            owner_uid=uid,
            auth_state=auth_state,
            service_control=_LiveServiceControl(service_state),
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
        )
    except BaseException:
        if native_server is not None:
            native_server.server_close()
        http_server.server_close()
        raise
