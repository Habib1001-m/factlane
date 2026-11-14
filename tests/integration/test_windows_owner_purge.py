from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter, _establish_embedding_compatibility, trusted_write_context_for_profile
from factlane.contract import AdapterError, ScopeContext
from factlane.embeddings import EmbeddingProfile
from factlane.recovery import PM1, RecoveryActivationHooks, RecoveryHold, RecoveryPlan, RecoveryScope, RecoveryTarget
from factlane.storage import SQLiteVecEngine
from factlane.windows_maintenance import WindowsExclusiveMaintenanceLease, WindowsFileIdentity
from factlane.windows_owner_purge import WindowsOwnerPurgeOperator, resolve_windows_permanent_purge_targets_exclusive
from factlane.windows_security import current_user_sid, protect_owner_private_path, validate_owner_private_path


native_windows = pytest.mark.skipif(os.name != "nt", reason="requires native Windows")


@pytest.fixture(autouse=True)
def _owner_private_windows_tmp_path(tmp_path: Path) -> None:
    if os.name == "nt":
        protect_owner_private_path(tmp_path, owner_sid=current_user_sid(), directory=True)


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


class _FixedProvider:
    def __init__(self, profile: EmbeddingProfile) -> None:
        self.profile = profile
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        vector = [0.0] * (self.profile.output_dimension - 1) + [1.0]
        return [list(vector) for _ in texts]

    def embed_query(self, _text: str) -> list[float]:
        self.query_calls += 1
        return [0.0] * (self.profile.output_dimension - 1) + [1.0]


def _provider_status(profile: EmbeddingProfile) -> dict[str, object]:
    return {
        "provider_kind": profile.provider_kind,
        "model": profile.base_model_identity,
        "digest": profile.model_digest,
        "ollama_version": "test-fixture",
        "semantic_family": profile.semantic_family,
        "native_dimension": profile.source_dimension,
        "output_dimension": profile.output_dimension,
        "effective_context_window": profile.minimum_context_window,
    }


async def _open_adapter(tmp_path: Path) -> tuple[SQLiteVecEngine, MemoryAdapter]:
    profile = _profile()
    engine = SQLiteVecEngine(str(tmp_path / "memory.db"), profile)
    await engine.open()
    provider = _FixedProvider(profile)
    await _establish_embedding_compatibility(engine, provider, _provider_status(profile))
    return engine, MemoryAdapter(
        engine,
        provider,  # type: ignore[arg-type]
        trusted_write_context=trusted_write_context_for_profile(
            "owner-current",
            contributor_ref="windows-owner-purge-tests",
        ),
    )


def _provenance(key: str, marker: str) -> dict[str, str]:
    return {
        "source_class": "CURRENT_REPO",
        "source_ref": key,
        "source_hash": marker * 64,
        "review_ref": "windows-owner-purge-tests",
        "extraction_method": "AUTOMATED_CHECK",
    }


async def _store(adapter: MemoryAdapter, *, key: str, fact: str, marker: str) -> dict[str, Any]:
    stamp = "2026-11-06T00:00:00Z"
    result = await adapter.store(
        fact=fact,
        scope="PROJECT",
        memory_type="PROJECT_LEARNED_FACT",
        source_provenance=_provenance(key, marker),
        freshness_policy={"kind": "manual"},
        idempotency_key=key,
        project_id="factlane",
        source_timestamp=stamp,
        last_verified_at=stamp,
        verified_by="OWNER",
        requested_lifecycle_state="VALIDATED_CURRENT",
        confidence=0.95,
        tags=["subject:windows-owner-purge-test"],
    )
    return result["results"][0]


async def _row(engine: SQLiteVecEngine, memory_id: str) -> dict[str, Any]:
    rows = await engine.get_record(memory_id, ScopeContext("PROJECT", project_id="factlane"), history=True)
    assert len(rows) == 1
    return rows[0]


async def _prepare_database(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any], str]:
    engine, adapter = await _open_adapter(tmp_path)
    token = "windowspurgetargetmarker9713"
    target = await _store(adapter, key="windows-purge-target", fact=f"Sensitive target {token}.", marker="a")
    survivor = await _store(
        adapter,
        key="windows-purge-survivor",
        fact="Unrelated survivor must remain exact.",
        marker="b",
    )
    target_row = await _row(engine, target["memory_id"])
    survivor_row = await _row(engine, survivor["memory_id"])
    assert engine.conn is not None
    engine.conn.execute(
        "INSERT INTO memory_graph(source_hash,target_hash,similarity,connection_types,metadata,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            target_row["native_content_hash"],
            survivor_row["native_content_hash"],
            0.9,
            '["related"]',
            "{}",
            1.0,
        ),
    )
    await adapter.close()
    db = tmp_path / "memory.db"
    checkpoint = sqlite3.connect(db, timeout=5.0, isolation_level=None)
    try:
        assert tuple(checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) == (0, 0, 0)
    finally:
        checkpoint.close()
    assert token.encode("utf-8") in db.read_bytes()
    return db, target_row, survivor_row, token


def _plan(db: Path, row: dict[str, Any], operation_id: str) -> RecoveryPlan:
    return RecoveryPlan(
        operation_id=operation_id,
        db_path=str(db),
        profile=_profile(),
        targets=(
            RecoveryTarget(
                record_id=row["record_id"],
                memory_id=row["memory_id"],
                revision=int(row["revision"]),
                scope=RecoveryScope("PROJECT", project_id="factlane"),
                expected_lifecycle=row["lifecycle_state"],
                expected_materialization=PM1,
                expected_native_hash=row["native_content_hash"],
            ),
        ),
        retained_identity_fields_classified_non_sensitive=True,
    )


def _native_identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return int(info.st_dev), int(info.st_ino)


def _synthetic_identity(path: Path) -> WindowsFileIdentity:
    device, inode = _native_identity(path)
    return WindowsFileIdentity(device, inode.to_bytes(16, "little", signed=False), str(path.resolve()))


class _VacuumLostResponseConnection(sqlite3.Connection):
    fail_after_vacuum = False

    def execute(self, sql: str, parameters=(), /):  # type: ignore[override]
        cursor = super().execute(sql, parameters)
        if sql.strip().upper() == "VACUUM" and type(self).fail_after_vacuum:
            type(self).fail_after_vacuum = False
            raise sqlite3.OperationalError("synthetic lost VACUUM response")
        return cursor


class _FakeInPlaceLease:
    acquire_count = 0
    retain_count = 0

    @classmethod
    def reset(cls) -> None:
        cls.acquire_count = 0
        cls.retain_count = 0
        _VacuumLostResponseConnection.fail_after_vacuum = False

    def __init__(self, db_path: str) -> None:
        type(self).acquire_count += 1
        self.db = Path(db_path)
        self._native_initial = _native_identity(self.db)
        self.identity = _synthetic_identity(self.db)
        self.connection = sqlite3.connect(
            self.db,
            timeout=5.0,
            isolation_level=None,
            factory=_VacuumLostResponseConnection,
        )
        self.connection.row_factory = sqlite3.Row
        checkpoint = tuple(self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        assert checkpoint in {(0, 0, 0), (0, -1, -1)}
        assert str(self.connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower() == "delete"
        self.connection.execute("PRAGMA synchronous=FULL")
        assert str(self.connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()[0]).lower() == "exclusive"
        self.connection.execute("BEGIN EXCLUSIVE")
        self.connection.commit()
        self.retained = False
        self.closed = False
        self._runtime_activation_prepared = False

    def revalidate(self) -> WindowsFileIdentity:
        if os.name == "nt":
            assert _native_identity(self.db) == self._native_initial
        return self.identity

    def flush_file_buffers(self) -> None:
        self.connection.execute("PRAGMA schema_version").fetchone()

    def prepare_runtime_activation(self) -> None:
        if self._runtime_activation_prepared:
            return
        self.connection.close()
        self.connection = sqlite3.connect(self.db, timeout=5.0, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=5000")
        assert str(self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal"
        self.connection.execute("PRAGMA synchronous=NORMAL")
        assert str(self.connection.execute("PRAGMA locking_mode=NORMAL").fetchone()[0]).lower() == "normal"
        self._runtime_activation_prepared = True

    def retain_fail_closed(self) -> None:
        type(self).retain_count += 1
        self.retained = True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if not self.retained:
            self.connection.close()


def _hooks(operation_id: str, events: list[str], *, fail_publish: bool = False) -> RecoveryActivationHooks:
    async def ready(_result, _lease) -> None:
        events.append("activation-ready")

    async def running(_result, _lease) -> None:
        events.append("publish-running")
        if fail_publish:
            raise RuntimeError("synthetic publish failure")

    return RecoveryActivationHooks(operation_id=operation_id, activation_ready=ready, publish_running=running)


def _assert_interlock(db: Path, operation_id: str | None) -> None:
    conn = sqlite3.connect(db, timeout=5.0, isolation_level=None)
    try:
        row = conn.execute("SELECT value FROM adapter_meta WHERE key='sensitive_recovery_interlock'").fetchone()
        assert (None if row is None else str(row[0])) == operation_id
    finally:
        conn.close()


def _assert_purged(db: Path, target: dict[str, Any], survivor: dict[str, Any], token: str) -> None:
    conn = sqlite3.connect(db, timeout=5.0, isolation_level=None)
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tombstone = conn.execute(
            "SELECT fact,lifecycle_state FROM adapter_records WHERE record_id=?",
            (target["record_id"],),
        ).fetchone()
        assert tombstone == ("[purged-sensitive-memory]", "QUARANTINED")
        assert conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content_hash=?",
            (target["native_content_hash"],),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_graph WHERE source_hash=? OR target_hash=?",
            (target["native_content_hash"], target["native_content_hash"]),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_content_fts WHERE memory_content_fts MATCH ?",
            (token,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT fact FROM adapter_records WHERE record_id=?",
            (survivor["record_id"],),
        ).fetchone()[0] == survivor["fact"]
        assert conn.execute(
            "SELECT COUNT(*) FROM lifecycle_events WHERE action='SENSITIVE_PURGE' AND record_id=?",
            (target["record_id"],),
        ).fetchone()[0] == 1
    finally:
        conn.close()
    payloads = [db.read_bytes()]
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(db) + suffix)
        if sidecar.exists():
            payloads.append(sidecar.read_bytes())
    assert token.encode("utf-8") not in b"".join(payloads)


def test_default_path_fails_closed_until_in_place_kernel_lands(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    db.write_bytes(b"not-mutated")
    plan = RecoveryPlan(
        operation_id="windows-kernel-hold",
        db_path=str(db),
        profile=_profile(),
        targets=(
            RecoveryTarget(
                record_id="record",
                memory_id="memory",
                revision=0,
                scope=RecoveryScope("PROJECT", project_id="factlane"),
                expected_lifecycle="VALIDATED_CURRENT",
                expected_materialization=PM1,
                expected_native_hash="a" * 64,
            ),
        ),
        retained_identity_fields_classified_non_sensitive=True,
    )
    events: list[str] = []
    with pytest.raises(RecoveryHold) as caught:
        asyncio.run(
            WindowsOwnerPurgeOperator().execute(
                plan,
                receipt_path=str(tmp_path / "receipt.json"),
                activation_hooks=_hooks(plan.operation_id, events),
            )
        )
    assert caught.value.code == "HOLD_WINDOWS_PURGE_MAINTENANCE_UNAVAILABLE"
    assert db.read_bytes() == b"not-mutated"
    assert events == []


def test_in_place_panel_purge_preserves_identity_semantics_and_activation_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_promotion(*_args, **_kwargs):
        raise AssertionError("Windows panel purge must never use rename/promotion")

    monkeypatch.setattr(WindowsExclusiveMaintenanceLease, "promote_replacement", forbidden_promotion)
    monkeypatch.setattr(
        WindowsExclusiveMaintenanceLease,
        "_rename_handle_replace",
        staticmethod(forbidden_promotion),
    )
    _FakeInPlaceLease.reset()
    db, target, survivor, token = asyncio.run(_prepare_database(tmp_path))
    native_before = _native_identity(db)
    plan = _plan(db, target, "windows-panel-success")
    events: list[str] = []
    receipt = tmp_path / "purge.receipt.json"
    state = tmp_path / "purge.state.json"
    result = asyncio.run(
        WindowsOwnerPurgeOperator(maintenance_lease_factory=_FakeInPlaceLease).execute(
            plan,
            receipt_path=str(receipt),
            state_path=str(state),
            activation_hooks=_hooks(plan.operation_id, events),
        )
    )
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert result.logical_purge_committed is True and result.promoted is True
    assert events == ["activation-ready", "publish-running"]
    assert _FakeInPlaceLease.acquire_count == 1
    if os.name == "nt":
        assert _native_identity(db) == native_before
    persisted = json.loads(state.read_text())
    assert persisted["phase"] == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    receipt_payload = json.loads(receipt.read_text())
    assert receipt_payload["windows_panel_scope"] == "OWNER_CONTROL_PANEL_ONLY"
    assert receipt_payload["in_place_vacuum"] == "PASS"
    assert receipt_payload["database_file_identity_preserved"] is True
    assert "vacuum_into_sanitized_image" not in receipt_payload
    if os.name == "nt":
        sid = current_user_sid()
        validate_owner_private_path(state, owner_sid=sid, directory=False)
        validate_owner_private_path(receipt, owner_sid=sid, directory=False)
    _assert_interlock(db, None)
    _assert_purged(db, target, survivor, token)


def test_lost_vacuum_response_leaves_interlock_and_retry_does_not_duplicate_logical_purge(tmp_path: Path) -> None:
    _FakeInPlaceLease.reset()
    _VacuumLostResponseConnection.fail_after_vacuum = True
    db, target, survivor, token = asyncio.run(_prepare_database(tmp_path))
    plan = _plan(db, target, "windows-panel-vacuum-retry")
    receipt = tmp_path / "vacuum.receipt.json"
    state = tmp_path / "vacuum.state.json"
    first_events: list[str] = []
    with pytest.raises(RecoveryHold) as caught:
        asyncio.run(
            WindowsOwnerPurgeOperator(maintenance_lease_factory=_FakeInPlaceLease).execute(
                plan,
                receipt_path=str(receipt),
                state_path=str(state),
                activation_hooks=_hooks(plan.operation_id, first_events),
            )
        )
    assert caught.value.code == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
    assert first_events == []
    assert json.loads(state.read_text())["phase"] == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
    _assert_interlock(db, plan.operation_id)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT fact FROM adapter_records WHERE record_id=?",
            (target["record_id"],),
        ).fetchone()[0] == "[purged-sensitive-memory]"
        assert conn.execute(
            "SELECT COUNT(*) FROM lifecycle_events WHERE action='SENSITIVE_PURGE' AND record_id=?",
            (target["record_id"],),
        ).fetchone()[0] == 1
    finally:
        conn.close()

    async def ordinary_runtime_is_blocked() -> None:
        engine = SQLiteVecEngine(str(db), _profile())
        with pytest.raises(AdapterError) as blocked:
            await engine.open()
        assert blocked.value.code == "MAINTENANCE_IN_PROGRESS"

    asyncio.run(ordinary_runtime_is_blocked())
    retry_events: list[str] = []
    result = asyncio.run(
        WindowsOwnerPurgeOperator(maintenance_lease_factory=_FakeInPlaceLease).execute(
            plan,
            receipt_path=str(receipt),
            state_path=str(state),
            activation_hooks=_hooks(plan.operation_id, retry_events),
        )
    )
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert retry_events == ["activation-ready", "publish-running"]
    assert _FakeInPlaceLease.acquire_count == 2
    _assert_interlock(db, None)
    _assert_purged(db, target, survivor, token)


def test_publish_failure_restores_interlock_and_retry_does_not_redelete(tmp_path: Path) -> None:
    _FakeInPlaceLease.reset()
    db, target, survivor, token = asyncio.run(_prepare_database(tmp_path))
    plan = _plan(db, target, "windows-panel-publish-retry")
    receipt = tmp_path / "publish.receipt.json"
    state = tmp_path / "publish.state.json"
    first_events: list[str] = []
    with pytest.raises(RecoveryHold) as caught:
        asyncio.run(
            WindowsOwnerPurgeOperator(maintenance_lease_factory=_FakeInPlaceLease).execute(
                plan,
                receipt_path=str(receipt),
                state_path=str(state),
                activation_hooks=_hooks(plan.operation_id, first_events, fail_publish=True),
            )
        )
    assert caught.value.code == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
    assert first_events == ["activation-ready", "publish-running"]
    assert json.loads(state.read_text())["phase"] == "S1_LOCAL_FACTLANE_ACTIVATION_READY"
    _assert_interlock(db, plan.operation_id)
    retry_events: list[str] = []
    result = asyncio.run(
        WindowsOwnerPurgeOperator(maintenance_lease_factory=_FakeInPlaceLease).execute(
            plan,
            receipt_path=str(receipt),
            state_path=str(state),
            activation_hooks=_hooks(plan.operation_id, retry_events),
        )
    )
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert retry_events == ["activation-ready", "publish-running"]
    _assert_interlock(db, None)
    _assert_purged(db, target, survivor, token)


def test_exclusive_target_resolver_uses_same_in_place_lease_without_logical_mutation(tmp_path: Path) -> None:
    _FakeInPlaceLease.reset()
    db, target, _survivor, token = asyncio.run(_prepare_database(tmp_path))
    identity_before = _native_identity(db)
    resolved = asyncio.run(
        resolve_windows_permanent_purge_targets_exclusive(
            operation_id="windows-panel-resolver",
            db_path=str(db),
            profile=_profile(),
            identities=(
                {
                    "scope": "PROJECT",
                    "project_id": "factlane",
                    "worktree_id": None,
                    "workflow_id": None,
                    "agent_id": None,
                    "memory_id": target["memory_id"],
                    "record_id": target["record_id"],
                    "revision": int(target["revision"]),
                },
            ),
            maintenance_lease_factory=_FakeInPlaceLease,
        )
    )
    assert len(resolved) == 1
    assert resolved[0].record_id == target["record_id"]
    assert resolved[0].memory_id == target["memory_id"]
    assert resolved[0].expected_materialization == PM1
    assert resolved[0].expected_native_hash == target["native_content_hash"]
    assert _native_identity(db) == identity_before
    assert token.encode("utf-8") in db.read_bytes()
    assert _FakeInPlaceLease.acquire_count == 1


@native_windows
def test_native_windows_in_place_kernel_runs_real_panel_purge_without_file_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_promotion(*_args, **_kwargs):
        raise AssertionError("native Windows panel purge must never use rename/promotion")

    monkeypatch.setattr(WindowsExclusiveMaintenanceLease, "promote_replacement", forbidden_promotion)
    monkeypatch.setattr(
        WindowsExclusiveMaintenanceLease,
        "_rename_handle_replace",
        staticmethod(forbidden_promotion),
    )
    db, target, survivor, token = asyncio.run(_prepare_database(tmp_path))
    identity_before = _native_identity(db)
    resolved = asyncio.run(
        resolve_windows_permanent_purge_targets_exclusive(
            operation_id="windows-panel-native-resolver",
            db_path=str(db),
            profile=_profile(),
            identities=(
                {
                    "scope": "PROJECT",
                    "project_id": "factlane",
                    "worktree_id": None,
                    "workflow_id": None,
                    "agent_id": None,
                    "memory_id": target["memory_id"],
                    "record_id": target["record_id"],
                    "revision": int(target["revision"]),
                },
            ),
        )
    )
    assert len(resolved) == 1 and resolved[0].record_id == target["record_id"]
    assert _native_identity(db) == identity_before
    assert token.encode("utf-8") in db.read_bytes()
    plan = _plan(db, target, "windows-panel-native-real")
    events: list[str] = []
    receipt = tmp_path / "native.receipt.json"
    state = tmp_path / "native.state.json"
    result = asyncio.run(
        WindowsOwnerPurgeOperator().execute(
            plan,
            receipt_path=str(receipt),
            state_path=str(state),
            activation_hooks=_hooks(plan.operation_id, events),
        )
    )
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert events == ["activation-ready", "publish-running"]
    assert _native_identity(db) == identity_before
    validate_owner_private_path(state, owner_sid=current_user_sid(), directory=False)
    validate_owner_private_path(receipt, owner_sid=current_user_sid(), directory=False)
    _assert_interlock(db, None)
    _assert_purged(db, target, survivor, token)

    async def runtime_reopens() -> None:
        engine = SQLiteVecEngine(str(db), _profile())
        await engine.open()
        await engine.close()

    asyncio.run(runtime_reopens())


@native_windows
def test_native_precommit_hold_restores_runtime_mode_and_releases_exclusion(tmp_path: Path) -> None:
    db, target, _survivor, token = asyncio.run(_prepare_database(tmp_path))
    bad_target = dict(target)
    bad_target["lifecycle_state"] = "HISTORICAL"
    plan = _plan(db, bad_target, "windows-panel-native-precommit-hold")
    with pytest.raises(RecoveryHold) as caught:
        asyncio.run(
            WindowsOwnerPurgeOperator().execute(
                plan,
                receipt_path=str(tmp_path / "hold.receipt.json"),
                state_path=str(tmp_path / "hold.state.json"),
                activation_hooks=_hooks(plan.operation_id, []),
            )
        )
    assert caught.value.code == "HOLD_TARGET_DRIFT_NO_MUTATION"
    assert token.encode("utf-8") in db.read_bytes()
    _assert_interlock(db, None)

    async def runtime_reopens() -> None:
        engine = SQLiteVecEngine(str(db), _profile())
        await engine.open()
        await engine.close()

    asyncio.run(runtime_reopens())


@native_windows
def test_native_post_vacuum_failure_keeps_interlock_and_retry_seals_without_second_purge_event(tmp_path: Path) -> None:
    class _FailAfterVacuumOnce(WindowsOwnerPurgeOperator):
        failed = False

        def _verify_post_vacuum(self, conn, plan, survivor, lifecycle):
            if not type(self).failed:
                type(self).failed = True
                raise RecoveryHold(
                    "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE",
                    "synthetic native failure immediately after in-place VACUUM",
                )
            return super()._verify_post_vacuum(conn, plan, survivor, lifecycle)

    db, target, survivor, token = asyncio.run(_prepare_database(tmp_path))
    identity_before = _native_identity(db)
    plan = _plan(db, target, "windows-panel-native-vacuum-retry")
    receipt = tmp_path / "native-vacuum.receipt.json"
    state = tmp_path / "native-vacuum.state.json"
    with pytest.raises(RecoveryHold) as caught:
        asyncio.run(
            _FailAfterVacuumOnce().execute(
                plan,
                receipt_path=str(receipt),
                state_path=str(state),
                activation_hooks=_hooks(plan.operation_id, []),
            )
        )
    assert caught.value.code == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
    assert json.loads(state.read_text())["phase"] == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
    assert _native_identity(db) == identity_before
    _assert_interlock(db, plan.operation_id)
    probe = sqlite3.connect(db)
    try:
        assert probe.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert probe.execute(
            "SELECT fact FROM adapter_records WHERE record_id=?",
            (target["record_id"],),
        ).fetchone()[0] == "[purged-sensitive-memory]"
        assert probe.execute(
            "SELECT COUNT(*) FROM lifecycle_events WHERE action='SENSITIVE_PURGE' AND record_id=?",
            (target["record_id"],),
        ).fetchone()[0] == 1
    finally:
        probe.close()

    async def runtime_is_blocked() -> None:
        engine = SQLiteVecEngine(str(db), _profile())
        with pytest.raises(AdapterError) as blocked:
            await engine.open()
        assert blocked.value.code == "MAINTENANCE_IN_PROGRESS"

    asyncio.run(runtime_is_blocked())
    retry_events: list[str] = []
    result = asyncio.run(
        WindowsOwnerPurgeOperator().execute(
            plan,
            receipt_path=str(receipt),
            state_path=str(state),
            activation_hooks=_hooks(plan.operation_id, retry_events),
        )
    )
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert retry_events == ["activation-ready", "publish-running"]
    assert _native_identity(db) == identity_before
    _assert_interlock(db, None)
    _assert_purged(db, target, survivor, token)
    final = sqlite3.connect(db)
    try:
        assert final.execute(
            "SELECT COUNT(*) FROM lifecycle_events WHERE action='SENSITIVE_PURGE' AND record_id=?",
            (target["record_id"],),
        ).fetchone()[0] == 1
    finally:
        final.close()


@native_windows
def test_native_publish_failure_restores_interlock_releases_for_retry_and_does_not_redelete(tmp_path: Path) -> None:
    db, target, survivor, token = asyncio.run(_prepare_database(tmp_path))
    identity_before = _native_identity(db)
    plan = _plan(db, target, "windows-panel-native-publish-retry")
    receipt = tmp_path / "native-publish.receipt.json"
    state = tmp_path / "native-publish.state.json"
    first_events: list[str] = []
    with pytest.raises(RecoveryHold) as caught:
        asyncio.run(
            WindowsOwnerPurgeOperator().execute(
                plan,
                receipt_path=str(receipt),
                state_path=str(state),
                activation_hooks=_hooks(plan.operation_id, first_events, fail_publish=True),
            )
        )
    assert caught.value.code == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
    assert first_events == ["activation-ready", "publish-running"]
    assert json.loads(state.read_text())["phase"] == "S1_LOCAL_FACTLANE_ACTIVATION_READY"
    assert _native_identity(db) == identity_before
    _assert_interlock(db, plan.operation_id)

    retry_events: list[str] = []
    result = asyncio.run(
        WindowsOwnerPurgeOperator().execute(
            plan,
            receipt_path=str(receipt),
            state_path=str(state),
            activation_hooks=_hooks(plan.operation_id, retry_events),
        )
    )
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert retry_events == ["activation-ready", "publish-running"]
    assert _native_identity(db) == identity_before
    _assert_interlock(db, None)
    _assert_purged(db, target, survivor, token)
    final = sqlite3.connect(db)
    try:
        assert final.execute(
            "SELECT COUNT(*) FROM lifecycle_events WHERE action='SENSITIVE_PURGE' AND record_id=?",
            (target["record_id"],),
        ).fetchone()[0] == 1
    finally:
        final.close()
