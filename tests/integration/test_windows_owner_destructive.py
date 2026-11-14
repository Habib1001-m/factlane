from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

if sys.platform != "win32":
    pytest.skip("native Windows Owner destructive custody qualification only", allow_module_level=True)

from factlane.embeddings import EmbeddingProfile
from factlane.maintenance_coordinator import PreparedMaintenance
import factlane.owner_destructive as owner_destructive_module
from factlane.owner_destructive import (
    OwnerDestructiveError,
    OwnerDestructiveOrchestrator,
    PurgeSuccess,
    PurgeTargetIdentity,
)
from factlane.recovery import PM1, RecoveryScope, RecoveryTarget
from factlane.windows_security import (
    current_user_sid,
    protect_owner_private_path,
    validate_owner_private_path,
)


class _ForbiddenMutationSurface:
    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected mutation/control access: {name}")


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="windows-purge-256",
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


def _target() -> RecoveryTarget:
    return RecoveryTarget(
        record_id="22222222-2222-4222-8222-222222222222",
        memory_id="11111111-1111-4111-8111-111111111111",
        revision=1,
        scope=RecoveryScope("PROJECT", project_id="factlane"),
        expected_lifecycle="VALIDATED_CURRENT",
        expected_materialization=PM1,
        expected_native_hash="a" * 64,
    )


def _identity(target: RecoveryTarget) -> PurgeTargetIdentity:
    return PurgeTargetIdentity(
        scope=target.scope,
        memory_id=target.memory_id,
        record_id=target.record_id,
        revision=target.revision,
    )


def _orchestrator(state_root: Path, target: RecoveryTarget) -> OwnerDestructiveOrchestrator:
    async def resolver(_targets):
        return (target,)

    forbidden = _ForbiddenMutationSurface()
    return OwnerDestructiveOrchestrator(
        db_path=str(state_root.parent / "memory.db"),
        profile=_profile(),
        target_resolver=resolver,
        require_post_close_target_resolver=False,
        verify_fresh_grant=lambda **_kwargs: None,
        coordinator=forbidden,
        recovery_operator=forbidden,
        service_state=forbidden,
        state_root=state_root,
        retained_identity_fields_classified_non_sensitive=True,
    )


def _set_unsafe_acl(path: Path) -> None:
    import win32security

    owner_sid = current_user_sid()
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{owner_sid}D:P(A;;GA;;;{owner_sid})(A;;GR;;;WD)",
        win32security.SDDL_REVISION_1,
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | getattr(win32security, "PROTECTED_DACL_SECURITY_INFORMATION", 0x80000000),
        descriptor.GetSecurityDescriptorOwner(),
        None,
        descriptor.GetSecurityDescriptorDacl(),
        None,
    )


def _create_junction(junction: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: {result.stderr or result.stdout}")


def _write_owner_private_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    protect_owner_private_path(path, owner_sid=current_user_sid(), directory=False)


def _preview(orchestrator: OwnerDestructiveOrchestrator, target: RecoveryTarget):
    return asyncio.run(
        orchestrator.preview(
            operation_id=str(uuid.uuid4()),
            targets=(_identity(target),),
        )
    )


def test_windows_purge_state_rejects_unsafe_acl_and_ancestor_junction_before_mutation(tmp_path: Path) -> None:
    target = _target()
    sid = current_user_sid()

    unsafe_root = tmp_path / "unsafe-purge-state"
    unsafe_root.mkdir()
    protect_owner_private_path(unsafe_root, owner_sid=sid, directory=True)
    _set_unsafe_acl(unsafe_root)
    with pytest.raises(OwnerDestructiveError) as unsafe_error:
        _orchestrator(unsafe_root, target)
    assert unsafe_error.value.code == "PURGE_STATE_INVALID"
    assert list(unsafe_root.iterdir()) == []

    junction_target = tmp_path / "junction-target"
    junction_target.mkdir()
    junction = tmp_path / "state-junction"
    _create_junction(junction, junction_target)
    try:
        with pytest.raises(OwnerDestructiveError) as junction_error:
            _orchestrator(junction / "purge-state", target)
        assert junction_error.value.code == "PURGE_STATE_INVALID"
        assert not (junction_target / "purge-state").exists()
    finally:
        os.rmdir(junction)


def test_windows_unsafe_recovery_receipt_is_rejected_before_maintenance_or_mutation(tmp_path: Path) -> None:
    target = _target()
    state_root = tmp_path / "purge-state"
    orchestrator = _orchestrator(state_root, target)
    preview = _preview(orchestrator, target)
    receipt_path, _, _ = orchestrator._paths(preview.operation_id)
    receipt_path.write_text("{}", encoding="utf-8")
    _set_unsafe_acl(receipt_path)

    with pytest.raises(OwnerDestructiveError) as caught:
        asyncio.run(
            orchestrator.execute(
                preview,
                typed_confirmation=preview.typed_confirmation,
                grant_secret="grant",
                drain_deadline=1000.0,
            )
        )
    assert caught.value.code == "PURGE_RECOVERY_STATE_MISMATCH"


def test_windows_purge_journal_result_are_owner_private_and_retry_reads_exact_result(tmp_path: Path) -> None:
    target = _target()
    state_root = tmp_path / "purge-state"
    orchestrator = _orchestrator(state_root, target)
    preview = _preview(orchestrator, target)
    receipt_path, recovery_state_path, result_path = orchestrator._paths(preview.operation_id)
    _write_owner_private_json(receipt_path, {"operation_id": preview.operation_id})
    _write_owner_private_json(recovery_state_path, {"phase": "S1_LOCAL_FACTLANE_PURGE_VERIFIED"})

    prepared = PreparedMaintenance(
        operation_id=preview.operation_id,
        original_generation=7,
        intent_revision=1,
        initially_open_instance_ids=(),
        registrations=(),
    )
    orchestrator._write_journal(
        preview,
        phase="PREPARED",
        prepared=prepared,
        targets=(target,),
        service_initial=None,
    )
    expected = PurgeSuccess(
        operation_id=preview.operation_id,
        preview_digest=preview.preview_digest,
        status="S1_LOCAL_FACTLANE_PURGE_VERIFIED",
        receipt_path=str(receipt_path),
        state_path=str(recovery_state_path),
        target_count=1,
        ready_instance_ids=(),
        resume_failures={},
    )
    orchestrator._persist_completed(expected)

    sid = current_user_sid()
    validate_owner_private_path(state_root, owner_sid=sid, directory=True)
    validate_owner_private_path(orchestrator._journal_path(preview.operation_id), owner_sid=sid, directory=False)
    validate_owner_private_path(result_path, owner_sid=sid, directory=False)
    validate_owner_private_path(receipt_path, owner_sid=sid, directory=False)
    validate_owner_private_path(recovery_state_path, owner_sid=sid, directory=False)

    retry = _orchestrator(state_root, target)
    actual = asyncio.run(
        retry.execute(
            preview,
            typed_confirmation=preview.typed_confirmation,
            grant_secret="already-consumed",
            drain_deadline=1000.0,
        )
    )
    assert actual == expected


def test_windows_purge_publication_pins_parent_against_junction_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    state_root = tmp_path / "purge-state"
    orchestrator = _orchestrator(state_root, target)
    preview = _preview(orchestrator, target)
    receipt_path, recovery_state_path, result_path = orchestrator._paths(preview.operation_id)
    _write_owner_private_json(receipt_path, {"operation_id": preview.operation_id})
    _write_owner_private_json(recovery_state_path, {"phase": "S1_LOCAL_FACTLANE_PURGE_VERIFIED"})
    result = PurgeSuccess(
        operation_id=preview.operation_id,
        preview_digest=preview.preview_digest,
        status="S1_LOCAL_FACTLANE_PURGE_VERIFIED",
        receipt_path=str(receipt_path),
        state_path=str(recovery_state_path),
        target_count=1,
        ready_instance_ids=(),
        resume_failures={},
    )

    redirected = tmp_path / "redirected"
    redirected.mkdir()
    parked = tmp_path / "parked-state"
    original_rename = owner_destructive_module._windows_rename_handle_replace_relative
    race = {"blocked": False, "moved": False}

    def racing_rename(source_handle, parent_handle, destination: Path) -> None:
        try:
            os.rename(state_root, parked)
        except OSError:
            race["blocked"] = True
            original_rename(source_handle, parent_handle, destination)
            return

        race["moved"] = True
        try:
            _create_junction(state_root, redirected)
            original_rename(source_handle, parent_handle, destination)
        finally:
            if state_root.exists():
                os.rmdir(state_root)
            os.rename(parked, state_root)

    monkeypatch.setattr(owner_destructive_module, "_windows_rename_handle_replace_relative", racing_rename)
    orchestrator._persist_completed(result)

    assert race == {"blocked": True, "moved": False}
    assert result_path.is_file()
    assert not (redirected / result_path.name).exists()
    validate_owner_private_path(result_path, owner_sid=current_user_sid(), directory=False)


def test_windows_purge_publication_retains_exact_temp_handle_against_leaf_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    state_root = tmp_path / "purge-state"
    orchestrator = _orchestrator(state_root, target)
    preview = _preview(orchestrator, target)
    receipt_path, recovery_state_path, result_path = orchestrator._paths(preview.operation_id)
    _write_owner_private_json(receipt_path, {"operation_id": preview.operation_id})
    _write_owner_private_json(recovery_state_path, {"phase": "S1_LOCAL_FACTLANE_PURGE_VERIFIED"})
    result = PurgeSuccess(
        operation_id=preview.operation_id,
        preview_digest=preview.preview_digest,
        status="S1_LOCAL_FACTLANE_PURGE_VERIFIED",
        receipt_path=str(receipt_path),
        state_path=str(recovery_state_path),
        target_count=1,
        ready_instance_ids=(),
        resume_failures={},
    )

    original_rename = owner_destructive_module._windows_rename_handle_replace_relative
    race = {"rename_blocked": False, "renamed": False, "attacker_installed": False}
    attacker_marker = "ATTACKER_TEMP_LEAF"

    def racing_rename(source_handle, parent_handle, destination: Path) -> None:
        stages = list(state_root.glob(f".{result_path.name}.*.tmp"))
        assert len(stages) == 1
        stage = stages[0]
        parked_stage = stage.with_name(stage.name + ".parked")
        attacker = stage.with_name(stage.name + ".attacker")
        attacker.write_text(json.dumps({"marker": attacker_marker}), encoding="utf-8")
        protect_owner_private_path(attacker, owner_sid=current_user_sid(), directory=False)
        try:
            os.replace(stage, parked_stage)
        except OSError:
            race["rename_blocked"] = True
        else:
            race["renamed"] = True
            os.replace(attacker, stage)
            race["attacker_installed"] = True
        original_rename(source_handle, parent_handle, destination)
        for leftover in (stage, parked_stage, attacker):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass

    monkeypatch.setattr(owner_destructive_module, "_windows_rename_handle_replace_relative", racing_rename)
    orchestrator._persist_completed(result)

    assert race["rename_blocked"] or race["renamed"]
    assert json.loads(result_path.read_text(encoding="utf-8")) == {"version": 1, **result.to_mapping()}
    assert attacker_marker not in result_path.read_text(encoding="utf-8")
    validate_owner_private_path(result_path, owner_sid=current_user_sid(), directory=False)
