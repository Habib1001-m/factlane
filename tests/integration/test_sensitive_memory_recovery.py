from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter, trusted_write_context_for_profile
from factlane.contract import AdapterError, PUBLIC_TOOL_NAMES, ScopeContext
from factlane.embeddings import EmbeddingProfile
from factlane.recovery import MaintenanceLease, PM0, PM1, RecoveryHold, RecoveryPlan, RecoveryScope, RecoveryTarget, SensitiveMemoryRecoveryOperator
from factlane.storage import SQLiteVecEngine, register_storage_v2_writer


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
    return engine, MemoryAdapter(
        engine,
        FixedProvider(p),  # type: ignore[arg-type]
        trusted_write_context=trusted_write_context_for_profile(
            "owner-current",
            contributor_ref="sensitive-recovery-tests",
        ),
    )


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


def test_recovery_rejects_unsupported_sqlite_before_lock_or_state(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "memory.db"
    receipt = tmp_path / "runtime-floor.receipt.json"
    state = receipt.with_suffix(receipt.suffix + ".state.json")
    obsolete_sidecar = Path(str(db) + ".recovery.lock")

    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        item = await _store(
            adapter,
            key="runtime-floor",
            fact="Runtime floor recovery target remains unchanged.",
            marker="f",
        )
        row = await _target_row(engine, item["memory_id"])
        await adapter.close()
        return row

    row = asyncio.run(prepare())
    before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    plan = _plan(db, row, operation="runtime-floor-op")
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 37, 2))

    with pytest.raises(RecoveryHold) as exc_info:
        asyncio.run(
            SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _: []).execute(
                plan,
                receipt_path=str(receipt),
            )
        )

    assert exc_info.value.code == "HOLD_SQLITE_RUNTIME_UNSUPPORTED_NO_MUTATION"
    assert db.exists()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_hash
    assert not obsolete_sidecar.exists()
    assert not receipt.exists()
    assert not state.exists()


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
        def _seal_and_promote(self, conn, plan, survivor_snapshot, maintenance_lease):
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

    async def normal_runtime_is_blocked() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "memory.db"), profile())
        with pytest.raises(AdapterError) as blocked:
            await engine.open()
        assert blocked.value.code == "MAINTENANCE_IN_PROGRESS"

    asyncio.run(normal_runtime_is_blocked())

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio
import sys

from factlane.contract import AdapterError
from factlane.embeddings import EmbeddingProfile
from factlane.storage import SQLiteVecEngine

p = EmbeddingProfile(
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
e = SQLiteVecEngine(sys.argv[1], p)

async def run():
    try:
        await e.open()
    except AdapterError as exc:
        print(exc.code)
        return
    raise SystemExit("runtime unexpectedly opened")

asyncio.run(run())
""",
            str(tmp_path / "memory.db"),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "MAINTENANCE_IN_PROGRESS"

    result = _run(plan, tmp_path)
    assert result.state == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert _fact(tmp_path / "memory.db", row["record_id"]) == "[purged-sensitive-memory]"

    async def normal_runtime_reopens_after_recovery() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "memory.db"), profile())
        await engine.open()
        await engine.close()

    asyncio.run(normal_runtime_reopens_after_recovery())



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


def test_r27_ordinary_writer_cannot_disappear_across_final_recovery_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer attaching after the final quiescence check must not succeed on the old inode."""

    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        target = await _store(
            adapter,
            key="r27-target",
            fact="R27 recovery target payload.",
            marker="8",
        )
        row = await _target_row(engine, target["memory_id"])
        await adapter.close()
        return row

    target_row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    plan = _plan(db, target_row, operation="r27-seal-window-op")

    writer_ready = threading.Event()
    promotion_complete = threading.Event()
    writer_done = threading.Event()
    writer_outcome: dict[str, Any] = {}

    def writer_thread() -> None:
        async def run_writer() -> None:
            adapter: MemoryAdapter | None = None
            try:
                _, adapter = await _open_adapter(tmp_path)
                writer_ready.set()
                if not promotion_complete.wait(timeout=10):
                    raise AssertionError("promotion did not complete")
                item = await _store(
                    adapter,
                    key="r27-late-writer",
                    fact="R27 late writer must not disappear.",
                    marker="9",
                )
                writer_outcome.update(kind="success", record_id=item["record_id"])
            except AdapterError as exc:
                writer_outcome.update(kind="blocked", code=exc.code)
                writer_ready.set()
            except BaseException as exc:  # surfaced in the parent thread below
                writer_outcome.update(kind="unexpected", error=repr(exc))
                writer_ready.set()
            finally:
                if adapter is not None:
                    await adapter.close()
                writer_done.set()

        asyncio.run(run_writer())

    import factlane.recovery as recovery_module

    original_replace = recovery_module.os.replace
    writer: threading.Thread | None = None

    def race_replace(src: str | Path, dst: str | Path) -> None:
        nonlocal writer
        if str(dst) == str(db) and str(src).endswith(".sanitized"):
            writer = threading.Thread(target=writer_thread, daemon=True)
            writer.start()
            assert writer_ready.wait(timeout=10), "ordinary writer did not attach in seal window"
            original_replace(src, dst)
            promotion_complete.set()
            assert writer_done.wait(timeout=10), "ordinary writer did not finish after promotion"
            writer.join(timeout=1)
            return
        original_replace(src, dst)

    monkeypatch.setattr(recovery_module.os, "replace", race_replace)

    result = _run(plan, tmp_path)
    assert result.promoted is True
    assert writer_outcome == {
        "kind": "blocked",
        "code": "MAINTENANCE_IN_PROGRESS",
    }


def test_r28_live_runtime_engine_shared_lease_blocks_recovery_even_if_handle_inventory_misses_it(
    tmp_path: Path,
) -> None:
    async def prepare() -> tuple[SQLiteVecEngine, MemoryAdapter, dict[str, Any]]:
        engine, adapter = await _open_adapter(tmp_path)
        target = await _store(
            adapter,
            key="r28-target",
            fact="R28 target remains untouched while runtime engine is live.",
            marker="a",
        )
        row = await _target_row(engine, target["memory_id"])
        return engine, adapter, row

    engine, adapter, target_row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    plan = _plan(db, target_row, operation="r28-live-runtime-op")
    operator = SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _: [])
    try:
        with pytest.raises(RecoveryHold) as caught:
            _run(plan, tmp_path, operator=operator)
        assert caught.value.code == "HOLD_NONQUIESCENT_NO_MUTATION"
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before_hash
        assert _fact(db, target_row["record_id"]) == target_row["fact"]
    finally:
        asyncio.run(adapter.close())
        assert engine.conn is None


def test_r29_multiple_runtime_engines_share_maintenance_lease_and_release_cleanly(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first_engine, first_adapter = await _open_adapter(tmp_path)
        second_engine, second_adapter = await _open_adapter(tmp_path)
        db = tmp_path / "memory.db"
        try:
            assert first_engine.conn is not None
            assert second_engine.conn is not None
            await _store(
                first_adapter,
                key="r29-first",
                fact="R29 first runtime writer remains normal.",
                marker="b",
            )
            await _store(
                second_adapter,
                key="r29-second",
                fact="R29 second runtime writer remains normal.",
                marker="c",
            )
            with pytest.raises(RecoveryHold) as both_open:
                with MaintenanceLease(str(db), "r29-both-open"):
                    pass
            assert both_open.value.code == "HOLD_NONQUIESCENT_NO_MUTATION"

            await second_adapter.close()
            with pytest.raises(RecoveryHold) as one_open:
                with MaintenanceLease(str(db), "r29-one-open"):
                    pass
            assert one_open.value.code == "HOLD_NONQUIESCENT_NO_MUTATION"
        finally:
            if second_engine.conn is not None:
                await second_adapter.close()
            await first_adapter.close()

    asyncio.run(exercise())
    db = tmp_path / "memory.db"
    with MaintenanceLease(str(db), "r29-after-close"):
        pass


def test_r30_symlink_alias_uses_same_maintenance_lock_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    alias_dir = tmp_path / "alias"
    alias_dir.symlink_to(real_dir, target_is_directory=True)

    async def prepare() -> None:
        _, adapter = await _open_adapter(real_dir)
        await adapter.close()

    asyncio.run(prepare())
    real_db = real_dir / "memory.db"
    alias_engine = SQLiteVecEngine(str(alias_dir / "memory.db"), profile())

    import factlane.storage as storage_module

    def backend_must_not_load() -> Any:
        raise AssertionError("backend initialization must not start while recovery owns EX")

    monkeypatch.setattr(storage_module, "load_pinned_sqlite_vec_storage", backend_must_not_load)
    with MaintenanceLease(str(real_db), "r30-realpath-holder"):
        with pytest.raises(AdapterError) as caught:
            asyncio.run(alias_engine.open())
    assert caught.value.code == "MAINTENANCE_IN_PROGRESS"


def test_r31_backend_close_failure_retains_shared_lease_until_close_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def prepare() -> SQLiteVecEngine:
        engine, _ = await _open_adapter(tmp_path)
        return engine

    engine = asyncio.run(prepare())
    assert engine.storage is not None
    original_close = engine.storage.close

    async def fail_close() -> None:
        raise RuntimeError("synthetic backend close failure")

    monkeypatch.setattr(engine.storage, "close", fail_close)
    with pytest.raises(AdapterError) as caught:
        asyncio.run(engine.close())
    assert caught.value.code == "BACKEND_UNAVAILABLE"

    db = tmp_path / "memory.db"
    with pytest.raises(RecoveryHold) as blocked:
        with MaintenanceLease(str(db), "r31-must-remain-blocked"):
            pass
    assert blocked.value.code == "HOLD_NONQUIESCENT_NO_MUTATION"

    monkeypatch.setattr(engine.storage, "close", original_close)
    asyncio.run(engine.close())
    with MaintenanceLease(str(db), "r31-after-clean-close"):
        pass


def test_r32_cancelled_runtime_open_releases_shared_lease_after_backend_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import factlane.storage as storage_module

    instances: list[Any] = []

    class CancelDuringInitialize:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            self.closed = False
            self.conn = None
            instances.append(self)

        async def initialize(self, strict_dimension_check: bool = True) -> None:
            del strict_dimension_check
            raise asyncio.CancelledError

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(storage_module, "load_pinned_sqlite_vec_storage", lambda: CancelDuringInitialize)
    monkeypatch.setattr(
        storage_module,
        "bind_deferred_embedding_initializer",
        lambda storage, initializer: None,
    )
    engine = SQLiteVecEngine(str(tmp_path / "memory.db"), profile())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(engine.open())
    assert instances and instances[0].closed is True

    with MaintenanceLease(str(tmp_path / "memory.db"), "r32-after-cancel"):
        pass


def test_r33_nested_parent_first_create_keeps_runtime_lease_contract(tmp_path: Path) -> None:
    nested_db = tmp_path / "nested" / "deep" / "memory.db"

    async def exercise() -> None:
        engine = SQLiteVecEngine(str(nested_db), profile())
        await engine.open()
        try:
            assert engine.conn is not None
            assert nested_db.exists()
        finally:
            await engine.close()

    asyncio.run(exercise())
    assert nested_db.exists()
    assert not Path(str(nested_db.resolve()) + ".recovery.lock").exists()
    with MaintenanceLease(str(nested_db), "r33-after-close"):
        pass


def test_r34_maintenance_postflight_capability_expires_and_is_db_bound(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    other_db = tmp_path / "other.db"
    sqlite3.connect(db).close()
    sqlite3.connect(other_db).close()
    capability = None
    with MaintenanceLease(str(db), "r34-capability") as lease:
        capability = lease.capability
        assert capability is not None
        with pytest.raises(AdapterError) as wrong_db:
            SQLiteVecEngine._for_maintenance(str(other_db), profile(), capability)
        assert wrong_db.value.code == "WRITE_AUTHORIZATION_DENIED"

    assert capability is not None
    with pytest.raises(AdapterError) as expired:
        SQLiteVecEngine._for_maintenance(str(db), profile(), capability)
    assert expired.value.code == "WRITE_AUTHORIZATION_DENIED"


def test_r35_hardlink_alias_shares_database_inode_lease(tmp_path: Path) -> None:
    async def prepare() -> tuple[SQLiteVecEngine, MemoryAdapter]:
        return await _open_adapter(tmp_path)

    engine, adapter = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    alias = tmp_path / "memory-hardlink.db"
    os.link(db, alias)
    try:
        with pytest.raises(RecoveryHold) as blocked:
            with MaintenanceLease(str(alias), "r35-hardlink"):
                pass
        assert blocked.value.code == "HOLD_NONQUIESCENT_NO_MUTATION"
    finally:
        asyncio.run(adapter.close())
        assert engine.conn is None


def test_r36_retargeted_directory_alias_same_inode_cannot_split_lease(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir(); second.mkdir()
    alias_dir = tmp_path / "active"
    alias_dir.symlink_to(first, target_is_directory=True)

    async def prepare() -> tuple[SQLiteVecEngine, MemoryAdapter]:
        return await _open_adapter(alias_dir)

    engine, adapter = asyncio.run(prepare())
    first_db = first / "memory.db"
    second_db = second / "memory.db"
    os.link(first_db, second_db)
    alias_dir.unlink()
    alias_dir.symlink_to(second, target_is_directory=True)
    try:
        with pytest.raises(RecoveryHold) as blocked:
            with MaintenanceLease(str(alias_dir / "memory.db"), "r36-retarget"):
                pass
        assert blocked.value.code == "HOLD_NONQUIESCENT_NO_MUTATION"
    finally:
        asyncio.run(adapter.close())
        assert engine.conn is None


def test_r37_promoted_new_inode_is_exclusive_before_canonical_replace_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        target = await _store(
            adapter, key="r37-target", fact="R37 promotion target.", marker="f"
        )
        row = await _target_row(engine, target["memory_id"])
        await adapter.close()
        return row

    target_row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    plan = _plan(db, target_row, operation="r37-new-inode-lock")
    import factlane.recovery as recovery_module
    original_replace = recovery_module.os.replace
    checked = {"value": False}

    def replace_then_probe(src: str | Path, dst: str | Path) -> None:
        original_replace(src, dst)
        if str(dst) == str(db) and str(src).endswith(".sanitized"):
            import factlane.storage as storage_module
            with pytest.raises(AdapterError) as blocked:
                storage_module._acquire_runtime_maintenance_lease(str(db))
            assert blocked.value.code == "MAINTENANCE_IN_PROGRESS"
            checked["value"] = True

    monkeypatch.setattr(recovery_module.os, "replace", replace_then_probe)
    result = _run(plan, tmp_path)
    assert result.promoted is True
    assert checked["value"] is True


def test_r38_postflight_close_failure_retains_all_exclusion_inodes_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        target = await _store(
            adapter, key="r38-target", fact="R38 postflight cleanup target.", marker="1"
        )
        row = await _target_row(engine, target["memory_id"])
        await adapter.close()
        return row

    target_row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    old_alias = tmp_path / "old-inode.db"
    os.link(db, old_alias)
    plan = _plan(db, target_row, operation="r38-close-failure")
    original_close = SQLiteVecEngine.close
    captured: dict[str, SQLiteVecEngine] = {}

    async def fail_maintenance_close(engine: SQLiteVecEngine) -> None:
        if engine._maintenance_capability is not None:
            captured["engine"] = engine
            raise RuntimeError("synthetic postflight close failure")
        await original_close(engine)

    monkeypatch.setattr(SQLiteVecEngine, "close", fail_maintenance_close)
    operator = SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _: [])
    try:
        with pytest.raises(RecoveryHold) as caught:
            _run(plan, tmp_path, operator=operator)
        assert caught.value.code == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
        assert captured["engine"].conn is not None

        for path in (db, old_alias):
            contender = SQLiteVecEngine(str(path), profile())
            with pytest.raises(AdapterError) as blocked:
                asyncio.run(contender.open())
            assert blocked.value.code == "MAINTENANCE_IN_PROGRESS"

        with pytest.raises(RecoveryHold):
            with MaintenanceLease(str(db), "r38-second-recovery"):
                pass
    finally:
        monkeypatch.setattr(SQLiteVecEngine, "close", original_close)
        engine = captured.get("engine")
        if engine is not None and engine.conn is not None:
            engine.conn.close()
            if engine.storage is not None and hasattr(engine.storage, "conn"):
                engine.storage.conn = None
        import factlane.recovery as recovery_module
        handles = recovery_module._RETAINED_MAINTENANCE_EXCLUSIONS.pop(str(db.resolve()), [])
        import fcntl
        for handle in handles:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def test_r39_cancelled_postflight_close_retains_exclusion_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        target = await _store(
            adapter, key="r39-target", fact="R39 cancellation target.", marker="2"
        )
        row = await _target_row(engine, target["memory_id"])
        await adapter.close()
        return row

    target_row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    plan = _plan(db, target_row, operation="r39-close-cancel")
    original_close = SQLiteVecEngine.close
    captured: dict[str, SQLiteVecEngine] = {}

    async def cancel_maintenance_close(engine: SQLiteVecEngine) -> None:
        if engine._maintenance_capability is not None:
            captured["engine"] = engine
            raise asyncio.CancelledError
        await original_close(engine)

    monkeypatch.setattr(SQLiteVecEngine, "close", cancel_maintenance_close)
    operator = SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _: [])
    try:
        with pytest.raises(asyncio.CancelledError):
            _run(plan, tmp_path, operator=operator)
        contender = SQLiteVecEngine(str(db), profile())
        with pytest.raises(AdapterError) as blocked:
            asyncio.run(contender.open())
        assert blocked.value.code == "MAINTENANCE_IN_PROGRESS"
    finally:
        monkeypatch.setattr(SQLiteVecEngine, "close", original_close)
        engine = captured.get("engine")
        if engine is not None and engine.conn is not None:
            engine.conn.close()
            if engine.storage is not None and hasattr(engine.storage, "conn"):
                engine.storage.conn = None
        import factlane.recovery as recovery_module
        handles = recovery_module._RETAINED_MAINTENANCE_EXCLUSIONS.pop(str(db.resolve()), [])
        import fcntl
        for handle in handles:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def test_r40_postflight_open_failure_retains_exclusion_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def prepare() -> dict[str, Any]:
        engine, adapter = await _open_adapter(tmp_path)
        target = await _store(
            adapter, key="r40-target", fact="R40 postflight-open target.", marker="3"
        )
        row = await _target_row(engine, target["memory_id"])
        await adapter.close()
        return row

    target_row = asyncio.run(prepare())
    db = tmp_path / "memory.db"
    plan = _plan(db, target_row, operation="r40-open-failure")
    original_open = SQLiteVecEngine.open
    captured: dict[str, SQLiteVecEngine] = {}

    async def fail_maintenance_open(engine: SQLiteVecEngine) -> None:
        if engine._maintenance_capability is not None:
            captured["engine"] = engine
            engine.conn = sqlite3.connect(engine.db_path)
            raise RuntimeError("synthetic postflight open failure after SQLite attach")
        await original_open(engine)

    monkeypatch.setattr(SQLiteVecEngine, "open", fail_maintenance_open)
    operator = SensitiveMemoryRecoveryOperator(foreign_handle_provider=lambda _: [])
    try:
        with pytest.raises(RecoveryHold) as caught:
            _run(plan, tmp_path, operator=operator)
        assert caught.value.code == "S1_LOGICAL_PURGE_COMMITTED_SEALING_INCOMPLETE"
        assert captured["engine"].conn is not None

        contender = SQLiteVecEngine(str(db), profile())
        with pytest.raises(AdapterError) as blocked:
            asyncio.run(contender.open())
        assert blocked.value.code == "MAINTENANCE_IN_PROGRESS"
    finally:
        monkeypatch.setattr(SQLiteVecEngine, "open", original_open)
        engine = captured.get("engine")
        if engine is not None and engine.conn is not None:
            engine.conn.close()
            engine.conn = None
        import factlane.recovery as recovery_module
        handles = recovery_module._RETAINED_MAINTENANCE_EXCLUSIONS.pop(str(db.resolve()), [])
        import fcntl
        for handle in handles:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def test_r41_fail_closed_retained_exclusion_revokes_maintenance_capability(
    tmp_path: Path,
) -> None:
    db = tmp_path / "memory.db"
    sqlite3.connect(db).close()
    capability = None
    with MaintenanceLease(str(db), "r41-capability-revoke") as lease:
        capability = lease.capability
        assert capability is not None
        lease.retain_fail_closed()

    assert capability is not None
    with pytest.raises(AdapterError) as expired:
        SQLiteVecEngine._for_maintenance(str(db), profile(), capability)
    assert expired.value.code == "WRITE_AUTHORIZATION_DENIED"

    import factlane.recovery as recovery_module
    handles = recovery_module._RETAINED_MAINTENANCE_EXCLUSIONS.pop(str(db.resolve()), [])
    import fcntl
    for handle in handles:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


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
        reviewer = MemoryAdapter(
            reopened,
            FixedProvider(profile()),  # type: ignore[arg-type]
            trusted_write_context=trusted_write_context_for_profile("read-only"),
        )
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
        # This test intentionally simulates trusted internal post-commit drift. Raw
        # legacy writers remain fenced by the dedicated storage-v2 negative control.
        register_storage_v2_writer(conn)
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
