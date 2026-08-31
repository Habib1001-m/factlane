from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter
from factlane.contract import AdapterError, ScopeContext
from factlane.embeddings import EmbeddingProfile
from factlane.storage import SQLiteVecEngine


def profile() -> EmbeddingProfile:
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


class FixedProvider:
    def __init__(self, embedding_profile: EmbeddingProfile) -> None:
        self.profile = embedding_profile
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        vector = [0.0] * (self.profile.output_dimension - 1) + [1.0]
        return [list(vector) for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        del text
        self.query_calls += 1
        return [0.0] * (self.profile.output_dimension - 1) + [1.0]


async def _open_adapter(tmp_path, filename: str = "memory.db") -> tuple[SQLiteVecEngine, MemoryAdapter]:
    embedding_profile = profile()
    engine = SQLiteVecEngine(str(tmp_path / filename), embedding_profile)
    await engine.open()
    return engine, MemoryAdapter(engine, FixedProvider(embedding_profile))  # type: ignore[arg-type]


def _provenance(key: str, marker: str) -> dict[str, str]:
    return {
        "source_class": "CURRENT_REPO",
        "source_ref": key,
        "source_hash": marker * 64,
        "review_ref": "storage-compaction-tests",
        "extraction_method": "AUTOMATED_CHECK",
    }


async def _store_current(adapter: MemoryAdapter, *, key: str, fact: str, marker: str) -> dict[str, Any]:
    stamp = "2026-08-30T00:00:00Z"
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
        tags=["subject:storage-compaction"],
    )
    return result["results"][0]


async def _replace_current(
    adapter: MemoryAdapter,
    current: dict[str, Any],
    *,
    key: str,
    fact: str,
    marker: str,
) -> dict[str, Any]:
    stamp = "2026-08-30T00:00:00Z"
    result = await adapter.update(
        memory_id=current["memory_id"],
        scope="PROJECT",
        project_id="factlane",
        expected_revision=current["revision"],
        mode="REPLACE",
        idempotency_key=key,
        replacement={
            "fact": fact,
            "memory_type": "PROJECT_LEARNED_FACT",
            "source_provenance": _provenance(key, marker),
            "freshness_policy": {"kind": "manual"},
            "source_timestamp": stamp,
            "verified_by": "OWNER",
            "tags": ["subject:storage-compaction"],
        },
    )
    return result["results"][0]


async def _lineage(adapter: MemoryAdapter, *, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    old = await _store_current(
        adapter,
        key=f"{key}-original",
        fact="The original authoritative fact remains available as history.",
        marker="a",
    )
    current = await _replace_current(
        adapter,
        old,
        key=f"{key}-replacement",
        fact="The replacement authoritative fact remains current.",
        marker="b",
    )
    return old, current


def _scope(adapter: MemoryAdapter) -> ScopeContext:
    return adapter._safe_scope("PROJECT", "factlane", None, None, None)


def _insert_edge(engine: SQLiteVecEngine, source_hash: str, target_hash: str) -> None:
    assert engine.conn is not None
    engine.conn.execute(
        "INSERT INTO memory_graph(source_hash, target_hash, similarity, connection_types, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_hash, target_hash, 0.9, '["related"]', "{}", 1.0),
    )
    engine.conn.commit()


def _snapshot(engine: SQLiteVecEngine) -> tuple[Any, ...]:
    assert engine.conn is not None
    conn = engine.conn
    return (
        tuple(conn.execute("SELECT * FROM adapter_records ORDER BY record_id").fetchall()),
        tuple(conn.execute("SELECT * FROM memories ORDER BY id").fetchall()),
        tuple(conn.execute("SELECT rowid FROM memory_embeddings ORDER BY rowid").fetchall()),
        tuple(conn.execute("SELECT * FROM memory_graph ORDER BY source_hash, target_hash").fetchall()),
    )


def _lineage_forks(engine: SQLiteVecEngine) -> int:
    assert engine.conn is not None
    rows = engine.conn.execute(
        "SELECT record_id, parent_record_id, lifecycle_state FROM adapter_records"
    ).fetchall()
    parents = {row[0]: row[1] for row in rows}
    current_roots: dict[str, int] = {}
    for record_id, _, lifecycle_state in rows:
        if lifecycle_state != "VALIDATED_CURRENT":
            continue
        root = record_id
        seen: set[str] = set()
        while parents[root] is not None:
            if root in seen:
                raise AssertionError("adapter lineage contains a cycle")
            seen.add(root)
            root = parents[root]
        current_roots[root] = current_roots.get(root, 0) + 1
    return sum(max(0, count - 1) for count in current_roots.values())


def _assert_no_orphans(engine: SQLiteVecEngine) -> None:
    assert engine.conn is not None
    conn = engine.conn
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_embeddings e "
        "WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = e.rowid)"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM memories m "
        "WHERE NOT EXISTS (SELECT 1 FROM adapter_records a WHERE a.native_content_hash = m.content_hash)"
    ).fetchone()[0] == 0
    assert _lineage_forks(engine) == 0


async def _verify_compaction_and_restart(tmp_path) -> None:
    engine, adapter = await _open_adapter(tmp_path)
    old, current = await _lineage(adapter, key="history")
    scope = _scope(adapter)
    old_before = (await engine.get_record(old["memory_id"], scope, history=True))[0]
    current_before = (await engine.get_record(current["memory_id"], scope, history=True))[0]
    assert engine.conn is not None
    old_native_id = engine.conn.execute(
        "SELECT id FROM memories WHERE content_hash = ?",
        (old_before["native_content_hash"],),
    ).fetchone()[0]
    _insert_edge(engine, old_before["native_content_hash"], current_before["native_content_hash"])

    try:
        assert await engine.compact_superseded_record(old["record_id"]) is True
        old_after = (await engine.get_record(old["memory_id"], scope, history=True))[0]
        current_after = (await engine.get_record(current["memory_id"], scope, history=True))[0]
        assert {**old_before, "lifecycle_state": "HISTORICAL"} == old_after
        assert current_after == current_before

        historical_get = await adapter.get(
            memory_id=old["memory_id"],
            scope="PROJECT",
            project_id="factlane",
            retrieval_mode="REVIEW_HISTORY",
        )
        assert historical_get["results"][0]["lifecycle_state"] == "HISTORICAL"
        keyword_history = await adapter.search(
            query=old["fact"],
            intent_class="HISTORICAL_QUESTION",
            scope="PROJECT",
            project_id="factlane",
            retrieval_mode="REVIEW_HISTORY",
            retrieval_mode_kind="EXACT",
        )
        assert [row["record_id"] for row in keyword_history["results"]] == [old["record_id"]]
        semantic_history = await adapter.search(
            query=old["fact"],
            intent_class="HISTORICAL_QUESTION",
            scope="PROJECT",
            project_id="factlane",
            retrieval_mode="REVIEW_HISTORY",
            retrieval_mode_kind="SEMANTIC",
        )
        assert old["record_id"] not in {row["record_id"] for row in semantic_history["results"]}
        assert current["record_id"] in {row["record_id"] for row in semantic_history["results"]}

        current_get = await adapter.get(
            memory_id=current["memory_id"],
            scope="PROJECT",
            project_id="factlane",
        )
        assert [row["record_id"] for row in current_get["results"]] == [current["record_id"]]
        assert current_get["results"][0]["authority_role"] == current["authority_role"]
        assert current_get["results"][0]["lifecycle_state"] == "VALIDATED_CURRENT"
        assert engine.conn is not None
        assert engine.conn.execute(
            "SELECT COUNT(*) FROM memory_graph WHERE source_hash = ? OR target_hash = ?",
            (old_before["native_content_hash"], old_before["native_content_hash"]),
        ).fetchone()[0] == 0
        assert engine.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content_hash = ?",
            (old_before["native_content_hash"],),
        ).fetchone()[0] == 0
        assert engine.conn.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE rowid = ?",
            (old_native_id,),
        ).fetchone()[0] == 0
        status = await engine.status(scope)
        assert status["counts"]["HISTORICAL"] == 1
        assert status["counts"]["VALIDATED_CURRENT"] == 1
        _assert_no_orphans(engine)
        assert engine.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert engine.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        await adapter.close()

    engine, adapter = await _open_adapter(tmp_path)
    try:
        historical_get = await adapter.get(
            memory_id=old["memory_id"],
            scope="PROJECT",
            project_id="factlane",
            retrieval_mode="REVIEW_HISTORY",
        )
        assert historical_get["results"][0]["lifecycle_state"] == "HISTORICAL"
        current_get = await adapter.get(
            memory_id=current["memory_id"],
            scope="PROJECT",
            project_id="factlane",
        )
        assert current_get["results"][0]["revision"] == 1
        reverified = await adapter.update(
            memory_id=current["memory_id"],
            scope="PROJECT",
            project_id="factlane",
            expected_revision=1,
            mode="REVERIFY",
            idempotency_key="history-reverify",
            verification={
                "source_provenance": _provenance("history-reverify", "c"),
                "source_timestamp": "2026-08-31T00:00:00Z",
                "verified_by": "OWNER",
            },
        )
        assert reverified["results"][0]["memory_id"] == current["memory_id"]
        assert reverified["results"][0]["revision"] == 2
        assert reverified["results"][0]["lifecycle_state"] == "VALIDATED_CURRENT"
        assert _lineage_forks(engine) == 0
        assert engine.conn is not None
        assert engine.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert engine.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        await adapter.close()


def test_compaction_preserves_history_and_current_authority_across_restart(tmp_path) -> None:
    asyncio.run(_verify_compaction_and_restart(tmp_path))


async def _verify_refusals(tmp_path) -> None:
    engine, adapter = await _open_adapter(tmp_path)
    try:
        current = await _store_current(
            adapter,
            key="refusal-current",
            fact="The current fact must remain authoritative.",
            marker="a",
        )
        for record_id in (current["record_id"],):
            before = _snapshot(engine)
            with pytest.raises(AdapterError):
                await engine.compact_superseded_record(record_id)
            assert _snapshot(engine) == before

        candidate = await adapter.store(
            fact="A candidate fact is not eligible for compaction.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            source_provenance=_provenance("refusal-candidate", "b"),
            freshness_policy={"kind": "manual"},
            idempotency_key="refusal-candidate",
            project_id="factlane",
            requested_lifecycle_state="CANDIDATE",
        )
        candidate_id = candidate["results"][0]["record_id"]
        for lifecycle in ("CANDIDATE", "STALE", "QUARANTINED"):
            assert engine.conn is not None
            engine.conn.execute(
                "UPDATE adapter_records SET lifecycle_state = ? WHERE record_id = ?",
                (lifecycle, candidate_id),
            )
            engine.conn.commit()
            before = _snapshot(engine)
            with pytest.raises(AdapterError):
                await engine.compact_superseded_record(candidate_id)
            assert _snapshot(engine) == before

        before = _snapshot(engine)
        with pytest.raises(AdapterError) as missing:
            await engine.compact_superseded_record("missing-record")
        assert missing.value.code == "NOT_FOUND"
        assert _snapshot(engine) == before
    finally:
        await adapter.close()


def test_compaction_refuses_current_unsupported_and_unknown_records(tmp_path) -> None:
    asyncio.run(_verify_refusals(tmp_path))


async def _verify_partial_state(tmp_path) -> None:
    variants = (
        ("missing-vector", "DELETE FROM memory_embeddings WHERE rowid = (SELECT id FROM memories WHERE content_hash = ?)",),
        ("tombstoned-native", "UPDATE memories SET deleted_at = 1 WHERE content_hash = ?",),
        ("missing-native", "DELETE FROM memories WHERE content_hash = ?",),
    )
    for filename, statement in variants:
        engine, adapter = await _open_adapter(tmp_path, f"{filename}.db")
        try:
            old, _ = await _lineage(adapter, key=filename)
            scope = _scope(adapter)
            old_row = (await engine.get_record(old["memory_id"], scope, history=True))[0]
            assert engine.conn is not None
            engine.conn.execute(statement, (old_row["native_content_hash"],))
            engine.conn.commit()
            before = _snapshot(engine)
            with pytest.raises(AdapterError):
                await engine.compact_superseded_record(old["record_id"])
            assert _snapshot(engine) == before
            unchanged = (await engine.get_record(old["memory_id"], scope, history=True))[0]
            assert unchanged["lifecycle_state"] == "SUPERSEDED"
        finally:
            await adapter.close()


def test_compaction_fails_closed_on_partial_materialization(tmp_path) -> None:
    asyncio.run(_verify_partial_state(tmp_path))


async def _verify_rollback(tmp_path) -> None:
    engine, adapter = await _open_adapter(tmp_path)
    try:
        old, current = await _lineage(adapter, key="rollback")
        scope = _scope(adapter)
        old_row = (await engine.get_record(old["memory_id"], scope, history=True))[0]
        current_row = (await engine.get_record(current["memory_id"], scope, history=True))[0]
        _insert_edge(engine, old_row["native_content_hash"], current_row["native_content_hash"])
        assert engine.conn is not None
        engine.conn.execute(
            "CREATE TRIGGER fail_compaction_delete BEFORE DELETE ON memories "
            f"WHEN OLD.content_hash = '{old_row['native_content_hash']}' "
            "BEGIN SELECT RAISE(ABORT, 'late compaction failure'); END"
        )
        engine.conn.commit()
        before = _snapshot(engine)
        with pytest.raises(sqlite3.IntegrityError, match="late compaction failure"):
            await engine.compact_superseded_record(old["record_id"])
        assert _snapshot(engine) == before
        engine.conn.execute("DROP TRIGGER fail_compaction_delete")
        engine.conn.commit()
        assert await engine.compact_superseded_record(old["record_id"]) is True
        _assert_no_orphans(engine)
    finally:
        await adapter.close()


def test_compaction_rolls_back_after_late_transaction_failure(tmp_path) -> None:
    asyncio.run(_verify_rollback(tmp_path))


async def _verify_idempotent_compaction(tmp_path) -> None:
    engine, adapter = await _open_adapter(tmp_path)
    try:
        old, current = await _lineage(adapter, key="idempotent")
        assert engine.conn is not None
        scope = _scope(adapter)
        old_row = (await engine.get_record(old["memory_id"], scope, history=True))[0]
        current_before = (await engine.get_record(current["memory_id"], scope, history=True))[0]
        old_native_id = engine.conn.execute(
            "SELECT id FROM memories WHERE content_hash = ?",
            (old_row["native_content_hash"],),
        ).fetchone()[0]
        vector = engine.conn.execute(
            "SELECT content_embedding, store FROM memory_embeddings WHERE rowid = ?",
            (old_native_id,),
        ).fetchone()
        assert await engine.compact_superseded_record(old["record_id"]) is True
        before = _snapshot(engine)
        assert await engine.compact_superseded_record(old["record_id"]) is False
        assert _snapshot(engine) == before
        target_after = (await engine.get_record(old["memory_id"], scope, history=True))[0]
        assert vector is not None
        current_native_ids = {
            row[0] for row in engine.conn.execute("SELECT id FROM memories").fetchall()
        }
        unrelated_orphan_id = max(current_native_ids) + 1000
        engine.conn.execute(
            "INSERT INTO memory_embeddings(rowid, content_embedding, store) VALUES (?, ?, ?)",
            (unrelated_orphan_id, vector[0], vector[1]),
        )
        engine.conn.commit()
        assert await engine.compact_superseded_record(old["record_id"]) is False
        assert (await engine.get_record(old["memory_id"], scope, history=True))[0] == target_after
        assert (await engine.get_record(current["memory_id"], scope, history=True))[0] == current_before
        orphan = engine.conn.execute(
            "SELECT rowid, content_embedding, store FROM memory_embeddings WHERE rowid = ?",
            (unrelated_orphan_id,),
        ).fetchone()
        assert orphan is not None
        assert orphan[1:] == vector
    finally:
        await adapter.close()


def test_compaction_is_a_safe_noop_after_successful_compaction(tmp_path) -> None:
    asyncio.run(_verify_idempotent_compaction(tmp_path))
