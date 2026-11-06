from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter, _establish_embedding_compatibility, trusted_write_context_for_profile
from factlane.contract import ScopeContext
from factlane.embeddings import EmbeddingProfile
from factlane.maintenance_coordinator import (
    MaintenanceBarrierStore,
    MaintenanceCoordinatorError,
    PreparedMaintenance,
    ResumeReport,
)
from factlane.owner_destructive import (
    BACKUP_RETENTION_WARNING,
    OwnerDestructiveError,
    OwnerDestructiveOrchestrator,
    PurgeTargetIdentity,
)
from factlane.recovery import (
    PM1,
    RecoveryActivationHooks,
    RecoveryHold,
    RecoveryResult,
    RecoveryScope,
    RecoveryTarget,
    SensitiveMemoryRecoveryOperator,
    resolve_permanent_purge_targets_exclusive,
)
from factlane.storage import SQLiteVecEngine
from factlane.webui_service import ServiceStatus, WebUIServiceState


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="test-256",
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


def _target(*, revision: int = 1, lifecycle: str = "VALIDATED_CURRENT", native_hash: str = "a" * 64) -> RecoveryTarget:
    return RecoveryTarget(
        record_id="22222222-2222-4222-8222-222222222222",
        memory_id="11111111-1111-4111-8111-111111111111",
        revision=revision,
        scope=RecoveryScope("PROJECT", project_id="factlane"),
        expected_lifecycle=lifecycle,
        expected_materialization=PM1,
        expected_native_hash=native_hash,
    )


def _identity(target: RecoveryTarget | None = None) -> PurgeTargetIdentity:
    item = target or _target()
    return PurgeTargetIdentity(
        scope=item.scope,
        memory_id=item.memory_id,
        record_id=item.record_id,
        revision=item.revision,
    )


@dataclass
class _ServiceState:
    calls: list[tuple[str, str | None]]
    recovery_required: bool = False

    def begin_maintenance(self, operation_id: str) -> None:
        self.calls.append(("service-begin", operation_id))

    def finish_maintenance(self, operation_id: str) -> None:
        self.calls.append(("service-finish", operation_id))

    def mark_recovery_required(self) -> None:
        self.recovery_required = True
        self.calls.append(("service-recovery", None))

    def clear_recovery_required(self) -> None:
        self.recovery_required = False
        self.calls.append(("service-clear-recovery", None))


class _Coordinator:
    def __init__(self, calls: list[tuple[str, str | None]], *, shared: dict[str, Any] | None = None) -> None:
        self.calls = calls
        self.shared = shared if shared is not None else {"state": "RUNNING", "generation": 7, "prepare_calls": 0}
        self.fail_publish = False
        self.resume_failures: dict[str, str] = {}
        self.participant_failures: dict[str, str] = {}
        self.resume_delay = 0.0
        self.resume_error: BaseException | None = None
        self.abort_error_after_running: BaseException | None = None
        self.recover_error: BaseException | None = None
        self.recover_calls = 0
        self.prepared = PreparedMaintenance(
            operation_id=str(uuid.uuid4()),
            original_generation=7,
            intent_revision=1,
            initially_open_instance_ids=(),
            registrations=(),
        )

    async def prepare_close(self, operation_id: str, *, drain_deadline: float) -> PreparedMaintenance:
        assert drain_deadline > 0
        self.shared["prepare_calls"] += 1
        self.shared["state"] = "PREPARING"
        self.calls.append(("prepare", operation_id))
        self.prepared = PreparedMaintenance(
            operation_id=operation_id,
            original_generation=7,
            intent_revision=1,
            initially_open_instance_ids=(),
            registrations=(),
        )
        return self.prepared

    async def abort_pre_mutation(self, prepared: PreparedMaintenance) -> None:
        self.shared["state"] = "RUNNING"
        self.calls.append(("abort", prepared.operation_id))
        if self.abort_error_after_running is not None:
            raise self.abort_error_after_running

    async def recover_aborted_pre_mutation(self, prepared: PreparedMaintenance):
        self.recover_calls += 1
        self.calls.append(("recover-aborted", prepared.operation_id))
        if self.recover_error is not None:
            raise self.recover_error
        assert self.shared["state"] == "RUNNING"
        assert self.shared["generation"] == prepared.original_generation
        return SimpleNamespace(
            state="RUNNING",
            operation_id=prepared.operation_id,
            generation=prepared.original_generation,
            original_generation=prepared.original_generation,
            intent_revision=prepared.intent_revision,
        )

    def rearm_recovery(self, prepared: PreparedMaintenance):
        assert self.shared["state"] == "RECOVERY_REQUIRED"
        self.shared["state"] = "PREPARING"
        self.calls.append(("rearm-recovery", prepared.operation_id))
        return SimpleNamespace(state="PREPARING", operation_id=prepared.operation_id)

    def begin_activation(self, prepared: PreparedMaintenance, *, new_generation: int):
        assert self.shared["state"] == "PREPARING"
        self.shared["state"] = "ACTIVATING"
        self.shared["generation"] = new_generation
        self.calls.append(("begin-activation", f"{prepared.operation_id}:{new_generation}"))
        return SimpleNamespace(state="ACTIVATING", generation=new_generation, device=11, inode=22)

    def publish_running(self, prepared: PreparedMaintenance, *, new_generation: int):
        assert self.shared["state"] == "ACTIVATING"
        self.calls.append(("publish-running", f"{prepared.operation_id}:{new_generation}"))
        if self.fail_publish:
            raise RuntimeError("publish failed")
        self.shared["state"] = "RUNNING"
        return SimpleNamespace(state="RUNNING", generation=new_generation, device=11, inode=22)

    def activation_state(self, operation_id: str, *, new_generation: int, device=None, inode=None):
        del device, inode
        if self.shared["generation"] != new_generation:
            raise RuntimeError("generation mismatch")
        return SimpleNamespace(
            state=self.shared["state"],
            operation_id=operation_id,
            generation=new_generation,
            device=11,
            inode=22,
        )

    async def resume_running(self, prepared: PreparedMaintenance, *, new_generation: int) -> ResumeReport:
        assert self.shared["state"] == "RUNNING"
        self.calls.append(("resume-running", f"{prepared.operation_id}:{new_generation}"))
        if self.resume_error is not None:
            raise self.resume_error
        if self.resume_delay:
            await asyncio.sleep(self.resume_delay)
        return ResumeReport(
            ("runtime-a",),
            dict(self.resume_failures),
            participant_failures=dict(self.participant_failures),
        )

    def mark_recovery_required(self, operation_id: str) -> None:
        self.shared["state"] = "RECOVERY_REQUIRED"
        self.calls.append(("coordinator-recovery", operation_id))


class _Operator:
    def __init__(self, calls: list[tuple[str, str | None]], *, error: BaseException | None = None) -> None:
        self.calls = calls
        self.error = error
        self.execute_count = 0
        self.last_plan = None

    async def execute(
        self,
        plan,
        *,
        receipt_path: str,
        state_path: str | None = None,
        activation_hooks: RecoveryActivationHooks | None = None,
    ) -> RecoveryResult:
        self.execute_count += 1
        self.last_plan = plan
        self.calls.append(("operator", plan.operation_id))
        if self.error is not None:
            if isinstance(self.error, RecoveryHold) and self.error.code == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE":
                assert state_path is not None
                Path(state_path).write_text(
                    json.dumps({"phase": "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"}),
                    encoding="utf-8",
                )
            raise self.error
        result = RecoveryResult(
            plan.operation_id,
            "S1_LOCAL_FACTLANE_PURGE_VERIFIED",
            True,
            True,
            receipt_path,
            state_path or "",
            len(plan.targets),
        )
        lease = SimpleNamespace(capability=object())
        if activation_hooks is not None:
            await activation_hooks.activation_ready(result, lease)
        Path(receipt_path).write_text(json.dumps({"operation_id": plan.operation_id}), encoding="utf-8")
        assert state_path is not None
        Path(state_path).write_text(json.dumps({"phase": "S1_LOCAL_FACTLANE_ACTIVATION_READY"}), encoding="utf-8")
        if activation_hooks is not None:
            await activation_hooks.publish_running(result, lease)
        Path(state_path).write_text(json.dumps({"phase": "S1_LOCAL_FACTLANE_PURGE_VERIFIED"}), encoding="utf-8")
        return result


def _orchestrator(
    tmp_path: Path,
    *,
    resolver,
    verifier=None,
    operator: _Operator | SensitiveMemoryRecoveryOperator | None = None,
    calls: list[tuple[str, str | None]] | None = None,
    coordinator: _Coordinator | None = None,
    service: _ServiceState | None = None,
    shared: dict[str, Any] | None = None,
    post_close_resolver=None,
    require_post_close_resolver: bool = False,
) -> tuple[OwnerDestructiveOrchestrator, _Coordinator, _ServiceState, _Operator | SensitiveMemoryRecoveryOperator]:
    call_log = calls if calls is not None else []
    coordinator = coordinator or _Coordinator(call_log, shared=shared)
    service = service or _ServiceState(call_log)
    chosen_operator = operator or _Operator(call_log)
    return (
        OwnerDestructiveOrchestrator(
            db_path=str(tmp_path / "memory.db"),
            profile=_profile(),
            target_resolver=resolver,
            post_close_target_resolver=post_close_resolver,
            require_post_close_target_resolver=require_post_close_resolver,
            verify_fresh_grant=verifier,
            coordinator=coordinator,
            recovery_operator=chosen_operator,
            service_state=service,
            state_root=tmp_path / "purge-state",
            retained_identity_fields_classified_non_sensitive=True,
        ),
        coordinator,
        service,
        chosen_operator,
    )


def test_preview_is_exact_safe_and_confirmation_is_bound_to_digest(tmp_path: Path) -> None:
    target = _target()

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        raise AssertionError("preview must not consume authorization")

    orchestrator, *_ = _orchestrator(tmp_path, resolver=resolver, verifier=verifier)
    operation_id = str(uuid.uuid4())
    preview = asyncio.run(orchestrator.preview(operation_id=operation_id, targets=(_identity(target),)))

    public = preview.to_mapping()
    assert public["operation_id"] == operation_id
    assert public["targets"] == [
        {
            "scope": "PROJECT",
            "project_id": "factlane",
            "worktree_id": None,
            "workflow_id": None,
            "agent_id": None,
            "memory_id": target.memory_id,
            "record_id": target.record_id,
            "revision": 1,
        }
    ]
    assert public["backup_retention_warning"] == BACKUP_RETENTION_WARNING
    assert preview.typed_confirmation == f"PURGE {preview.preview_digest}"
    serialized = json.dumps(public, sort_keys=True)
    assert target.expected_native_hash not in serialized
    assert "physical media" not in serialized.lower()


def test_recovery_rearm_is_exact_operation_and_prepared_identity_bound(tmp_path: Path) -> None:
    db = tmp_path / "rearm.db"
    db.write_bytes(b"factlane-rearm-fixture")
    barrier = MaintenanceBarrierStore(tmp_path / "barrier", str(db))
    barrier.initialize(generation=7)
    operation_id = str(uuid.uuid4())
    prepared = barrier.begin_prepare(operation_id, expected_generation=7)
    barrier.mark_recovery_required(operation_id)

    rearmed = barrier.rearm_recovery(
        operation_id,
        original_generation=prepared.original_generation,
        intent_revision=prepared.intent_revision,
    )
    assert rearmed.state == "PREPARING"
    assert rearmed.operation_id == operation_id
    assert rearmed.original_generation == 7
    assert rearmed.intent_revision == prepared.intent_revision

    barrier.mark_recovery_required(operation_id)
    with pytest.raises(MaintenanceCoordinatorError) as wrong_generation:
        barrier.rearm_recovery(
            operation_id,
            original_generation=8,
            intent_revision=prepared.intent_revision,
        )
    assert wrong_generation.value.code == "ACTIVATION_IDENTITY_MISMATCH"

    with pytest.raises(MaintenanceCoordinatorError) as wrong_operation:
        barrier.rearm_recovery(
            str(uuid.uuid4()),
            original_generation=7,
            intent_revision=prepared.intent_revision,
        )
    assert wrong_operation.value.code == "INVALID_BARRIER_TRANSITION"


def test_typed_confirmation_mismatch_stops_before_maintenance_or_grant(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        calls.append(("grant", None))

    orchestrator, *_ = _orchestrator(tmp_path, resolver=resolver, verifier=verifier, calls=calls)
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(orchestrator.execute(preview, typed_confirmation="PURGE wrong", grant_secret="grant", drain_deadline=1000.0))
    assert caught.value.code == "PURGE_CONFIRMATION_MISMATCH"
    assert calls == []


def test_execute_rejects_forged_preview_before_maintenance(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        calls.append(("grant", None))

    orchestrator, *_ = _orchestrator(tmp_path, resolver=resolver, verifier=verifier, calls=calls)
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    forged = replace(preview, preview_digest="0" * 64, typed_confirmation="PURGE " + "0" * 64)

    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(
            orchestrator.execute(
                forged,
                typed_confirmation=forged.typed_confirmation,
                grant_secret="grant",
                drain_deadline=1000.0,
            )
        )
    assert caught.value.code == "PURGE_PREVIEW_INVALID"
    assert calls == []


def test_success_rechecks_preview_consumes_grant_after_close_and_activates(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []
    resolve_count = 0

    async def resolver(_targets):
        nonlocal resolve_count
        resolve_count += 1
        calls.append(("resolve", str(resolve_count)))
        return (target,)

    async def verifier(*, grant_secret: str, preview: str):
        assert grant_secret == "fresh-grant"
        assert preview
        calls.append(("grant", None))

    orchestrator, coordinator, _service, operator = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        calls=calls,
    )
    operation_id = str(uuid.uuid4())
    preview = asyncio.run(orchestrator.preview(operation_id=operation_id, targets=(_identity(target),)))
    result = asyncio.run(
        orchestrator.execute(
            preview,
            typed_confirmation=preview.typed_confirmation,
            grant_secret="fresh-grant",
            drain_deadline=1000.0,
        )
    )

    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert resolve_count == 2
    assert calls.index(("prepare", operation_id)) < calls.index(("resolve", "2")) < calls.index(("grant", None)) < calls.index(("operator", operation_id))
    assert ("begin-activation", f"{operation_id}:8") in calls
    assert ("publish-running", f"{operation_id}:8") in calls
    assert calls[-2:] == [("resume-running", f"{operation_id}:8"), ("service-finish", operation_id)]
    assert isinstance(operator, _Operator)
    assert operator.last_plan.targets == (target,)
    assert operator.last_plan.operation_id == operation_id
    assert coordinator.prepared.original_generation == 7


def test_configured_post_close_resolver_runs_after_prepare_and_normal_resolver_is_preview_only(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []
    preview_calls = 0
    operation_id = str(uuid.uuid4())

    async def resolver(_targets):
        nonlocal preview_calls
        preview_calls += 1
        if preview_calls > 1:
            raise AssertionError("normal resolver must not be used after coordinator exclusion")
        calls.append(("preview-resolve", None))
        return (target,)

    async def post_close_resolver(resolved_operation_id, identities):
        assert resolved_operation_id == operation_id
        assert identities == (_identity(target),)
        calls.append(("exclusive-resolve", resolved_operation_id))
        return (target,)

    async def verifier(**_kwargs):
        calls.append(("grant", None))

    orchestrator, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        post_close_resolver=post_close_resolver,
        require_post_close_resolver=True,
        verifier=verifier,
        calls=calls,
    )
    preview = asyncio.run(orchestrator.preview(operation_id=operation_id, targets=(_identity(target),)))
    result = asyncio.run(
        orchestrator.execute(
            preview,
            typed_confirmation=preview.typed_confirmation,
            grant_secret="grant",
            drain_deadline=1000.0,
        )
    )

    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert preview_calls == 1
    assert calls.index(("prepare", operation_id)) < calls.index(("exclusive-resolve", operation_id))
    assert calls.index(("exclusive-resolve", operation_id)) < calls.index(("grant", None))
    assert calls.index(("grant", None)) < calls.index(("operator", operation_id))


def test_required_post_close_resolver_missing_fails_closed_at_construction(tmp_path: Path) -> None:
    target = _target()

    async def resolver(_targets):
        return (target,)

    with pytest.raises(TypeError, match="post-close target resolver"):
        _orchestrator(
            tmp_path,
            resolver=resolver,
            post_close_resolver=None,
            require_post_close_resolver=True,
        )


def test_execute_prefers_per_call_verifier_and_passes_exact_authorization_preview(tmp_path: Path) -> None:
    target = _target()
    seen: list[tuple[str, str]] = []

    async def resolver(_targets):
        return (target,)

    async def constructor_verifier(**_kwargs):
        raise AssertionError("constructor verifier must not run when a per-call verifier is supplied")

    async def per_call_verifier(*, grant_secret: str, preview: str):
        seen.append((grant_secret, preview))

    orchestrator, *_ = _orchestrator(tmp_path, resolver=resolver, verifier=constructor_verifier)
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    result = asyncio.run(
        orchestrator.execute(
            preview,
            typed_confirmation=preview.typed_confirmation,
            grant_secret="request-bound-grant",
            drain_deadline=1000.0,
            verify_fresh_grant=per_call_verifier,
        )
    )
    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert seen == [("request-bound-grant", preview.authorization_preview)]


def test_execute_per_call_verifier_rejects_wrong_authorization_preview_before_mutation(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []

    async def resolver(_targets):
        return (target,)

    orchestrator, *_parts, operator = _orchestrator(tmp_path, resolver=resolver, verifier=None, calls=calls)
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    expected = preview.authorization_preview + "-stale"

    async def request_bound_verifier(*, grant_secret: str, preview: str):
        assert grant_secret == "grant"
        if preview != expected:
            raise OwnerDestructiveError("SENSITIVE_PREVIEW_MISMATCH", "request-bound authorization preview mismatch")

    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(
            orchestrator.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="grant",
                drain_deadline=1000.0,
                verify_fresh_grant=request_bound_verifier,
            )
        )
    assert caught.value.code == "SENSITIVE_PREVIEW_MISMATCH"
    assert isinstance(operator, _Operator)
    assert operator.execute_count == 0
    assert ("abort", preview.operation_id) in calls


def test_execute_without_constructor_or_per_call_verifier_fails_closed(tmp_path: Path) -> None:
    target = _target()

    async def resolver(_targets):
        return (target,)

    orchestrator, coordinator, *_ = _orchestrator(tmp_path, resolver=resolver, verifier=None)
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(
            orchestrator.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="grant",
                drain_deadline=1000.0,
            )
        )
    assert caught.value.code == "PURGE_AUTHORIZATION_NOT_CONFIGURED"
    assert coordinator.shared["prepare_calls"] == 0


def test_recheck_drift_aborts_and_reopens_without_consuming_grant(tmp_path: Path) -> None:
    original = _target()
    changed = _target(lifecycle="STALE")
    calls: list[tuple[str, str | None]] = []
    count = 0

    async def resolver(_targets):
        nonlocal count
        count += 1
        return (original if count == 1 else changed,)

    async def verifier(**_kwargs):
        calls.append(("grant", None))

    orchestrator, *_ = _orchestrator(tmp_path, resolver=resolver, verifier=verifier, calls=calls)
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(original),)))
    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(orchestrator.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="grant", drain_deadline=1000.0))

    assert caught.value.code == "PURGE_PREVIEW_STALE"
    assert ("abort", preview.operation_id) in calls
    assert ("service-finish", preview.operation_id) in calls
    assert ("grant", None) not in calls
    assert not any(name == "operator" for name, _ in calls)


def test_fresh_grant_failure_after_close_aborts_before_mutation(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        calls.append(("grant", None))
        raise RuntimeError("expired grant")

    orchestrator, *_ = _orchestrator(tmp_path, resolver=resolver, verifier=verifier, calls=calls)
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(RuntimeError, match="expired grant"):
        asyncio.run(orchestrator.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="grant", drain_deadline=1000.0))
    assert ("abort", preview.operation_id) in calls
    assert ("service-finish", preview.operation_id) in calls
    assert not any(name == "operator" for name, _ in calls)


def test_prepare_recovery_required_marks_service_recovery_without_finish_or_purge(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        raise AssertionError("grant verification must not run after recovery-required prepare")

    coordinator = _Coordinator(calls)

    async def recovery_required_prepare(_operation_id: str, *, drain_deadline: float):
        assert drain_deadline > 0
        raise MaintenanceCoordinatorError(
            "MAINTENANCE_RECOVERY_REQUIRED",
            "runtime close reconciliation requires recovery",
        )

    coordinator.prepare_close = recovery_required_prepare  # type: ignore[method-assign]
    orchestrator, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        calls=calls,
        coordinator=coordinator,
    )
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))

    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(
            orchestrator.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="grant",
                drain_deadline=1000.0,
            )
        )

    assert caught.value.code == "PURGE_RECOVERY_REQUIRED"
    assert caught.value.recovery_required is True
    assert ("service-recovery", None) in calls
    assert ("service-finish", preview.operation_id) not in calls
    assert not any(name == "operator" for name, _ in calls)
    assert orchestrator._read_journal(preview.operation_id) is None


def test_ordinary_clean_prepare_failure_finishes_service_and_preserves_original_error(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []

    async def resolver(_targets):
        return (target,)

    coordinator = _Coordinator(calls)

    async def clean_prepare_failure(_operation_id: str, *, drain_deadline: float):
        assert drain_deadline > 0
        raise RuntimeError("clean prepare failure")

    coordinator.prepare_close = clean_prepare_failure  # type: ignore[method-assign]
    orchestrator, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=lambda **_kwargs: None,
        calls=calls,
        coordinator=coordinator,
    )
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))

    with pytest.raises(RuntimeError, match="clean prepare failure"):
        asyncio.run(
            orchestrator.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="grant",
                drain_deadline=1000.0,
            )
        )

    assert ("service-finish", preview.operation_id) in calls
    assert ("service-recovery", None) not in calls
    assert not any(name == "operator" for name, _ in calls)


def test_precommit_operator_failure_aborts_and_reopens(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        calls.append(("grant", None))

    operator = _Operator(calls, error=RecoveryHold("HOLD_NO_MUTATION", "precommit hold"))
    orchestrator, *_ = _orchestrator(tmp_path, resolver=resolver, verifier=verifier, operator=operator, calls=calls)
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(RecoveryHold) as caught:
        asyncio.run(orchestrator.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="grant", drain_deadline=1000.0))
    assert caught.value.code == "HOLD_NO_MUTATION"
    assert ("abort", preview.operation_id) in calls
    assert ("service-finish", preview.operation_id) in calls
    assert not any(name == "coordinator-recovery" for name, _ in calls)


def test_abort_participant_failure_enters_premutation_recovery_and_retry_requires_fresh_preview(
    tmp_path: Path,
) -> None:
    target = _target()
    shared = {"state": "RUNNING", "generation": 7, "prepare_calls": 0}
    first_calls: list[tuple[str, str | None]] = []
    grant_secrets: list[str] = []

    async def resolver(_targets):
        return (target,)

    async def verifier(*, grant_secret: str, preview: str):
        assert preview
        grant_secrets.append(grant_secret)

    first_coordinator = _Coordinator(first_calls, shared=shared)
    first_coordinator.abort_error_after_running = MaintenanceCoordinatorError(
        "MAINTENANCE_RECOVERY_REQUIRED",
        "Owner participant reopen failed after base abort",
    )
    first_service = _ServiceState(first_calls)
    first_operator = _Operator(first_calls, error=RecoveryHold("HOLD_NO_MUTATION", "precommit hold"))
    first, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=first_operator,
        calls=first_calls,
        coordinator=first_coordinator,
        service=first_service,
    )
    preview = asyncio.run(first.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))

    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(
            first.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="initial-grant",
                drain_deadline=1000.0,
            )
        )

    assert caught.value.code == "PURGE_RECOVERY_REQUIRED"
    assert caught.value.recovery_required is True
    assert shared["state"] == "RUNNING"
    assert first_service.recovery_required is True
    journal = first._read_journal(preview.operation_id)
    assert journal is not None
    assert journal["phase"] == "PREMUTATION_RECOVERY_REQUIRED"
    assert first_operator.execute_count == 1
    assert grant_secrets == ["initial-grant"]

    recoverable = first.recoverable_preview(preview.operation_id)
    assert recoverable is not None
    retry_calls: list[tuple[str, str | None]] = []
    retry_coordinator = _Coordinator(retry_calls, shared=shared)
    retry_operator = _Operator(retry_calls)
    retry, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=retry_operator,
        calls=retry_calls,
        coordinator=retry_coordinator,
        service=first_service,
    )

    with pytest.raises(OwnerDestructiveError) as repreivew:
        asyncio.run(
            retry.execute(
                recoverable,
                typed_confirmation=recoverable.typed_confirmation,
                grant_secret="must-not-be-consumed",
                drain_deadline=1001.0,
            )
        )

    assert repreivew.value.code == "PURGE_REPREVIEW_REQUIRED"
    assert retry_operator.execute_count == 0
    assert retry_coordinator.recover_calls == 1
    assert shared["prepare_calls"] == 1
    assert not any(name == "prepare" for name, _ in retry_calls)
    assert grant_secrets == ["initial-grant"]
    assert first_service.recovery_required is False
    assert retry._read_journal(preview.operation_id) is None

    fresh = asyncio.run(retry.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    result = asyncio.run(
        retry.execute(
            fresh,
            typed_confirmation=fresh.typed_confirmation,
            grant_secret="fresh-grant",
            drain_deadline=1002.0,
        )
    )
    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert retry_operator.execute_count == 1
    assert shared["prepare_calls"] == 2
    assert grant_secrets == ["initial-grant", "fresh-grant"]


def test_premutation_recovery_retry_remains_blocked_while_owner_reopen_still_fails(tmp_path: Path) -> None:
    target = _target()
    shared = {"state": "RUNNING", "generation": 7, "prepare_calls": 0}
    first_calls: list[tuple[str, str | None]] = []
    grant_secrets: list[str] = []

    async def resolver(_targets):
        return (target,)

    async def verifier(*, grant_secret: str, preview: str):
        assert preview
        grant_secrets.append(grant_secret)

    first_coordinator = _Coordinator(first_calls, shared=shared)
    first_coordinator.abort_error_after_running = MaintenanceCoordinatorError(
        "MAINTENANCE_RECOVERY_REQUIRED",
        "Owner participant reopen failed after base abort",
    )
    first_service = _ServiceState(first_calls)
    first, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=_Operator(first_calls, error=RecoveryHold("HOLD_NO_MUTATION", "precommit hold")),
        calls=first_calls,
        coordinator=first_coordinator,
        service=first_service,
    )
    preview = asyncio.run(first.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(OwnerDestructiveError):
        asyncio.run(
            first.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="initial-grant",
                drain_deadline=1000.0,
            )
        )

    retry_calls: list[tuple[str, str | None]] = []
    retry_coordinator = _Coordinator(retry_calls, shared=shared)
    retry_coordinator.recover_error = MaintenanceCoordinatorError(
        "MAINTENANCE_RECOVERY_REQUIRED",
        "Owner participant reopen still failed",
    )
    retry_operator = _Operator(retry_calls)
    retry, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=retry_operator,
        calls=retry_calls,
        coordinator=retry_coordinator,
        service=first_service,
    )
    recoverable = retry.recoverable_preview(preview.operation_id)
    assert recoverable is not None

    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(
            retry.execute(
                recoverable,
                typed_confirmation=recoverable.typed_confirmation,
                grant_secret="must-not-be-consumed",
                drain_deadline=1001.0,
            )
        )

    assert caught.value.code == "PURGE_RECOVERY_REQUIRED"
    assert caught.value.recovery_required is True
    assert retry_operator.execute_count == 0
    assert shared["prepare_calls"] == 1
    assert grant_secrets == ["initial-grant"]
    assert first_service.recovery_required is True
    journal = retry._read_journal(preview.operation_id)
    assert journal is not None
    assert journal["phase"] == "PREMUTATION_RECOVERY_REQUIRED"


@pytest.mark.parametrize("initial_failed", [False, True])
def test_premutation_recovery_restores_original_or_newer_stop_service_truth_before_repreview(
    tmp_path: Path,
    initial_failed: bool,
) -> None:
    target = _target()
    shared = {"state": "RUNNING", "generation": 7, "prepare_calls": 0}
    controller_actions: list[str] = []
    service = WebUIServiceState(
        tmp_path / "premutation-service" / "state.json",
        controller=controller_actions.append,
        clock=lambda: 10.0,
        boot_id=lambda: "boot-a",
    )
    if initial_failed:
        service.mark_failed()
    else:
        service.mark_ready()
    verifier_calls = 0

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        if not initial_failed:
            service.request_stop("stop-during-purge-before-abort")

    first_calls: list[tuple[str, str | None]] = []
    first_coordinator = _Coordinator(first_calls, shared=shared)
    first_coordinator.abort_error_after_running = MaintenanceCoordinatorError(
        "MAINTENANCE_RECOVERY_REQUIRED",
        "Owner participant reopen failed after base abort",
    )
    first, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=_Operator(first_calls, error=RecoveryHold("HOLD_NO_MUTATION", "precommit hold")),
        calls=first_calls,
        coordinator=first_coordinator,
        service=service,  # type: ignore[arg-type]
    )
    preview = asyncio.run(first.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(OwnerDestructiveError) as first_error:
        asyncio.run(
            first.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="grant-1",
                drain_deadline=1000.0,
            )
        )
    assert first_error.value.code == "PURGE_RECOVERY_REQUIRED"
    assert service.snapshot().status is ServiceStatus.RECOVERY_REQUIRED

    retry_calls: list[tuple[str, str | None]] = []
    retry, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=_Operator(retry_calls),
        calls=retry_calls,
        coordinator=_Coordinator(retry_calls, shared=shared),
        service=service,  # type: ignore[arg-type]
    )
    recoverable = retry.recoverable_preview(preview.operation_id)
    assert recoverable is not None
    with pytest.raises(OwnerDestructiveError) as retry_error:
        asyncio.run(
            retry.execute(
                recoverable,
                typed_confirmation=recoverable.typed_confirmation,
                grant_secret="must-not-be-consumed",
                drain_deadline=1001.0,
            )
        )
    assert retry_error.value.code == "PURGE_REPREVIEW_REQUIRED"
    assert verifier_calls == 1
    if initial_failed:
        assert service.snapshot().status is ServiceStatus.FAILED
        assert controller_actions == []
    else:
        assert service.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT
        assert controller_actions == ["stop"]


def test_postcommit_sealing_failure_marks_recovery_required_and_keeps_access_blocked(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        calls.append(("grant", None))

    operator = _Operator(
        calls,
        error=RecoveryHold(
            "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE",
            "logical purge committed but sealing is incomplete",
        ),
    )
    orchestrator, *_ = _orchestrator(tmp_path, resolver=resolver, verifier=verifier, operator=operator, calls=calls)
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(orchestrator.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="grant", drain_deadline=1000.0))

    assert caught.value.code == "PURGE_RECOVERY_REQUIRED"
    assert caught.value.recovery_required is True
    assert ("coordinator-recovery", preview.operation_id) in calls
    assert ("service-recovery", None) in calls
    assert ("abort", preview.operation_id) not in calls
    assert ("service-finish", preview.operation_id) not in calls
    assert not any(name in {"begin-activation", "publish-running", "resume-running"} for name, _ in calls)


def test_recovery_required_retry_uses_persisted_prepared_state_fresh_grant_and_no_prepare_again(tmp_path: Path) -> None:
    target = _target()
    first_calls: list[tuple[str, str | None]] = []
    shared = {"state": "RUNNING", "generation": 7, "prepare_calls": 0}
    grant_secrets: list[str] = []

    async def resolver(_targets):
        return (target,)

    async def verifier(*, grant_secret: str, preview: str):
        assert preview
        grant_secrets.append(grant_secret)

    failing = _Operator(
        first_calls,
        error=RecoveryHold(
            "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE",
            "logical purge committed but sealing is incomplete",
        ),
    )
    first, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=failing,
        calls=first_calls,
        shared=shared,
    )
    preview = asyncio.run(first.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(first.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="initial-grant", drain_deadline=1000.0))
    assert caught.value.code == "PURGE_RECOVERY_REQUIRED"
    assert shared["state"] == "RECOVERY_REQUIRED"
    assert shared["prepare_calls"] == 1

    retry_calls: list[tuple[str, str | None]] = []
    retry_coordinator = _Coordinator(retry_calls, shared=shared)
    retry_operator = _Operator(retry_calls)
    retry_service = _ServiceState(retry_calls, recovery_required=True)
    retry, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=retry_operator,
        calls=retry_calls,
        coordinator=retry_coordinator,
        service=retry_service,
    )
    result = asyncio.run(
        retry.execute(
            preview,
            typed_confirmation=preview.typed_confirmation,
            grant_secret="recovery-grant",
            drain_deadline=1001.0,
        )
    )

    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert shared["prepare_calls"] == 1
    assert grant_secrets == ["initial-grant", "recovery-grant"]
    assert retry_operator.execute_count == 1
    assert ("prepare", preview.operation_id) not in retry_calls
    assert ("rearm-recovery", preview.operation_id) in retry_calls
    assert ("begin-activation", f"{preview.operation_id}:8") in retry_calls
    assert ("publish-running", f"{preview.operation_id}:8") in retry_calls
    assert ("resume-running", f"{preview.operation_id}:8") in retry_calls
    assert ("service-clear-recovery", None) in retry_calls


def test_recoverable_preview_reconstructs_exact_durable_binding_without_resolving_targets(tmp_path: Path) -> None:
    target = _target()
    first_calls: list[tuple[str, str | None]] = []
    shared = {"state": "RUNNING", "generation": 7, "prepare_calls": 0}
    grants: list[str] = []

    async def resolver(_targets):
        return (target,)

    async def verifier(*, grant_secret: str, preview: str):
        assert preview
        grants.append(grant_secret)

    failing = _Operator(
        first_calls,
        error=RecoveryHold(
            "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE",
            "logical purge committed but sealing is incomplete",
        ),
    )
    first, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=failing,
        calls=first_calls,
        shared=shared,
    )
    preview = asyncio.run(first.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(
            first.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="initial-grant",
                drain_deadline=1000.0,
            )
        )
    assert caught.value.code == "PURGE_RECOVERY_REQUIRED"
    assert shared["prepare_calls"] == 1

    async def resolver_after_restart(_targets):
        raise AssertionError("durable preview recovery must not resolve current target state")

    retry_calls: list[tuple[str, str | None]] = []
    retry_operator = _Operator(retry_calls)
    retry, *_ = _orchestrator(
        tmp_path,
        resolver=resolver_after_restart,
        verifier=verifier,
        operator=retry_operator,
        calls=retry_calls,
        coordinator=_Coordinator(retry_calls, shared=shared),
        service=_ServiceState(retry_calls, recovery_required=True),
    )

    recovered = retry.recoverable_preview(preview.operation_id)
    assert recovered == preview
    result = asyncio.run(
        retry.execute(
            recovered,
            typed_confirmation=recovered.typed_confirmation,
            grant_secret="recovery-grant",
            drain_deadline=1001.0,
        )
    )

    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert shared["prepare_calls"] == 1
    assert retry_operator.execute_count == 1
    assert grants == ["initial-grant", "recovery-grant"]
    assert ("prepare", preview.operation_id) not in retry_calls


def test_recoverable_preview_rejects_tampered_persisted_target_state_without_resolving(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []
    shared = {"state": "RUNNING", "generation": 7, "prepare_calls": 0}

    async def resolver(_targets):
        return (target,)

    failing = _Operator(
        calls,
        error=RecoveryHold(
            "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE",
            "logical purge committed but sealing is incomplete",
        ),
    )
    first, *_ = _orchestrator(tmp_path, resolver=resolver, operator=failing, calls=calls, shared=shared)
    preview = asyncio.run(first.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(OwnerDestructiveError):
        asyncio.run(
            first.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="grant",
                drain_deadline=1000.0,
                verify_fresh_grant=lambda **_kwargs: None,
            )
        )

    journal_path = tmp_path / "purge-state" / f"{preview.operation_id}.purge-operation.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["targets"][0]["memory_id"] = "tampered-memory-id"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    async def resolver_after_restart(_targets):
        raise AssertionError("tamper detection must not resolve current target state")

    retry, *_ = _orchestrator(
        tmp_path,
        resolver=resolver_after_restart,
        calls=[],
        coordinator=_Coordinator([], shared=shared),
        service=_ServiceState([], recovery_required=True),
    )
    with pytest.raises(OwnerDestructiveError) as caught:
        retry.recoverable_preview(preview.operation_id)
    assert caught.value.code == "PURGE_RECOVERY_STATE_MISMATCH"


def test_recovery_retry_wrong_preview_or_grant_stays_blocked_without_operator(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []
    shared = {"state": "RUNNING", "generation": 7, "prepare_calls": 0}

    async def resolver(_targets):
        return (target,)

    async def verifier(*, grant_secret: str, preview: str):
        del preview
        if grant_secret != "good-recovery-grant":
            raise RuntimeError("grant rejected")

    failing = _Operator(
        calls,
        error=RecoveryHold("S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE", "sealing incomplete"),
    )
    first, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=failing,
        calls=calls,
        shared=shared,
    )
    preview = asyncio.run(first.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(OwnerDestructiveError):
        asyncio.run(first.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="good-recovery-grant", drain_deadline=1000.0))

    retry_calls: list[tuple[str, str | None]] = []
    retry_operator = _Operator(retry_calls)
    retry, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=retry_operator,
        calls=retry_calls,
        coordinator=_Coordinator(retry_calls, shared=shared),
        service=_ServiceState(retry_calls, recovery_required=True),
    )
    forged = replace(preview, _target_state_digest="0" * 64)
    with pytest.raises(OwnerDestructiveError) as wrong_preview:
        asyncio.run(retry.execute(forged, typed_confirmation=forged.typed_confirmation, grant_secret="good-recovery-grant", drain_deadline=1001.0))
    assert wrong_preview.value.code in {"PURGE_OPERATION_BINDING_MISMATCH", "PURGE_PREVIEW_INVALID", "PURGE_RECOVERY_STATE_MISMATCH"}
    assert retry_operator.execute_count == 0
    assert shared["state"] == "RECOVERY_REQUIRED"

    with pytest.raises(RuntimeError, match="grant rejected"):
        asyncio.run(retry.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="wrong-grant", drain_deadline=1001.0))
    assert retry_operator.execute_count == 0
    assert shared["state"] == "RECOVERY_REQUIRED"
    assert not any(name in {"rearm-recovery", "begin-activation", "publish-running"} for name, _ in retry_calls)

    wrong_operation = replace(preview, operation_id=str(uuid.uuid4()))
    with pytest.raises(OwnerDestructiveError) as wrong_op:
        asyncio.run(
            retry.execute(
                wrong_operation,
                typed_confirmation=wrong_operation.typed_confirmation,
                grant_secret="good-recovery-grant",
                drain_deadline=1001.0,
            )
        )
    assert wrong_op.value.code == "PURGE_PREVIEW_INVALID"
    assert shared["state"] == "RECOVERY_REQUIRED"


def test_running_before_outer_result_persistence_reconciles_without_redelete(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []
    shared = {"state": "RUNNING", "generation": 7, "prepare_calls": 0}

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        return None

    first_operator = _Operator(calls)
    first, *_ = _orchestrator(tmp_path, resolver=resolver, verifier=verifier, operator=first_operator, calls=calls, shared=shared)
    preview = asyncio.run(first.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    original_persist = first._persist_completed

    def crash_before_outer_result(result):
        del result
        raise SystemExit("synthetic lost response after RUNNING")

    first._persist_completed = crash_before_outer_result  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        asyncio.run(first.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="grant", drain_deadline=1000.0))
    first._persist_completed = original_persist  # type: ignore[method-assign]
    assert shared["state"] == "RUNNING"
    assert first_operator.execute_count == 1

    retry_calls: list[tuple[str, str | None]] = []
    retry_operator = _Operator(retry_calls)
    retry, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=retry_operator,
        calls=retry_calls,
        coordinator=_Coordinator(retry_calls, shared=shared),
        service=_ServiceState(retry_calls),
    )
    result = asyncio.run(retry.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="unused", drain_deadline=1001.0))
    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert retry_operator.execute_count == 0
    assert shared["prepare_calls"] == 1


def test_resume_failures_after_running_are_reconnect_failures_not_recovery(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        return None

    orchestrator, coordinator, *_ = _orchestrator(tmp_path, resolver=resolver, verifier=verifier, calls=calls)
    coordinator.resume_failures = {"runtime-a": "UNREACHABLE"}
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    result = asyncio.run(orchestrator.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="grant", drain_deadline=1000.0))
    assert result.resume_failures == {"runtime-a": "UNREACHABLE"}
    assert coordinator.shared["state"] == "RUNNING"
    assert not any(name == "coordinator-recovery" for name, _ in calls)


def test_lost_success_retry_returns_persisted_result_without_duplicate_deletion_or_grant(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []

    async def resolver(_targets):
        calls.append(("resolve", None))
        return (target,)

    grant_count = 0

    async def verifier(**_kwargs):
        nonlocal grant_count
        grant_count += 1
        calls.append(("grant", None))

    orchestrator, *_parts, operator = _orchestrator(tmp_path, resolver=resolver, verifier=verifier, calls=calls)
    assert isinstance(operator, _Operator)
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    first = asyncio.run(orchestrator.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="grant", drain_deadline=1000.0))
    retry_calls: list[tuple[str, str | None]] = []
    retry_operator = _Operator(retry_calls)
    retry_orchestrator, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=retry_operator,
        calls=retry_calls,
    )
    second = asyncio.run(
        retry_orchestrator.execute(
            preview,
            typed_confirmation=preview.typed_confirmation,
            grant_secret="same-consumed-grant",
            drain_deadline=1000.0,
        )
    )

    assert second == first
    assert operator.execute_count == 1
    assert retry_operator.execute_count == 0
    assert grant_count == 1
    assert sum(name == "prepare" for name, _ in calls) == 1
    assert retry_calls == []


class _FixedProvider:
    def __init__(self, profile: EmbeddingProfile) -> None:
        self.profile = profile
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        vector = [0.0] * (self.profile.output_dimension - 1) + [1.0]
        return [list(vector) for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        del text
        return [0.0] * (self.profile.output_dimension - 1) + [1.0]


def _provider_status(profile: EmbeddingProfile) -> dict[str, object]:
    return {
        "provider_kind": profile.provider_kind,
        "model": profile.base_model_identity,
        "digest": profile.model_digest,
        "ollama_version": "owner-destructive-test",
        "semantic_family": profile.semantic_family,
        "native_dimension": profile.source_dimension,
        "output_dimension": profile.output_dimension,
        "effective_context_window": profile.minimum_context_window,
    }


@pytest.mark.skipif(os.name == "nt", reason="SensitiveMemoryRecoveryOperator requires POSIX recovery locking")
def test_disposable_real_recovery_operator_runs_through_owner_orchestration(tmp_path: Path) -> None:
    marker = "OWNER_DESTRUCTIVE_DISPOSABLE_MARKER"

    async def prepare() -> RecoveryTarget:
        profile = _profile()
        engine = SQLiteVecEngine(str(tmp_path / "memory.db"), profile)
        await engine.open()
        provider = _FixedProvider(profile)
        await _establish_embedding_compatibility(engine, provider, _provider_status(profile))
        adapter = MemoryAdapter(
            engine,
            provider,  # type: ignore[arg-type]
            trusted_write_context=trusted_write_context_for_profile(
                "owner-current",
                contributor_ref="owner-destructive-tests",
            ),
        )
        stamp = "2026-11-05T20:00:00Z"
        stored = await adapter.store(
            fact=f"Disposable target {marker}.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            source_provenance={
                "source_class": "CURRENT_REPO",
                "source_ref": "owner-destructive-test",
                "source_hash": "c" * 64,
                "review_ref": "owner-destructive-test",
                "extraction_method": "AUTOMATED_CHECK",
            },
            freshness_policy={"kind": "manual"},
            idempotency_key="owner-destructive-real",
            project_id="factlane",
            source_timestamp=stamp,
            last_verified_at=stamp,
            verified_by="OWNER",
            requested_lifecycle_state="VALIDATED_CURRENT",
            confidence=0.95,
            tags=["subject:owner-destructive-test"],
        )
        memory_id = stored["results"][0]["memory_id"]
        rows = await engine.get_record(memory_id, ScopeContext("PROJECT", project_id="factlane"), history=True)
        row = rows[0]
        await adapter.close()
        return RecoveryTarget(
            record_id=row["record_id"],
            memory_id=row["memory_id"],
            revision=int(row["revision"]),
            scope=RecoveryScope("PROJECT", project_id="factlane"),
            expected_lifecycle=row["lifecycle_state"],
            expected_materialization=PM1,
            expected_native_hash=row["native_content_hash"],
        )

    target = asyncio.run(prepare())

    async def resolver(targets):
        assert targets == (_identity(target),)
        return (target,)

    async def post_close_resolver(operation_id, targets):
        assert targets == (_identity(target),)
        return await resolve_permanent_purge_targets_exclusive(
            operation_id=operation_id,
            db_path=str(tmp_path / "memory.db"),
            profile=_profile(),
            identities=tuple(identity.to_mapping() for identity in targets),
        )

    async def verifier(**_kwargs):
        return None

    real_operator = SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _path: [])
    orchestrator, coordinator, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        post_close_resolver=post_close_resolver,
        require_post_close_resolver=True,
        verifier=verifier,
        operator=real_operator,
    )
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    result = asyncio.run(orchestrator.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="grant", drain_deadline=1000.0))

    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert coordinator.shared["state"] == "RUNNING"
    conn = sqlite3.connect(tmp_path / "memory.db")
    try:
        fact = conn.execute("SELECT fact FROM adapter_records WHERE record_id=?", (target.record_id,)).fetchone()[0]
        interlock = conn.execute(
            "SELECT value FROM adapter_meta WHERE key='sensitive_recovery_interlock'"
        ).fetchone()
    finally:
        conn.close()
    assert fact == "[purged-sensitive-memory]"
    assert interlock is None
    assert marker.encode() not in (tmp_path / "memory.db").read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="SensitiveMemoryRecoveryOperator requires POSIX recovery locking")
def test_real_postcommit_failure_new_orchestrator_authenticated_resume_has_one_logical_purge(tmp_path: Path) -> None:
    class FailSealOnce(SensitiveMemoryRecoveryOperator):
        def _seal_and_promote(self, conn, plan, survivor_snapshot, maintenance_lease):
            raise RecoveryHold("S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE", "synthetic sealing failure")

    marker = "OWNER_DESTRUCTIVE_RESUME_MARKER"

    async def prepare() -> RecoveryTarget:
        profile = _profile()
        engine = SQLiteVecEngine(str(tmp_path / "memory.db"), profile)
        await engine.open()
        provider = _FixedProvider(profile)
        await _establish_embedding_compatibility(engine, provider, _provider_status(profile))
        adapter = MemoryAdapter(
            engine,
            provider,  # type: ignore[arg-type]
            trusted_write_context=trusted_write_context_for_profile(
                "owner-current",
                contributor_ref="owner-destructive-resume-tests",
            ),
        )
        stamp = "2026-11-05T20:00:00Z"
        stored = await adapter.store(
            fact=f"Disposable resume target {marker}.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            source_provenance={
                "source_class": "CURRENT_REPO",
                "source_ref": "owner-destructive-resume",
                "source_hash": "d" * 64,
                "review_ref": "owner-destructive-resume",
                "extraction_method": "AUTOMATED_CHECK",
            },
            freshness_policy={"kind": "manual"},
            idempotency_key="owner-destructive-resume-real",
            project_id="factlane",
            source_timestamp=stamp,
            last_verified_at=stamp,
            verified_by="OWNER",
            requested_lifecycle_state="VALIDATED_CURRENT",
            confidence=0.95,
            tags=["subject:owner-destructive-resume-test"],
        )
        memory_id = stored["results"][0]["memory_id"]
        row = (await engine.get_record(memory_id, ScopeContext("PROJECT", project_id="factlane"), history=True))[0]
        await adapter.close()
        return RecoveryTarget(
            record_id=row["record_id"],
            memory_id=row["memory_id"],
            revision=int(row["revision"]),
            scope=RecoveryScope("PROJECT", project_id="factlane"),
            expected_lifecycle=row["lifecycle_state"],
            expected_materialization=PM1,
            expected_native_hash=row["native_content_hash"],
        )

    target = asyncio.run(prepare())
    resolver_calls = 0

    async def resolver(_targets):
        nonlocal resolver_calls
        resolver_calls += 1
        return (target,)

    grants: list[str] = []

    async def verifier(*, grant_secret: str, preview: str):
        assert preview
        grants.append(grant_secret)

    shared = {"state": "RUNNING", "generation": 7, "prepare_calls": 0}
    first, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=FailSealOnce(foreign_handle_provider=lambda _path: []),
        shared=shared,
    )
    preview = asyncio.run(first.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(OwnerDestructiveError) as first_error:
        asyncio.run(
            first.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="initial-grant",
                drain_deadline=1000.0,
            )
        )
    assert first_error.value.code == "PURGE_RECOVERY_REQUIRED"
    assert shared["state"] == "RECOVERY_REQUIRED"

    retry_calls: list[tuple[str, str | None]] = []
    retry = OwnerDestructiveOrchestrator(
        db_path=str(tmp_path / "memory.db"),
        profile=_profile(),
        target_resolver=resolver,
        require_post_close_target_resolver=False,
        verify_fresh_grant=verifier,
        coordinator=_Coordinator(retry_calls, shared=shared),
        recovery_operator=SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _path: []),
        service_state=_ServiceState(retry_calls, recovery_required=True),
        state_root=tmp_path / "purge-state",
        retained_identity_fields_classified_non_sensitive=True,
    )
    recovered = retry.recoverable_preview(preview.operation_id)
    assert recovered == preview
    result = asyncio.run(
        retry.execute(
            recovered,
            typed_confirmation=recovered.typed_confirmation,
            grant_secret="recovery-grant",
            drain_deadline=1001.0,
        )
    )
    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert shared["prepare_calls"] == 1
    assert resolver_calls == 2  # preview + initial post-close recheck; recovery uses persisted exact targets.
    assert grants == ["initial-grant", "recovery-grant"]
    conn = sqlite3.connect(tmp_path / "memory.db")
    try:
        purge_events = conn.execute(
            "SELECT COUNT(*) FROM lifecycle_events WHERE operation_id=? AND action='SENSITIVE_PURGE'",
            (preview.operation_id,),
        ).fetchone()[0]
        interlock = conn.execute(
            "SELECT value FROM adapter_meta WHERE key='sensitive_recovery_interlock'"
        ).fetchone()
    finally:
        conn.close()
    assert purge_events == 1
    assert interlock is None
    assert shared["state"] == "RUNNING"
    assert marker.encode() not in (tmp_path / "memory.db").read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="SensitiveMemoryRecoveryOperator requires POSIX recovery locking")
def test_publish_failure_reestablishes_interlock_and_ordinary_storage_remains_blocked(tmp_path: Path) -> None:
    target_holder: dict[str, RecoveryTarget] = {}

    async def prepare() -> None:
        profile = _profile()
        engine = SQLiteVecEngine(str(tmp_path / "memory.db"), profile)
        await engine.open()
        provider = _FixedProvider(profile)
        await _establish_embedding_compatibility(engine, provider, _provider_status(profile))
        adapter = MemoryAdapter(
            engine,
            provider,  # type: ignore[arg-type]
            trusted_write_context=trusted_write_context_for_profile(
                "owner-current",
                contributor_ref="owner-destructive-publish-fail",
            ),
        )
        stamp = "2026-11-05T20:00:00Z"
        stored = await adapter.store(
            fact="Disposable publish-failure target.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            source_provenance={
                "source_class": "CURRENT_REPO",
                "source_ref": "owner-destructive-publish-fail",
                "source_hash": "e" * 64,
                "review_ref": "owner-destructive-publish-fail",
                "extraction_method": "AUTOMATED_CHECK",
            },
            freshness_policy={"kind": "manual"},
            idempotency_key="owner-destructive-publish-fail",
            project_id="factlane",
            source_timestamp=stamp,
            last_verified_at=stamp,
            verified_by="OWNER",
            requested_lifecycle_state="VALIDATED_CURRENT",
            confidence=0.95,
            tags=["subject:owner-destructive-publish-fail"],
        )
        row = (await engine.get_record(stored["results"][0]["memory_id"], ScopeContext("PROJECT", project_id="factlane"), history=True))[0]
        await adapter.close()
        target_holder["target"] = RecoveryTarget(
            record_id=row["record_id"], memory_id=row["memory_id"], revision=int(row["revision"]),
            scope=RecoveryScope("PROJECT", project_id="factlane"), expected_lifecycle=row["lifecycle_state"],
            expected_materialization=PM1, expected_native_hash=row["native_content_hash"],
        )

    asyncio.run(prepare())
    target = target_holder["target"]

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        return None

    calls: list[tuple[str, str | None]] = []
    coordinator = _Coordinator(calls)
    coordinator.fail_publish = True
    orchestrator, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _path: []),
        calls=calls,
        coordinator=coordinator,
    )
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(orchestrator.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="grant", drain_deadline=1000.0))
    assert caught.value.code == "PURGE_RECOVERY_REQUIRED"
    assert coordinator.shared["state"] == "RECOVERY_REQUIRED"

    async def ordinary_open() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "memory.db"), _profile())
        with pytest.raises(Exception) as blocked:
            await engine.open()
        assert getattr(blocked.value, "code", None) == "MAINTENANCE_IN_PROGRESS"

    asyncio.run(ordinary_open())

    coordinator.fail_publish = False
    retry_calls: list[tuple[str, str | None]] = []
    retry = OwnerDestructiveOrchestrator(
        db_path=str(tmp_path / "memory.db"),
        profile=_profile(),
        target_resolver=resolver,
        require_post_close_target_resolver=False,
        verify_fresh_grant=verifier,
        coordinator=_Coordinator(retry_calls, shared=coordinator.shared),
        recovery_operator=SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _path: []),
        service_state=_ServiceState(retry_calls, recovery_required=True),
        state_root=tmp_path / "purge-state",
        retained_identity_fields_classified_non_sensitive=True,
    )
    resumed = asyncio.run(
        retry.execute(
            preview,
            typed_confirmation=preview.typed_confirmation,
            grant_secret="fresh-recovery-grant",
            drain_deadline=1001.0,
        )
    )
    assert resumed.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert coordinator.shared["prepare_calls"] == 1
    conn = sqlite3.connect(tmp_path / "memory.db")
    try:
        purge_events = conn.execute(
            "SELECT COUNT(*) FROM lifecycle_events WHERE operation_id=? AND action='SENSITIVE_PURGE'",
            (preview.operation_id,),
        ).fetchone()[0]
        interlock = conn.execute(
            "SELECT value FROM adapter_meta WHERE key='sensitive_recovery_interlock'"
        ).fetchone()
    finally:
        conn.close()
    assert purge_events == 1
    assert interlock is None
    assert coordinator.shared["state"] == "RUNNING"


@pytest.mark.parametrize("initial_failed", [False, True])
def test_service_initial_state_and_newer_owner_stop_remain_authoritative(tmp_path: Path, initial_failed: bool) -> None:
    target = _target()
    controller_actions: list[str] = []
    service = WebUIServiceState(
        tmp_path / "service" / "state.json",
        controller=controller_actions.append,
        clock=lambda: 10.0,
        boot_id=lambda: "boot-a",
    )
    if initial_failed:
        service.mark_failed()
    else:
        service.mark_ready()

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        if not initial_failed:
            service.request_stop("stop-during-purge")

    orchestrator, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=_Operator([]),
        service=service,  # type: ignore[arg-type]
    )
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    result = asyncio.run(orchestrator.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="grant", drain_deadline=1000.0))
    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    if initial_failed:
        assert service.snapshot().status is ServiceStatus.FAILED
        assert controller_actions == []
    else:
        assert service.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT
        assert controller_actions == ["stop"]


@pytest.mark.parametrize("initial_failed", [False, True])
def test_recovery_resume_restores_initial_failed_or_newer_owner_stop(tmp_path: Path, initial_failed: bool) -> None:
    target = _target()
    controller_actions: list[str] = []
    service = WebUIServiceState(
        tmp_path / "service-recovery" / "state.json",
        controller=controller_actions.append,
        clock=lambda: 10.0,
        boot_id=lambda: "boot-a",
    )
    if initial_failed:
        service.mark_failed()
    else:
        service.mark_ready()

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        return None

    shared = {"state": "RUNNING", "generation": 7, "prepare_calls": 0}
    first_calls: list[tuple[str, str | None]] = []
    first, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=_Operator(
            first_calls,
            error=RecoveryHold("S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE", "synthetic recovery hold"),
        ),
        calls=first_calls,
        service=service,  # type: ignore[arg-type]
        shared=shared,
    )
    preview = asyncio.run(first.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))
    with pytest.raises(OwnerDestructiveError):
        asyncio.run(first.execute(preview, typed_confirmation=preview.typed_confirmation, grant_secret="grant-1", drain_deadline=1000.0))
    assert service.snapshot().status is ServiceStatus.RECOVERY_REQUIRED

    if not initial_failed:
        service.request_stop("stop-during-recovery")
        assert service.snapshot().status is ServiceStatus.RECOVERY_REQUIRED

    retry_calls: list[tuple[str, str | None]] = []
    retry, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=_Operator(retry_calls),
        calls=retry_calls,
        coordinator=_Coordinator(retry_calls, shared=shared),
        service=service,  # type: ignore[arg-type]
    )
    result = asyncio.run(
        retry.execute(
            preview,
            typed_confirmation=preview.typed_confirmation,
            grant_secret="grant-2",
            drain_deadline=1001.0,
        )
    )
    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert shared["prepare_calls"] == 1
    if initial_failed:
        assert service.snapshot().status is ServiceStatus.FAILED
        assert controller_actions == []
    else:
        assert service.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT
        assert controller_actions == ["stop"]


def test_participant_resume_failure_stays_activation_ready_and_retry_resumes_before_verifying(tmp_path: Path) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []
    shared = {"state": "RUNNING", "generation": 7, "prepare_calls": 0}

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        return None

    coordinator = _Coordinator(calls, shared=shared)
    coordinator.resume_failures = {"runtime-b": "REOPEN_FAILED"}
    coordinator.participant_failures = {"owner_adapter": "OWNER_ADAPTER_REOPEN_FAILED"}
    service_state = _ServiceState(calls)
    orchestrator, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        calls=calls,
        coordinator=coordinator,
        service=service_state,
    )
    preview = asyncio.run(orchestrator.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))

    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(
            orchestrator.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="grant",
                drain_deadline=1000.0,
            )
        )

    assert caught.value.code == "PURGE_RECOVERY_REQUIRED"
    assert caught.value.recovery_required is True
    assert shared["state"] == "RUNNING"
    assert ("service-finish", preview.operation_id) not in calls
    assert ("service-recovery", None) in calls
    journal = orchestrator._read_journal(preview.operation_id)
    assert journal is not None
    assert journal["phase"] == "ACTIVATION_READY"
    assert journal["ready_instance_ids"] == ["runtime-a"]
    assert journal["resume_failures"] == {"runtime-b": "REOPEN_FAILED"}
    assert journal["maintenance_participant_failures"] == {"owner_adapter": "OWNER_ADAPTER_REOPEN_FAILED"}
    assert not (tmp_path / "purge-state" / f"{preview.operation_id}.purge-result.json").exists()

    retry_calls: list[tuple[str, str | None]] = []
    retry_coordinator = _Coordinator(retry_calls, shared=shared)
    retry_coordinator.resume_failures = {"runtime-b": "REOPEN_FAILED"}
    retry_service = _ServiceState(retry_calls, recovery_required=True)
    retry, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        calls=retry_calls,
        coordinator=retry_coordinator,
        service=retry_service,
    )
    result = asyncio.run(
        retry.execute(
            preview,
            typed_confirmation=preview.typed_confirmation,
            grant_secret="fresh-grant",
            drain_deadline=1001.0,
        )
    )

    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert result.ready_instance_ids == ("runtime-a",)
    assert result.resume_failures == {"runtime-b": "REOPEN_FAILED"}
    assert ("resume-running", f"{preview.operation_id}:8") in retry_calls
    assert ("service-clear-recovery", None) in retry_calls
    final_journal = retry._read_journal(preview.operation_id)
    assert final_journal is not None
    assert final_journal["phase"] == "VERIFIED"
    assert final_journal["maintenance_participant_failures"] == {}


def test_resume_running_observation_timeout_returns_exact_stdio_failure_without_participant_failure(
    tmp_path: Path,
) -> None:
    target = _target()
    calls: list[tuple[str, str | None]] = []

    async def resolver(_targets):
        return (target,)

    coordinator = _Coordinator(calls)
    coordinator.resume_error = MaintenanceCoordinatorError(
        "RUNTIME_RESUME_TIMEOUT",
        "runtime resume exceeded its absolute deadline",
    )
    orchestrator, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        coordinator=coordinator,
        calls=calls,
    )
    coordinator.shared["state"] = "RUNNING"

    ready, failures, participant_failures = asyncio.run(
        orchestrator._resume_running_observation(
            coordinator.prepared,
            new_generation=8,
        )
    )

    assert ready == ()
    assert failures == {"coordinator": "RUNTIME_RESUME_TIMEOUT"}
    assert participant_failures == {}
    assert calls == [("resume-running", f"{coordinator.prepared.operation_id}:8")]


def test_resume_timeout_stays_durable_activation_ready_and_retry_does_not_redelete(
    tmp_path: Path,
) -> None:
    target = _target()
    first_calls: list[tuple[str, str | None]] = []
    shared = {"state": "RUNNING", "generation": 7, "prepare_calls": 0}

    async def resolver(_targets):
        return (target,)

    async def verifier(**_kwargs):
        return None

    first_coordinator = _Coordinator(first_calls, shared=shared)
    first_coordinator.resume_error = MaintenanceCoordinatorError(
        "RUNTIME_RESUME_TIMEOUT",
        "runtime resume exceeded its absolute deadline",
    )
    first_service = _ServiceState(first_calls)
    first_operator = _Operator(first_calls)
    first, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=first_operator,
        calls=first_calls,
        coordinator=first_coordinator,
        service=first_service,
    )
    preview = asyncio.run(first.preview(operation_id=str(uuid.uuid4()), targets=(_identity(target),)))

    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(
            first.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="grant",
                drain_deadline=1000.0,
            )
        )

    assert caught.value.code == "PURGE_RECOVERY_REQUIRED"
    assert caught.value.recovery_required is True
    assert first_operator.execute_count == 1
    assert shared["state"] == "RUNNING"
    assert first_service.recovery_required is True
    assert ("service-recovery", None) in first_calls
    assert ("service-finish", preview.operation_id) not in first_calls
    journal = first._read_journal(preview.operation_id)
    assert journal is not None
    assert journal["phase"] == "ACTIVATION_READY"
    assert journal["ready_instance_ids"] == []
    assert journal["resume_failures"] == {"coordinator": "RUNTIME_RESUME_TIMEOUT"}
    assert journal["maintenance_participant_failures"] == {}
    assert not (tmp_path / "purge-state" / f"{preview.operation_id}.purge-result.json").exists()

    retry_calls: list[tuple[str, str | None]] = []
    retry_coordinator = _Coordinator(retry_calls, shared=shared)
    retry_service = _ServiceState(retry_calls, recovery_required=True)
    retry_operator = _Operator(retry_calls)
    retry, *_ = _orchestrator(
        tmp_path,
        resolver=resolver,
        verifier=verifier,
        operator=retry_operator,
        calls=retry_calls,
        coordinator=retry_coordinator,
        service=retry_service,
    )
    result = asyncio.run(
        retry.execute(
            preview,
            typed_confirmation=preview.typed_confirmation,
            grant_secret="fresh-grant",
            drain_deadline=1001.0,
        )
    )

    assert result.status == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert result.ready_instance_ids == ("runtime-a",)
    assert result.resume_failures == {}
    assert retry_operator.execute_count == 0
    assert shared["prepare_calls"] == 1
    assert ("prepare", preview.operation_id) not in retry_calls
    assert ("resume-running", f"{preview.operation_id}:8") in retry_calls
    assert ("service-clear-recovery", None) in retry_calls
    final_journal = retry._read_journal(preview.operation_id)
    assert final_journal is not None
    assert final_journal["phase"] == "VERIFIED"
    assert final_journal["maintenance_participant_failures"] == {}
