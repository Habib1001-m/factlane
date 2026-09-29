from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter
from factlane.contract import PUBLIC_TOOL_NAMES, ScopeContext
from factlane.embeddings import EmbeddingProfile
from factlane.recovery import MaintenanceLease, PM0, PM1, RecoveryHold, RecoveryPlan, RecoveryScope, RecoveryTarget, SensitiveMemoryRecoveryOperator
from factlane.storage import SQLiteVecEngine


def profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="test-256", provider_kind="OLLAMA_LOCAL",
        base_model_identity="nomic-embed-text:latest",
        model_digest="0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
        source_dimension=768, output_dimension=256,
        normalization_policy="OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
        distance_metric="cosine", projection_version="ollama-dimensions-v1",
        document_prefix="search_document: ", query_prefix="search_query: ",
    )


class FixedProvider:
    def __init__(self, embedding_profile: EmbeddingProfile) -> None:
        self.profile = embedding_profile
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


async def _open_adapter(tmp_path: Path, filename: str = "memory.db") -> tuple[SQLiteVecEngine, MemoryAdapter]:
    p = profile()
    engine = SQLiteVecEngine(str(tmp_path / filename), p)
    await engine.open()
    return engine, MemoryAdapter(engine, FixedProvider(p))  # type: ignore[arg-type]


def _provenance(key: str, marker: str) -> dict[str, str]:
    return {
        "source_class": "CURRENT_REPO", "source_ref": key,
        "source_hash": marker * 64, "review_ref": "sensitive-recovery-tests",
        "extraction_method": "AUTOMATED_CHECK",
    }


async def _store(adapter: MemoryAdapter, *, key: str, fact: str, marker: str) -> dict[str, Any]:
    stamp = "2026-09-29T00:00:00Z"
    result = await adapter.store(
        fact=fact, scope="PROJECT", memory_type="PROJECT_LEARNED_FACT",
        source_provenance=_provenance(key, marker), freshness_policy={"kind": "manual"},
        idempotency_key=key, project_id="factlane", source_timestamp=stamp,
        last_verified_at=stamp, verified_by="OWNER", requested_lifecycle_state="VALIDATED_CURRENT",
        confidence=0.95, tags=["subject:sensitive-recovery-test"],
    )
    return result["results"][0]


async def _target_row(engine: SQLiteVecEngine, memory_id: str) -> dict[str, Any]:
    rows = await engine.get_record(memory_id, ScopeContext("PROJECT", project_id="factlane"), history=True)
    assert len(rows) == 1
    return rows[0]


def _plan(db: Path, row: dict[str, Any], *, operation: str, materialization: str = PM1, incident_class: str = "S1") -> RecoveryPlan:
    return RecoveryPlan(
        operation_id=operation, db_path=str(db), profile=profile(),
        targets=(RecoveryTarget(
            record_id=row["record_id"], memory_id=row["memory_id"], revision=int(row["revision"]),
            scope=RecoveryScope("PROJECT", project_id="factlane"),
            expected_lifecycle=row["lifecycle_state"], expected_materialization=materialization,
            expected_native_hash=row["native_content_hash"],
        ),), incident_class=incident_class, retained_identity_fields_classified_non_sensitive=True,
    )


def _run(plan: RecoveryPlan, tmp_path: Path, *, operator: SensitiveMemoryRecoveryOperator | None = None):
    op = operator or SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _: [])
    return asyncio.run(op.execute(plan, receipt_path=str(tmp_path / f"{plan.operation_id}.receipt.json")))


def _fact(db: Path, record_id: str) -> str:
    conn = sqlite3.connect(db)
    try:
        return str(conn.execute("SELECT fact FROM adapter_records WHERE record_id = ?", (record_id,)).fetchone()[0])
    finally:
        conn.close()


def test_unknown_propagation_is_u1_and_cannot_construct_mutating_plan(tmp_path: Path) -> None:
    target = RecoveryTarget(record_id="r1", memory_id="m1", revision=0, scope=RecoveryScope("PROJECT", project_id="factlane"), expected_lifecycle="VALIDATED_CURRENT", expected_materialization=PM1, expected_native_hash="a" * 64)
    with pytest.raises(ValueError, match="U1"):
        RecoveryPlan(operation_id="unknown-propagation", db_path=str(tmp_path / "memory.db"), profile=profile(), targets=(target,), propagation_state="UNKNOWN")


def test_nonquiescent_handle_inventory_holds_before_mutation(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], str]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="nonquiescent", fact="Synthetic recovery marker remains unchanged.", marker="a")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row, row["fact"]
    row, before = asyncio.run(prepare())
    plan = _plan(tmp_path / "memory.db", row, operation="nonquiescent-op")
    operator = SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _: [{"pid": 42, "fd": "9", "path": str(tmp_path / "memory.db")}])
    with pytest.raises(RecoveryHold) as caught:
        _run(plan, tmp_path, operator=operator)
    assert caught.value.code == "HOLD_NONQUIESCENT_NO_MUTATION"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == before


def test_pm2_missing_vector_holds_without_tombstoning(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], str]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="pm2", fact="Synthetic PM2 recovery marker.", marker="b")
        row = await _target_row(engine, item["memory_id"])
        assert engine.conn is not None
        native_id = engine.conn.execute("SELECT id FROM memories WHERE content_hash = ?", (row["native_content_hash"],)).fetchone()[0]
        engine.conn.execute("DELETE FROM memory_embeddings WHERE rowid = ?", (native_id,))
        engine.conn.commit()
        await adapter.close()
        return row, row["fact"]
    row, before_fact = asyncio.run(prepare())
    plan = _plan(tmp_path / "memory.db", row, operation="pm2-op")
    with pytest.raises(RecoveryHold) as caught:
        _run(plan, tmp_path)
    assert caught.value.code == "HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == before_fact


def test_pm3_orphan_vector_is_never_attributed_heuristically(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], str]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="pm3", fact="Synthetic PM3 recovery marker.", marker="c")
        row = await _target_row(engine, item["memory_id"])
        assert engine.conn is not None
        engine.conn.execute("DELETE FROM memories WHERE content_hash = ?", (row["native_content_hash"],))
        engine.conn.commit()
        await adapter.close()
        return row, row["fact"]
    row, before_fact = asyncio.run(prepare())
    plan = _plan(tmp_path / "memory.db", row, operation="pm3-op")
    with pytest.raises(RecoveryHold) as caught:
        _run(plan, tmp_path)
    assert caught.value.code == "HOLD_UNATTRIBUTABLE_VECTOR_NO_MUTATION"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == before_fact


def test_belief_reference_is_unexpected_derived_state(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], str]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="belief", fact="Synthetic belief-linked recovery marker.", marker="d")
        row = await _target_row(engine, item["memory_id"])
        assert engine.conn is not None
        engine.conn.execute("INSERT INTO beliefs(belief_hash, content, confidence, status, created_at, updated_at, derived_from, contradicted_by, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '{}')", ("belief-1", "derived synthetic statement", 0.5, "candidate", "2026-09-29", "2026-09-29", json.dumps([row["native_content_hash"]])))
        engine.conn.commit()
        await adapter.close()
        return row, row["fact"]
    row, before_fact = asyncio.run(prepare())
    plan = _plan(tmp_path / "memory.db", row, operation="belief-op")
    with pytest.raises(RecoveryHold) as caught:
        _run(plan, tmp_path)
    assert caught.value.code == "HOLD_UNEXPECTED_DERIVED_STATE_NO_MUTATION"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == before_fact


def test_pm1_success_scrubs_payload_and_preserves_unrelated_record(tmp_path: Path) -> None:
    marker = "SYNTHETIC_INCIDENT_MARKER_ALPHA_7429"
    async def prepare() -> tuple[dict[str, Any], dict[str, Any]]:
        engine, adapter = await _open_adapter(tmp_path)
        target_item = await _store(adapter, key="success-target", fact=f"Target {marker}.", marker="e")
        survivor_item = await _store(adapter, key="success-survivor", fact="Unrelated current record must survive.", marker="f")
        target_row = await _target_row(engine, target_item["memory_id"])
        survivor_row = await _target_row(engine, survivor_item["memory_id"])
        assert engine.conn is not None
        engine.conn.execute("INSERT INTO memory_graph(source_hash, target_hash, similarity, connection_types, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)", (target_row["native_content_hash"], survivor_row["native_content_hash"], 0.9, '["related"]', "{}", 1.0))
        engine.conn.commit()
        await adapter.close()
        return target_row, survivor_row
    target_row, survivor_row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    plan = _plan(db, target_row, operation="success-op")
    result = _run(plan, tmp_path)
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert result.promoted is True
    assert tuple(PUBLIC_TOOL_NAMES) == ("memory_search", "memory_get", "memory_store", "memory_update", "memory_status")
    conn = sqlite3.connect(db)
    try:
        tombstone = conn.execute("SELECT fact, lifecycle_state, source_provenance, tags, contradiction_key, native_content_hash, payload_fingerprint, idempotency_key FROM adapter_records WHERE record_id = ?", (target_row["record_id"],)).fetchone()
        assert tombstone is not None
        assert tombstone[0] == "[purged-sensitive-memory]"
        assert tombstone[1] == "QUARANTINED"
        assert marker not in json.dumps(tombstone)
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE content_hash = ?", (target_row["native_content_hash"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_graph WHERE source_hash = ? OR target_hash = ?", (target_row["native_content_hash"], target_row["native_content_hash"])).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE content_hash = ?", (survivor_row["native_content_hash"],)).fetchone()[0] == 1
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert marker.encode() not in db.read_bytes()
    for sidecar in (Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        assert not sidecar.exists() or marker.encode() not in sidecar.read_bytes()
    receipt_path = tmp_path / "success-op.receipt.json"
    receipt_text = receipt_path.read_text()
    receipt = json.loads(receipt_text)
    assert marker not in receipt_text
    assert target_row["native_content_hash"] not in receipt_text
    assert target_row["payload_fingerprint"] not in receipt_text
    assert receipt["plaintext_pre_mutation_backup"] is False
    assert receipt["second_wal_checkpoint_truncate"] == "PASS"
    assert receipt["post_sealing_db_fts_verification"] == "PASS"
    assert receipt["raw_content_logged"] is False

# APPEND_CHECK

async def _replace_for_history(adapter: MemoryAdapter, current: dict[str, Any]) -> dict[str, Any]:
    result = await adapter.update(
        memory_id=current["memory_id"], scope="PROJECT", project_id="factlane",
        expected_revision=current["revision"], mode="REPLACE",
        idempotency_key="history-current",
        replacement={"fact": "Current descendant survives recovery.", "memory_type": "PROJECT_LEARNED_FACT", "source_provenance": _provenance("history-current", "2"), "freshness_policy": {"kind": "manual"}, "source_timestamp": "2026-09-29T00:00:00Z", "verified_by": "OWNER", "tags": ["subject:sensitive-recovery-test"]},
    )
    return result["results"][0]


def test_pm0_historical_adapter_only_preserves_current_descendant(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], dict[str, Any]]:
        engine, adapter = await _open_adapter(tmp_path)
        old = await _store(adapter, key="history-old", fact="Historical synthetic target.", marker="1")
        current = await _replace_for_history(adapter, old)
        rows = await engine.get_record(old["memory_id"], ScopeContext("PROJECT", project_id="factlane"), history=True)
        old_row = next(row for row in rows if row["record_id"] == old["record_id"])
        assert await engine.compact_superseded_record(old_row["record_id"]) is True
        rows_after = await engine.get_record(old["memory_id"], ScopeContext("PROJECT", project_id="factlane"), history=True)
        old_after = next(row for row in rows_after if row["record_id"] == old["record_id"])
        current_after = await _target_row(engine, current["memory_id"])
        await adapter.close()
        return old_after, current_after
    old_row, current_row = asyncio.run(prepare())
    plan = _plan(tmp_path / "memory.db", old_row, operation="historical-op", materialization=PM0)
    result = _run(plan, tmp_path)
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    conn = sqlite3.connect(tmp_path / "memory.db")
    try:
        old_state = conn.execute("SELECT fact, lifecycle_state FROM adapter_records WHERE record_id = ?", (old_row["record_id"],)).fetchone()
        assert old_state == ("[purged-sensitive-memory]", "QUARANTINED")
        count = conn.execute("SELECT COUNT(*) FROM memories WHERE content_hash = ?", (current_row["native_content_hash"],)).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_multi_record_target_set_gets_unique_tombstone_fields(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], dict[str, Any]]:
        engine, adapter = await _open_adapter(tmp_path)
        first = await _store(adapter, key="multi-a", fact="Synthetic target A.", marker="3")
        second = await _store(adapter, key="multi-b", fact="Synthetic target B.", marker="4")
        first_row = await _target_row(engine, first["memory_id"])
        second_row = await _target_row(engine, second["memory_id"])
        await adapter.close()
        return first_row, second_row
    first_row, second_row = asyncio.run(prepare())
    targets = tuple(RecoveryTarget(record_id=row["record_id"], memory_id=row["memory_id"], revision=int(row["revision"]), scope=RecoveryScope("PROJECT", project_id="factlane"), expected_lifecycle=row["lifecycle_state"], expected_materialization=PM1, expected_native_hash=row["native_content_hash"]) for row in (first_row, second_row))
    plan = RecoveryPlan(operation_id="multi-op", db_path=str(tmp_path / "memory.db"), profile=profile(), targets=targets, retained_identity_fields_classified_non_sensitive=True)
    _run(plan, tmp_path)
    conn = sqlite3.connect(tmp_path / "memory.db")
    try:
        rows = conn.execute("SELECT contradiction_key, native_content_hash, payload_fingerprint, idempotency_key FROM adapter_records ORDER BY record_id").fetchall()
        assert len(rows) == 2
        for index in range(4):
            assert len({row[index] for row in rows}) == 2
        old_values = {value for source in (first_row, second_row) for value in (source["contradiction_key"], source["native_content_hash"], source["payload_fingerprint"], source["idempotency_key"])}
        assert not any(value in old_values for row in rows for value in row)
    finally:
        conn.close()


def test_postcommit_sealing_failure_keeps_payload_purged_and_reruns(tmp_path: Path) -> None:
    class FailSealOnce(SensitiveMemoryRecoveryOperator):
        def _seal_and_promote(self, conn, plan, survivor_snapshot):
            raise RecoveryHold("S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE", "synthetic sealing failure")
    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="seal-fail", fact="Synthetic sealing target.", marker="5")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row
    row = asyncio.run(prepare())
    plan = _plan(tmp_path / "memory.db", row, operation="seal-op")
    failing = FailSealOnce(foreign_handle_provider=lambda _: [])
    with pytest.raises(RecoveryHold) as caught:
        _run(plan, tmp_path, operator=failing)
    assert caught.value.code == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == "[purged-sensitive-memory]"
    state = json.loads((tmp_path / "seal-op.receipt.json.state.json").read_text())
    assert state["phase"] == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
    result = _run(plan, tmp_path)
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == "[purged-sensitive-memory]"



def test_no_mutation_hold_does_not_persist_fts_secure_delete_config(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], dict[str, Any]]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="pm2-config", fact="Synthetic PM2 config marker.", marker="6")
        row = await _target_row(engine, item["memory_id"])
        assert engine.conn is not None
        before_config = dict(engine.conn.execute("SELECT k, v FROM memory_content_fts_config").fetchall())
        native_id = engine.conn.execute("SELECT id FROM memories WHERE content_hash = ?", (row["native_content_hash"],)).fetchone()[0]
        engine.conn.execute("DELETE FROM memory_embeddings WHERE rowid = ?", (native_id,))
        engine.conn.commit()
        await adapter.close()
        return row, before_config

    row, before_config = asyncio.run(prepare())
    plan = _plan(tmp_path / "memory.db", row, operation="pm2-config-op")
    with pytest.raises(RecoveryHold) as caught:
        _run(plan, tmp_path)
    assert caught.value.code == "HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION"
    conn = sqlite3.connect(tmp_path / "memory.db")
    try:
        after_config = dict(conn.execute("SELECT k, v FROM memory_content_fts_config").fetchall())
    finally:
        conn.close()
    assert after_config == before_config



def test_r01_competing_maintenance_lease_holds_before_mutation(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], str]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="lease-target", fact="Lease target remains unchanged.", marker="7")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row, row["fact"]

    row, before_fact = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    plan = _plan(db, row, operation="lease-op")
    with MaintenanceLease(str(db), "competing-holder"):
        with pytest.raises(RecoveryHold) as caught:
            _run(plan, tmp_path)
    assert caught.value.code == "HOLD_NONQUIESCENT_NO_MUTATION"
    assert _fact(db, row["record_id"]) == before_fact


def test_r09_r11_tombstone_allowlist_preserves_only_safe_identity_and_replaces_sensitive_fields(tmp_path: Path) -> None:
    marker = "R09ALLOWLISTMARKER7429"
    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="allowlist-old-idempotency", fact=f"Sensitive {marker} value.", marker="8")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row

    row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    plan = _plan(db, row, operation="allowlist-op")
    _run(plan, tmp_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        after = conn.execute("SELECT * FROM adapter_records WHERE record_id = ?", (row["record_id"],)).fetchone()
        assert after is not None
        for field in (
            "record_id", "memory_id", "revision", "parent_record_id", "scope", "project_id",
            "worktree_id", "workflow_id", "agent_id", "memory_type", "created_at",
            "embedding_profile_id", "embedding_model_digest", "embedding_output_dimension",
        ):
            assert after[field] == row[field]
        assert after["fact"] == "[purged-sensitive-memory]"
        assert json.loads(after["source_provenance"]) == {
            "extraction_method": "SENSITIVE_MEMORY_PURGE",
            "review_ref": "operator-recovery",
            "source_class": "RECOVERY_TOMBSTONE",
            "source_hash": after["source_provenance"] and json.loads(after["source_provenance"])["source_hash"],
            "source_ref": "recovery:allowlist-op",
        }
        assert after["source_timestamp"] is None
        assert after["last_verified_at"] is None
        assert after["verified_by"] == "UNVERIFIED"
        assert after["authority_role"] == "UNRESOLVED"
        assert json.loads(after["freshness_policy"]) == {"kind": "manual", "recheck_ref": None, "source_fingerprint": None, "ttl_seconds": None}
        assert json.loads(after["supersedes"]) == []
        assert after["contradiction_state"] == "QUARANTINED"
        assert after["confidence"] == 0.0
        assert json.loads(after["tags"]) == []
        assert after["lifecycle_state"] == "QUARANTINED"
        for field in ("contradiction_key", "native_content_hash", "payload_fingerprint", "idempotency_key"):
            assert after[field] != row[field]
        serialized = json.dumps(dict(after), sort_keys=True)
        assert marker not in serialized
        for field in ("contradiction_key", "native_content_hash", "payload_fingerprint", "idempotency_key"):
            assert str(row[field]) not in serialized
    finally:
        conn.close()


def test_r12_m1_uses_scrub_purge_path_instead_of_quarantine_only_hiding(tmp_path: Path) -> None:
    marker = "R12M1MARKER7429"
    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="m1-target", fact=f"Mis-store {marker}.", marker="9")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row

    row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    plan = _plan(db, row, operation="m1-op", incident_class="M1")
    result = _run(plan, tmp_path)
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT fact FROM adapter_records WHERE record_id = ?", (row["record_id"],)).fetchone()[0] == "[purged-sensitive-memory]"
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE content_hash = ?", (row["native_content_hash"],)).fetchone()[0] == 0
    finally:
        conn.close()
    assert marker.encode() not in db.read_bytes()


def test_r13_review_history_returns_only_safe_tombstone_after_purge(tmp_path: Path) -> None:
    marker = "R13HISTORYMARKER7429"
    async def prepare_and_recover() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="review-history-target", fact=f"History {marker}.", marker="a")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        operator = SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _: [])
        await operator.execute(
            _plan(tmp_path / "memory.db", row, operation="review-history-op"),
            receipt_path=str(tmp_path / "review-history-op.receipt.json"),
        )
        reopened = SQLiteVecEngine(str(tmp_path / "memory.db"), profile())
        await reopened.open()
        reviewer = MemoryAdapter(reopened, FixedProvider(profile()))  # type: ignore[arg-type]
        try:
            result = await reviewer.get(memory_id=row["memory_id"], scope="PROJECT", project_id="factlane", retrieval_mode="REVIEW_HISTORY")
            return result
        finally:
            await reviewer.close()

    result = asyncio.run(prepare_and_recover())
    payload = json.dumps(result, sort_keys=True)
    assert marker not in payload
    assert len(result["results"]) == 1
    assert result["results"][0]["fact"] == "[purged-sensitive-memory]"
    assert result["results"][0]["lifecycle_state"] == "QUARANTINED"


def test_r14_exact_multi_record_plan_purges_every_explicit_target(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], dict[str, Any]]:
        engine, adapter = await _open_adapter(tmp_path)
        first = await _store(adapter, key="prop-a", fact="Propagation target A.", marker="b")
        second = await _store(adapter, key="prop-b", fact="Propagation target B.", marker="c")
        rows = (await _target_row(engine, first["memory_id"]), await _target_row(engine, second["memory_id"]))
        await adapter.close()
        return rows

    first_row, second_row = asyncio.run(prepare())
    targets = tuple(
        RecoveryTarget(
            record_id=row["record_id"], memory_id=row["memory_id"], revision=int(row["revision"]),
            scope=RecoveryScope("PROJECT", project_id="factlane"), expected_lifecycle=row["lifecycle_state"],
            expected_materialization=PM1, expected_native_hash=row["native_content_hash"],
        )
        for row in (first_row, second_row)
    )
    db = tmp_path / "memory.db"
    _run(RecoveryPlan(operation_id="propagation-op", db_path=str(db), profile=profile(), targets=targets, retained_identity_fields_classified_non_sensitive=True), tmp_path)
    conn = sqlite3.connect(db)
    try:
        for row in (first_row, second_row):
            assert conn.execute("SELECT fact FROM adapter_records WHERE record_id = ?", (row["record_id"],)).fetchone()[0] == "[purged-sensitive-memory]"
            assert conn.execute("SELECT COUNT(*) FROM memories WHERE content_hash = ?", (row["native_content_hash"],)).fetchone()[0] == 0
    finally:
        conn.close()


def test_r15_discovered_additional_contaminated_record_forces_plan_refreeze(tmp_path: Path) -> None:
    target = RecoveryTarget(
        record_id="r15-a", memory_id="m15-a", revision=0,
        scope=RecoveryScope("PROJECT", project_id="factlane"), expected_lifecycle="VALIDATED_CURRENT",
        expected_materialization=PM1, expected_native_hash="d" * 64,
    )
    with pytest.raises(ValueError, match="refreeze"):
        RecoveryPlan(
            operation_id="r15-refreeze", db_path=str(tmp_path / "memory.db"), profile=profile(),
            targets=(target,), propagation_state="ADDITIONAL_CONTAMINATED_RECORD_DISCOVERED",
        )


def test_r17_current_native_delete_trigger_leaves_stale_fts_match_negative_control(tmp_path: Path) -> None:
    token = "r17staleftsmarker7429"
    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="r17-negative", fact=f"{token} disposable negative control", marker="e")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row

    row = asyncio.run(prepare())
    conn = sqlite3.connect(tmp_path / "memory.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_content_fts WHERE memory_content_fts MATCH ?", (token,)).fetchone()[0] == 1
        conn.execute("DELETE FROM memories WHERE content_hash = ?", (row["native_content_hash"],))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE content_hash = ?", (row["native_content_hash"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_content_fts WHERE memory_content_fts MATCH ?", (token,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_r18_operator_rebuild_removes_stale_fts_match(tmp_path: Path) -> None:
    token = "r18rebuildftsmarker7429"
    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="r18-target", fact=f"{token} disposable target", marker="f")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row

    row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    _run(_plan(db, row, operation="r18-op"), tmp_path)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_content_fts WHERE memory_content_fts MATCH ?", (token,)).fetchone()[0] == 0
        conn.execute("INSERT INTO memory_content_fts(memory_content_fts, rank) VALUES('integrity-check', 1)")
    finally:
        conn.close()


def test_r19_late_precommit_failure_rolls_back_entire_logical_operation(tmp_path: Path) -> None:
    class FailBeforeCommit(SensitiveMemoryRecoveryOperator):
        @staticmethod
        def _verify_logical_absence(conn, inspections):
            SensitiveMemoryRecoveryOperator._verify_logical_absence(conn, inspections)
            raise RecoveryHold("SYNTHETIC_PRECOMMIT_FAILURE", "synthetic late precommit failure")

    async def prepare() -> tuple[dict[str, Any], str]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="r19-target", fact="R19 original payload.", marker="1")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row, row["fact"]

    row, before_fact = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    operator = FailBeforeCommit(foreign_handle_provider=lambda _: [])
    with pytest.raises(RecoveryHold) as caught:
        _run(_plan(db, row, operation="r19-op"), tmp_path, operator=operator)
    assert caught.value.code == "SYNTHETIC_PRECOMMIT_FAILURE"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT fact FROM adapter_records WHERE record_id = ?", (row["record_id"],)).fetchone()[0] == before_fact
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE content_hash = ?", (row["native_content_hash"],)).fetchone()[0] == 1
        assert "secure-delete" not in dict(conn.execute("SELECT k, v FROM memory_content_fts_config").fetchall())
    finally:
        conn.close()


def test_r23_r24_receipt_has_no_old_confirmation_oracle_and_no_plaintext_backup(tmp_path: Path) -> None:
    marker = "R23RECEIPTMARKER7429"
    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="r23-old-idempotency", fact=f"Receipt {marker}.", marker="2")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row

    row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    _run(_plan(db, row, operation="r23-op"), tmp_path)
    receipt_path = tmp_path / "r23-op.receipt.json"
    receipt_text = receipt_path.read_text()
    receipt = json.loads(receipt_text)
    assert receipt["plaintext_pre_mutation_backup"] is False
    assert receipt["content_derived_confirmation_oracle"] is False
    assert receipt["raw_content_logged"] is False
    for forbidden in (
        marker, row["native_content_hash"], row["payload_fingerprint"], row["contradiction_key"], row["idempotency_key"],
    ):
        assert str(forbidden) not in receipt_text
    assert not list(tmp_path.glob("*.backup"))
    assert not list(tmp_path.glob("*.bak"))



def test_r09_unclassified_retained_identity_fields_refuse_surgical_plan(tmp_path: Path) -> None:
    target = RecoveryTarget(
        record_id="r09-unclassified", memory_id="m09-unclassified", revision=0,
        scope=RecoveryScope("PROJECT", project_id="factlane"), expected_lifecycle="VALIDATED_CURRENT",
        expected_materialization=PM1, expected_native_hash="9" * 64,
    )
    with pytest.raises(ValueError, match="independently classified non-sensitive"):
        RecoveryPlan(
            operation_id="r09-unclassified", db_path=str(tmp_path / "memory.db"), profile=profile(),
            targets=(target,), retained_identity_fields_classified_non_sensitive=False,
        )



def test_r03_pm0_requires_historical_lifecycle_proof(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], str]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="pm0-current", fact="Current row cannot claim PM0.", marker="3")
        row = await _target_row(engine, item["memory_id"])
        assert engine.conn is not None
        native_id = engine.conn.execute("SELECT id FROM memories WHERE content_hash = ?", (row["native_content_hash"],)).fetchone()[0]
        engine.conn.execute("DELETE FROM memory_embeddings WHERE rowid = ?", (native_id,))
        engine.conn.execute("DELETE FROM memory_graph WHERE source_hash = ? OR target_hash = ?", (row["native_content_hash"], row["native_content_hash"]))
        engine.conn.execute("DELETE FROM memories WHERE id = ?", (native_id,))
        engine.conn.commit()
        await adapter.close()
        return row, row["fact"]

    row, before_fact = asyncio.run(prepare())
    plan = _plan(tmp_path / "memory.db", row, operation="pm0-current-op", materialization=PM0)
    with pytest.raises(RecoveryHold) as caught:
        _run(plan, tmp_path)
    assert caught.value.code == "HOLD_PARTIAL_MATERIALIZATION_NO_MUTATION"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == before_fact



def test_r21_resume_rejects_drifted_recovery_tombstone(tmp_path: Path) -> None:
    class FailSealOnce(SensitiveMemoryRecoveryOperator):
        def _seal_and_promote(self, conn, plan, survivor_snapshot):
            raise RecoveryHold("S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE", "synthetic sealing failure")

    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="r21-drift", fact="R21 payload must never return.", marker="4")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row

    row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    plan = _plan(db, row, operation="r21-drift-op")
    with pytest.raises(RecoveryHold):
        _run(plan, tmp_path, operator=FailSealOnce(foreign_handle_provider=lambda _: []))
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE adapter_records SET tags = '[\"drifted\"]' WHERE record_id = ?", (row["record_id"],))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RecoveryHold) as caught:
        _run(plan, tmp_path)
    assert caught.value.code == "HOLD_TARGET_DRIFT_NO_MUTATION"
    assert _fact(db, row["record_id"]) == "[purged-sensitive-memory]"


def test_r22_sealing_detects_unrelated_survivor_drift_before_promotion(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], dict[str, Any]]:
        engine, adapter = await _open_adapter(tmp_path)
        target = await _store(adapter, key="r22-target", fact="R22 target payload.", marker="5")
        survivor = await _store(adapter, key="r22-survivor", fact="R22 survivor remains exact.", marker="6")
        target_row = await _target_row(engine, target["memory_id"])
        survivor_row = await _target_row(engine, survivor["memory_id"])
        await adapter.close()
        return target_row, survivor_row

    target_row, survivor_row = asyncio.run(prepare())

    class CorruptSanitizedImage(SensitiveMemoryRecoveryOperator):
        def _verify_sanitized_image(self, image, plan, survivor_snapshot):
            corrupt = sqlite3.connect(image)
            try:
                corrupt.execute("UPDATE adapter_records SET fact = 'CORRUPTED_SURVIVOR' WHERE record_id = ?", (survivor_row["record_id"],))
                corrupt.commit()
            finally:
                corrupt.close()
            return super()._verify_sanitized_image(image, plan, survivor_snapshot)

    db = tmp_path / "memory.db"
    plan = _plan(db, target_row, operation="r22-survivor-op")
    with pytest.raises(RecoveryHold) as caught:
        _run(plan, tmp_path, operator=CorruptSanitizedImage(foreign_handle_provider=lambda _: []))
    assert caught.value.code == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT fact FROM adapter_records WHERE record_id = ?", (survivor_row["record_id"],)).fetchone()[0] == survivor_row["fact"]
    finally:
        conn.close()



def test_r22_survivor_graph_snapshot_is_vacuum_stable_with_rowid_gap(tmp_path: Path) -> None:
    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        target = await _store(adapter, key="r22-gap-target", fact="R22 gap target.", marker="7")
        survivor_a = await _store(adapter, key="r22-gap-a", fact="R22 gap survivor A.", marker="8")
        survivor_b = await _store(adapter, key="r22-gap-b", fact="R22 gap survivor B.", marker="9")
        target_row = await _target_row(engine, target["memory_id"])
        a_row = await _target_row(engine, survivor_a["memory_id"])
        b_row = await _target_row(engine, survivor_b["memory_id"])
        assert engine.conn is not None
        engine.conn.execute(
            "INSERT INTO memory_graph(source_hash, target_hash, similarity, connection_types, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (target_row["native_content_hash"], a_row["native_content_hash"], 0.2, '["temporary"]', '{}', 1.0),
        )
        engine.conn.execute(
            "INSERT INTO memory_graph(source_hash, target_hash, similarity, connection_types, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (a_row["native_content_hash"], b_row["native_content_hash"], 0.8, '["survivor"]', '{}', 2.0),
        )
        engine.conn.commit()
        survivor_graph = engine.conn.execute(
            "SELECT rowid, source_hash, target_hash FROM memory_graph WHERE source_hash = ? AND target_hash = ?",
            (a_row["native_content_hash"], b_row["native_content_hash"]),
        ).fetchone()
        assert survivor_graph is not None and int(survivor_graph[0]) >= 2
        await adapter.close()
        return target_row

    target_row = asyncio.run(prepare())
    result = _run(_plan(tmp_path / "memory.db", target_row, operation="r22-gap-op"), tmp_path)
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"



def test_r03_pm0_historical_plan_does_not_require_expected_native_hash(tmp_path: Path) -> None:
    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        old = await _store(adapter, key="pm0-no-hash-old", fact="Historical PM0 hashless target.", marker="a")
        await _replace_for_history(adapter, old)
        rows = await engine.get_record(old["memory_id"], ScopeContext("PROJECT", project_id="factlane"), history=True)
        old_row = next(row for row in rows if row["record_id"] == old["record_id"])
        assert await engine.compact_superseded_record(old_row["record_id"]) is True
        rows_after = await engine.get_record(old["memory_id"], ScopeContext("PROJECT", project_id="factlane"), history=True)
        historical = next(row for row in rows_after if row["record_id"] == old["record_id"])
        await adapter.close()
        return historical

    row = asyncio.run(prepare())
    target = RecoveryTarget(
        record_id=row["record_id"], memory_id=row["memory_id"], revision=int(row["revision"]),
        scope=RecoveryScope("PROJECT", project_id="factlane"), expected_lifecycle="HISTORICAL",
        expected_materialization=PM0, expected_native_hash=None,
    )
    plan = RecoveryPlan(
        operation_id="pm0-no-hash-op", db_path=str(tmp_path / "memory.db"), profile=profile(),
        targets=(target,), retained_identity_fields_classified_non_sensitive=True,
    )
    result = _run(plan, tmp_path)
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == "[purged-sensitive-memory]"


def test_r02_quiescence_inventory_error_fails_closed(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], str]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="r02-inventory-error", fact="Inventory error target remains unchanged.", marker="b")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row, row["fact"]

    row, before_fact = asyncio.run(prepare())
    def unavailable(_: str):
        raise PermissionError("synthetic proc inventory denial")
    operator = SensitiveMemoryRecoveryOperator(foreign_handle_provider=unavailable)
    with pytest.raises(RecoveryHold) as caught:
        _run(_plan(tmp_path / "memory.db", row, operation="r02-inventory-error-op"), tmp_path, operator=operator)
    assert caught.value.code == "HOLD_NONQUIESCENT_NO_MUTATION"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == before_fact



def test_r03_pm0_hashless_cleanup_removes_attributable_graph_and_superseded_residue(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], dict[str, Any]]:
        engine, adapter = await _open_adapter(tmp_path)
        old = await _store(adapter, key="pm0-residue-old", fact="Historical PM0 residue target.", marker="c")
        current = await _replace_for_history(adapter, old)
        rows = await engine.get_record(old["memory_id"], ScopeContext("PROJECT", project_id="factlane"), history=True)
        old_row = next(row for row in rows if row["record_id"] == old["record_id"])
        assert await engine.compact_superseded_record(old_row["record_id"]) is True
        historical = next(
            row for row in await engine.get_record(old["memory_id"], ScopeContext("PROJECT", project_id="factlane"), history=True)
            if row["record_id"] == old["record_id"]
        )
        current_row = await _target_row(engine, current["memory_id"])
        assert engine.conn is not None
        engine.conn.execute(
            "INSERT INTO memory_graph(source_hash, target_hash, similarity, connection_types, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (historical["native_content_hash"], current_row["native_content_hash"], 0.4, '["stale"]', '{}', 1.0),
        )
        engine.conn.execute(
            "UPDATE memories SET superseded_by = ? WHERE content_hash = ?",
            (historical["native_content_hash"], current_row["native_content_hash"]),
        )
        engine.conn.commit()
        await adapter.close()
        return historical, current_row

    historical, current_row = asyncio.run(prepare())
    target = RecoveryTarget(
        record_id=historical["record_id"], memory_id=historical["memory_id"], revision=int(historical["revision"]),
        scope=RecoveryScope("PROJECT", project_id="factlane"), expected_lifecycle="HISTORICAL",
        expected_materialization=PM0, expected_native_hash=None,
    )
    plan = RecoveryPlan(
        operation_id="pm0-residue-op", db_path=str(tmp_path / "memory.db"), profile=profile(),
        targets=(target,), retained_identity_fields_classified_non_sensitive=True,
    )
    result = _run(plan, tmp_path)
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    conn = sqlite3.connect(tmp_path / "memory.db")
    try:
        old_hash = historical["native_content_hash"]
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_graph WHERE source_hash = ? OR target_hash = ?", (old_hash, old_hash)
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE superseded_by = ?", (old_hash,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE content_hash = ?", (current_row["native_content_hash"],)).fetchone()[0] == 1
    finally:
        conn.close()


def test_r20_sealing_state_is_monotonic_when_resume_is_blocked(tmp_path: Path) -> None:
    class FailSealOnce(SensitiveMemoryRecoveryOperator):
        def _seal_and_promote(self, conn, plan, survivor_snapshot):
            raise RecoveryHold("S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE", "synthetic sealing failure")

    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="r20-monotonic", fact="R20 committed payload never returns.", marker="d")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row

    row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    plan = _plan(db, row, operation="r20-monotonic-op")
    receipt = tmp_path / "r20-monotonic-op.receipt.json"
    failing = FailSealOnce(foreign_handle_provider=lambda _: [])
    with pytest.raises(RecoveryHold):
        asyncio.run(failing.execute(plan, receipt_path=str(receipt)))
    state_path = Path(str(receipt) + ".state.json")
    first_state = json.loads(state_path.read_text())
    assert first_state["phase"] == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
    assert first_state["plan_binding"]

    blocked = SensitiveMemoryRecoveryOperator(
        foreign_handle_provider=lambda _: [{"pid": 4242, "fd": "7", "path": str(db)}]
    )
    with pytest.raises(RecoveryHold) as caught:
        asyncio.run(blocked.execute(plan, receipt_path=str(receipt)))
    assert caught.value.code == "HOLD_NONQUIESCENT_NO_MUTATION"
    assert json.loads(state_path.read_text()) == first_state
    assert _fact(db, row["record_id"]) == "[purged-sensitive-memory]"


def test_r21_resume_requires_matching_safe_persisted_recovery_metadata(tmp_path: Path) -> None:
    class FailSealOnce(SensitiveMemoryRecoveryOperator):
        def _seal_and_promote(self, conn, plan, survivor_snapshot):
            raise RecoveryHold("S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE", "synthetic sealing failure")

    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="r21-state-binding", fact="R21 state-bound payload.", marker="e")
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row

    row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    plan = _plan(db, row, operation="r21-state-binding-op")
    receipt = tmp_path / "r21-state-binding-op.receipt.json"
    with pytest.raises(RecoveryHold):
        asyncio.run(FailSealOnce(foreign_handle_provider=lambda _: []).execute(plan, receipt_path=str(receipt)))
    state_path = Path(str(receipt) + ".state.json")
    state = json.loads(state_path.read_text())
    state["plan_binding"] = "0" * 64
    state_path.write_text(json.dumps(state, sort_keys=True) + "\n")

    with pytest.raises(RecoveryHold) as caught:
        asyncio.run(SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _: []).execute(plan, receipt_path=str(receipt)))
    assert caught.value.code == "HOLD_RECOVERY_STATE_MISMATCH"
    assert _fact(db, row["record_id"]) == "[purged-sensitive-memory]"


@pytest.mark.parametrize("derived_from", ["{\"unexpected\":\"shape\"}", "[{\"hash\":\"not-a-content-hash\"}]"])
def test_r08_malformed_belief_lineage_is_unexpected_derived_state(tmp_path: Path, derived_from: str) -> None:
    async def prepare() -> tuple[dict[str, Any], str]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="r08-belief-shape", fact="R08 malformed belief target.", marker="f")
        row = await _target_row(engine, item["memory_id"])
        assert engine.conn is not None
        engine.conn.execute(
            "INSERT INTO beliefs(belief_hash, content, confidence, status, created_at, updated_at, derived_from, contradicted_by, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '{}')",
            ("belief-shape", "derived synthetic statement", 0.5, "candidate", "2026-09-29", "2026-09-29", derived_from),
        )
        engine.conn.commit()
        await adapter.close()
        return row, row["fact"]

    row, before_fact = asyncio.run(prepare())
    with pytest.raises(RecoveryHold) as caught:
        _run(_plan(tmp_path / "memory.db", row, operation="r08-belief-shape-op"), tmp_path)
    assert caught.value.code == "HOLD_UNEXPECTED_DERIVED_STATE_NO_MUTATION"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == before_fact


def test_r08_lineage_fork_is_unexpected_derived_state(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], str]:
        engine, adapter = await _open_adapter(tmp_path)
        root = await _store(adapter, key="r08-fork-root", fact="R08 fork root.", marker="1")
        row = await _target_row(engine, root["memory_id"])
        assert engine.conn is not None
        source = engine.conn.execute("SELECT * FROM adapter_records WHERE record_id = ?", (row["record_id"],)).fetchone()
        columns = [value[1] for value in engine.conn.execute("PRAGMA table_info(adapter_records)").fetchall()]
        base = dict(zip(columns, source, strict=True))
        for index in (1, 2):
            child = dict(base)
            child["record_id"] = f"r08-fork-child-{index}"
            child["memory_id"] = f"r08-fork-memory-{index}"
            child["revision"] = 1
            child["parent_record_id"] = row["record_id"]
            child["fact"] = f"fork child {index}"
            child["native_content_hash"] = f"{index}" * 64
            child["payload_fingerprint"] = f"{index + 2}" * 64
            child["idempotency_key"] = f"r08-fork-child-{index}"
            engine.conn.execute(
                f"INSERT INTO adapter_records ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(child[column] for column in columns),
            )
        engine.conn.commit()
        await adapter.close()
        return row, row["fact"]

    row, before_fact = asyncio.run(prepare())
    with pytest.raises(RecoveryHold) as caught:
        _run(_plan(tmp_path / "memory.db", row, operation="r08-fork-op"), tmp_path)
    assert caught.value.code == "HOLD_UNEXPECTED_DERIVED_STATE_NO_MUTATION"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == before_fact


def test_r08_unknown_derived_table_fails_closed(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], str]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(adapter, key="r08-unknown-derived", fact="R08 unknown derived target.", marker="2")
        row = await _target_row(engine, item["memory_id"])
        assert engine.conn is not None
        engine.conn.execute("CREATE TABLE unexpected_recovery_derived_state(target_hash TEXT)")
        engine.conn.execute("INSERT INTO unexpected_recovery_derived_state(target_hash) VALUES (?)", (row["native_content_hash"],))
        engine.conn.commit()
        await adapter.close()
        return row, row["fact"]

    row, before_fact = asyncio.run(prepare())
    with pytest.raises(RecoveryHold) as caught:
        _run(_plan(tmp_path / "memory.db", row, operation="r08-unknown-derived-op"), tmp_path)
    assert caught.value.code == "HOLD_UNEXPECTED_DERIVED_STATE_NO_MUTATION"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == before_fact



def test_r14_multi_target_shared_graph_edge_counts_once(tmp_path: Path) -> None:
    async def prepare() -> tuple[dict[str, Any], dict[str, Any]]:
        engine, adapter = await _open_adapter(tmp_path)
        first = await _store(adapter, key="shared-graph-a", fact="Shared graph target A.", marker="a")
        second = await _store(adapter, key="shared-graph-b", fact="Shared graph target B.", marker="b")
        first_row = await _target_row(engine, first["memory_id"])
        second_row = await _target_row(engine, second["memory_id"])
        assert engine.conn is not None
        engine.conn.execute(
            "INSERT INTO memory_graph(source_hash, target_hash, similarity, connection_types, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (first_row["native_content_hash"], second_row["native_content_hash"], 0.9, '["contaminated-pair"]', '{}', 1.0),
        )
        engine.conn.commit()
        await adapter.close()
        return first_row, second_row

    first_row, second_row = asyncio.run(prepare())
    targets = tuple(
        RecoveryTarget(
            record_id=row["record_id"], memory_id=row["memory_id"], revision=int(row["revision"]),
            scope=RecoveryScope("PROJECT", project_id="factlane"), expected_lifecycle=row["lifecycle_state"],
            expected_materialization=PM1, expected_native_hash=row["native_content_hash"],
        )
        for row in (first_row, second_row)
    )
    plan = RecoveryPlan(
        operation_id="shared-graph-op", db_path=str(tmp_path / "memory.db"), profile=profile(),
        targets=targets, retained_identity_fields_classified_non_sensitive=True,
    )
    result = _run(plan, tmp_path)
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    conn = sqlite3.connect(tmp_path / "memory.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_graph").fetchone()[0] == 0
    finally:
        conn.close()
