from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter
from factlane.contract import AdapterError
from factlane.embeddings import EmbeddingProfile
from factlane.storage import SQLiteVecEngine


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


class _Usage:
    def __init__(self, free: int) -> None:
        self.free = free


async def _open_adapter(tmp_path, filename: str = "memory.db") -> tuple[SQLiteVecEngine, MemoryAdapter]:
    embedding_profile = _profile()
    engine = SQLiteVecEngine(str(tmp_path / filename), embedding_profile)
    await engine.open()
    return engine, MemoryAdapter(engine, _FixedProvider(embedding_profile))  # type: ignore[arg-type]


def _scope(adapter: MemoryAdapter, project_id: str = "factlane"):
    return adapter._safe_scope("PROJECT", project_id, None, None, None)


def _provenance(key: str, marker: str) -> dict[str, str]:
    return {
        "source_class": "CURRENT_REPO",
        "source_ref": key,
        "source_hash": marker * 64,
        "review_ref": "storage-housekeeping-tests",
        "extraction_method": "AUTOMATED_CHECK",
    }


async def _store_current(
    adapter: MemoryAdapter,
    *,
    key: str,
    fact: str,
    marker: str,
    project_id: str = "factlane",
) -> dict[str, Any]:
    result = await adapter.store(
        fact=fact,
        scope="PROJECT",
        memory_type="PROJECT_LEARNED_FACT",
        source_provenance=_provenance(key, marker),
        freshness_policy={"kind": "manual"},
        idempotency_key=key,
        project_id=project_id,
        source_timestamp="2026-08-30T00:00:00Z",
        last_verified_at="2026-08-30T00:00:00Z",
        verified_by="OWNER",
        requested_lifecycle_state="VALIDATED_CURRENT",
        confidence=0.95,
        tags=["subject:storage-housekeeping"],
        subject=key,
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
    project_id = current["scope"]["project_id"]
    result = await adapter.update(
        memory_id=current["memory_id"],
        scope="PROJECT",
        project_id=project_id,
        expected_revision=current["revision"],
        mode="REPLACE",
        idempotency_key=key,
        replacement={
            "fact": fact,
            "memory_type": "PROJECT_LEARNED_FACT",
            "source_provenance": _provenance(key, marker),
            "freshness_policy": {"kind": "manual"},
            "source_timestamp": "2026-08-30T00:00:00Z",
            "verified_by": "OWNER",
            "tags": ["subject:storage-housekeeping"],
            "subject": key,
        },
    )
    return result["results"][0]


async def _superseded(
    adapter: MemoryAdapter,
    key: str = "lineage",
    project_id: str = "factlane",
) -> tuple[dict[str, Any], dict[str, Any]]:
    old = await _store_current(
        adapter,
        key=f"{key}-old",
        fact=f"The old record {key} is superseded and fully materialized.",
        marker="a",
        project_id=project_id,
    )
    current = await _replace_current(
        adapter,
        old,
        key=f"{key}-current",
        fact=f"The replacement record {key} remains current.",
        marker="b",
    )
    return old, current


def _record_hash(engine: SQLiteVecEngine, record_id: str) -> str:
    assert engine.conn is not None
    return engine.conn.execute(
        "SELECT native_content_hash FROM adapter_records WHERE record_id = ?",
        (record_id,),
    ).fetchone()[0]


def _snapshot(engine: SQLiteVecEngine) -> tuple[Any, ...]:
    assert engine.conn is not None
    return (
        tuple(engine.conn.execute("SELECT * FROM adapter_records ORDER BY record_id").fetchall()),
        tuple(engine.conn.execute("SELECT * FROM memories ORDER BY id").fetchall()),
        tuple(engine.conn.execute("SELECT rowid, content_embedding, store FROM memory_embeddings ORDER BY rowid").fetchall()),
        tuple(engine.conn.execute("SELECT * FROM memory_graph ORDER BY source_hash, target_hash").fetchall()),
        tuple(engine.conn.execute("SELECT * FROM adapter_meta ORDER BY key").fetchall()),
    )


def _lineage_forks(engine: SQLiteVecEngine, project_id: str | None = None) -> int:
    assert engine.conn is not None
    if project_id is None:
        rows = engine.conn.execute(
            "SELECT memory_id, COUNT(*) FROM adapter_records "
            "WHERE lifecycle_state = 'VALIDATED_CURRENT' GROUP BY memory_id"
        ).fetchall()
    else:
        rows = engine.conn.execute(
            "SELECT memory_id, COUNT(*) FROM adapter_records "
            "WHERE lifecycle_state = 'VALIDATED_CURRENT' AND project_id = ? GROUP BY memory_id",
            (project_id,),
        ).fetchall()
    return sum(max(0, int(count) - 1) for _, count in rows)


def _assert_no_orphans(engine: SQLiteVecEngine, project_id: str | None = None) -> None:
    assert engine.conn is not None
    assert engine.conn.execute(
        "SELECT COUNT(*) FROM memory_embeddings e "
        "WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = e.rowid)"
    ).fetchone()[0] == 0
    assert _lineage_forks(engine, project_id) == 0


def _patch_free_space(monkeypatch, *free_bytes: int) -> None:
    values = iter(free_bytes)
    last = free_bytes[-1]

    def disk_usage(_path: str) -> _Usage:
        return _Usage(next(values, last))

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)


def _insert_vector_orphan(engine: SQLiteVecEngine, source_record_id: str) -> int:
    assert engine.conn is not None
    vector = engine.conn.execute(
        "SELECT content_embedding, store FROM memory_embeddings WHERE rowid = "
        "(SELECT id FROM memories WHERE content_hash = ?)",
        (_record_hash(engine, source_record_id),),
    ).fetchone()
    assert vector is not None
    existing_ids = [row[0] for row in engine.conn.execute("SELECT id FROM memories").fetchall()]
    orphan_id = max(existing_ids) + 1000
    engine.conn.execute(
        "INSERT INTO memory_embeddings(rowid, content_embedding, store) VALUES (?, ?, ?)",
        (orphan_id, vector[0], vector[1]),
    )
    engine.conn.commit()
    return orphan_id


def _insert_current_fork(engine: SQLiteVecEngine, record_id: str) -> str:
    assert engine.conn is not None
    columns = [row[1] for row in engine.conn.execute("PRAGMA table_info(adapter_records)").fetchall()]
    values = list(engine.conn.execute("SELECT * FROM adapter_records WHERE record_id = ?", (record_id,)).fetchone())
    fork_id = f"{record_id}-fork"
    values[columns.index("record_id")] = fork_id
    values[columns.index("native_content_hash")] = "f" * 64
    values[columns.index("payload_fingerprint")] = "e" * 64
    values[columns.index("idempotency_key")] = f"{record_id}-fork-key"
    placeholders = ",".join("?" for _ in columns)
    engine.conn.execute(
        f"INSERT INTO adapter_records ({','.join(columns)}) VALUES ({placeholders})",
        values,
    )
    engine.conn.commit()
    return fork_id


def test_housekeeping_is_an_internal_storage_surface() -> None:
    assert hasattr(SQLiteVecEngine, "housekeep_superseded")
    assert not hasattr(MemoryAdapter, "housekeep_superseded")


def test_housekeeping_rejects_invalid_bounds_before_mutation(tmp_path) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            await _superseded(adapter, "invalid")
            before = _snapshot(engine)
            with pytest.raises(AdapterError, match="max_records"):
                await engine.housekeep_superseded(
                    _scope(adapter), max_records=0, required_free_bytes=1
                )
            with pytest.raises(AdapterError, match="required_free_bytes"):
                await engine.housekeep_superseded(
                    _scope(adapter), max_records=1, required_free_bytes=-1
                )
            with pytest.raises(AdapterError, match="max_records"):
                await engine.housekeep_superseded(
                    _scope(adapter), max_records=True, required_free_bytes=1
                )
            assert _snapshot(engine) == before
        finally:
            await adapter.close()

    asyncio.run(run())


def test_no_pressure_is_a_noop(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            await _superseded(adapter, "no-pressure")
            _patch_free_space(monkeypatch, 1000)
            before = _snapshot(engine)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=2, required_free_bytes=100
            )
            assert result["outcome"] == "NO_ACTION_CAPACITY_REQUIREMENT_ALREADY_MET"
            assert result["mutation"] == "NONE"
            assert result["selected_records"] == 0
            assert result["compacted_records"] == 0
            assert result["capacity_requirement_met_before"] is True
            assert _snapshot(engine) == before
        finally:
            await adapter.close()

    asyncio.run(run())


def test_unknown_capacity_blocks_without_mutation(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            await _superseded(adapter, "unknown-capacity")

            def unavailable(_path: str) -> _Usage:
                raise OSError("capacity unavailable")

            monkeypatch.setattr(shutil, "disk_usage", unavailable)
            before = _snapshot(engine)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=1, required_free_bytes=100
            )
            assert result["outcome"] == "BLOCKED_UNKNOWN_CAPACITY"
            assert result["next_action"] == "RESTORE_CAPACITY_OBSERVABILITY_BEFORE_MEMORY_MUTATION"
            assert result["mutation"] == "NONE"
            assert _snapshot(engine) == before
        finally:
            await adapter.close()

    asyncio.run(run())


def test_partial_superseded_state_blocks_without_mutation(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            old, _ = await _superseded(adapter, "partial")
            assert engine.conn is not None
            engine.conn.execute(
                "DELETE FROM memory_embeddings WHERE rowid = "
                "(SELECT id FROM memories WHERE content_hash = ?)",
                (_record_hash(engine, old["record_id"]),),
            )
            engine.conn.commit()
            _patch_free_space(monkeypatch, 0)
            before = _snapshot(engine)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=1, required_free_bytes=100
            )
            assert result["outcome"] == "BLOCKED_PARTIAL_RECLAIM_STATE"
            assert result["compaction_blocked_partial"] == 1
            assert result["mutation"] == "NONE"
            assert _snapshot(engine) == before
        finally:
            await adapter.close()

    asyncio.run(run())


def test_preexisting_vector_orphan_blocks_without_mutation(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            old, _ = await _superseded(adapter, "orphan")
            orphan_id = _insert_vector_orphan(engine, old["record_id"])
            _patch_free_space(monkeypatch, 0)
            before = _snapshot(engine)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=1, required_free_bytes=100
            )
            assert result["outcome"] == "BLOCKED_PREEXISTING_VECTOR_ORPHAN"
            assert result["vector_orphans_after"] == 1
            assert result["mutation"] == "NONE"
            assert engine.conn is not None
            assert engine.conn.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE rowid = ?", (orphan_id,)
            ).fetchone()[0] == 1
            assert _snapshot(engine) == before
        finally:
            await adapter.close()

    asyncio.run(run())


def test_preexisting_current_fork_blocks_without_mutation(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            old, current = await _superseded(adapter, "fork")
            _insert_current_fork(engine, current["record_id"])
            _patch_free_space(monkeypatch, 0)
            before = _snapshot(engine)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=1, required_free_bytes=100
            )
            assert result["outcome"] == "BLOCKED_PREEXISTING_CURRENT_FORK"
            assert result["current_lineage_forks_after"] == 1
            assert result["mutation"] == "NONE"
            assert (await engine.get_record(old["memory_id"], _scope(adapter), history=True))[0]["lifecycle_state"] == "SUPERSEDED"
            assert _snapshot(engine) == before
        finally:
            await adapter.close()

    asyncio.run(run())


def test_preexisting_integrity_failure_blocks_without_mutation(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            old, _ = await _superseded(adapter, "integrity")

            async def failed_health(_scope) -> tuple[int, int, str, str]:
                return 0, 0, "ok", "not ok"

            monkeypatch.setattr(engine, "_housekeeping_health", failed_health, raising=False)
            _patch_free_space(monkeypatch, 0)
            before = _snapshot(engine)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=1, required_free_bytes=100
            )
            assert result["outcome"] == "BLOCKED_DATABASE_INTEGRITY"
            assert result["mutation"] == "NONE"
            assert result["integrity_check_after"] != "ok"
            assert (await engine.get_record(old["memory_id"], _scope(adapter), history=True))[0]["lifecycle_state"] == "SUPERSEDED"
            assert _snapshot(engine) == before
        finally:
            await adapter.close()

    asyncio.run(run())


def test_pressure_compacts_at_most_max_records_and_reports_health(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            lineages = [await _superseded(adapter, f"bounded-{index}") for index in range(3)]
            _patch_free_space(monkeypatch, 0, 1_000_000)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=2, required_free_bytes=100
            )
            assert result["outcome"] == "HOUSEKEEPING_COMPLETE_CAPACITY_REQUIREMENT_MET"
            assert result["selected_records"] == 2
            assert result["compacted_records"] == 2
            assert result["max_records"] == 2
            assert result["required_free_bytes"] == 100
            assert result["compaction_ready_before"] == 3
            assert result["compaction_ready_after"] == 1
            assert result["historical_total_after"] == 2
            assert result["vector_orphans_after"] == 0
            assert result["current_lineage_forks_after"] == 0
            assert result["quick_check_after"] == "ok"
            assert result["integrity_check_after"] == "ok"
            assert result["physical_file_shrink_claimed"] is False
            assert result["automatic_housekeeping"] is False
            remaining = [
                (await engine.get_record(old["memory_id"], _scope(adapter), history=True))[0]["lifecycle_state"]
                for old, _ in lineages
            ]
            assert remaining.count("HISTORICAL") == 2
            assert remaining.count("SUPERSEDED") == 1
            _assert_no_orphans(engine)
        finally:
            await adapter.close()

    asyncio.run(run())


def test_maintenance_is_scope_bound(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            old_a, _ = await _superseded(adapter, "scope-a", "scope-a")
            old_b, _ = await _superseded(adapter, "scope-b", "scope-b")
            _patch_free_space(monkeypatch, 0, 1_000_000)
            result = await engine.housekeep_superseded(
                _scope(adapter, "scope-a"), max_records=2, required_free_bytes=100
            )
            assert result["compacted_records"] == 1
            assert (await engine.get_record(old_a["memory_id"], _scope(adapter, "scope-a"), history=True))[0]["lifecycle_state"] == "HISTORICAL"
            assert (await engine.get_record(old_b["memory_id"], _scope(adapter, "scope-b"), history=True))[0]["lifecycle_state"] == "SUPERSEDED"
        finally:
            await adapter.close()

    asyncio.run(run())


def test_current_authority_is_preserved(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            old, current = await _superseded(adapter, "authority")
            before = (await engine.get_record(current["memory_id"], _scope(adapter), history=True))[0]
            _patch_free_space(monkeypatch, 0, 1_000_000)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=1, required_free_bytes=100
            )
            after = (await engine.get_record(current["memory_id"], _scope(adapter), history=True))[0]
            assert result["compacted_records"] == 1
            assert after == before
            assert after["lifecycle_state"] == "VALIDATED_CURRENT"
            assert (await engine.get_record(old["memory_id"], _scope(adapter), history=True))[0]["lifecycle_state"] == "HISTORICAL"
        finally:
            await adapter.close()

    asyncio.run(run())


def test_unexpected_compaction_failure_stops_the_run(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            await _superseded(adapter, "failure-one")
            await _superseded(adapter, "failure-two")
            await _superseded(adapter, "failure-three")
            original = engine.compact_superseded_record
            calls: list[str] = []

            async def fail_on_second(record_id: str) -> bool:
                calls.append(record_id)
                if len(calls) == 2:
                    raise AdapterError("COMPACTION_STATE_INVALID", "unexpected compaction failure")
                return await original(record_id)

            monkeypatch.setattr(engine, "compact_superseded_record", fail_on_second)
            _patch_free_space(monkeypatch, 0, 1_000_000)
            with pytest.raises(AdapterError, match="unexpected compaction failure"):
                await engine.housekeep_superseded(
                    _scope(adapter), max_records=3, required_free_bytes=100
                )
            assert len(calls) == 2
        finally:
            await adapter.close()

    asyncio.run(run())


def test_unmet_capacity_requirement_is_reported_without_success_claim(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            old, _ = await _superseded(adapter, "unmet")
            _patch_free_space(monkeypatch, 0, 0)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=1, required_free_bytes=100
            )
            assert result["outcome"] == "HOUSEKEEPING_COMPLETE_CAPACITY_REQUIREMENT_NOT_MET"
            assert result["capacity_requirement_met_before"] is False
            assert result["capacity_requirement_met_after"] is False
            assert result["next_action"] == "ADDITIONAL_CAPACITY_OR_OPERATOR_ACTION_REQUIRED"
            assert result["compacted_records"] == 1
            assert (await engine.get_record(old["memory_id"], _scope(adapter), history=True))[0]["lifecycle_state"] == "HISTORICAL"
        finally:
            await adapter.close()

    asyncio.run(run())


def test_capacity_requirement_met_after_observation_is_reported_truthfully(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            await _superseded(adapter, "met-after")
            _patch_free_space(monkeypatch, 0, 100)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=1, required_free_bytes=100
            )
            assert result["outcome"] == "HOUSEKEEPING_COMPLETE_CAPACITY_REQUIREMENT_MET"
            assert result["capacity_requirement_met_before"] is False
            assert result["capacity_requirement_met_after"] is True
            assert result["filesystem_free_bytes_after"] == 100
        finally:
            await adapter.close()

    asyncio.run(run())


def test_pressure_with_no_eligible_records_is_actionable_noop(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            await _store_current(adapter, key="only-current", fact="Current authority.", marker="a")
            _patch_free_space(monkeypatch, 0)
            before = _snapshot(engine)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=1, required_free_bytes=100
            )
            assert result["outcome"] == "NO_ELIGIBLE_RECORDS_CAPACITY_REQUIREMENT_NOT_MET"
            assert result["next_action"] == "ADDITIONAL_CAPACITY_OR_OPERATOR_ACTION_REQUIRED"
            assert result["mutation"] == "NONE"
            assert _snapshot(engine) == before
        finally:
            await adapter.close()

    asyncio.run(run())


def test_repeated_invocation_remains_bounded_over_remaining_eligibility(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            lineages = [await _superseded(adapter, f"repeat-{index}") for index in range(3)]
            _patch_free_space(monkeypatch, 0, 1000, 0, 1000, 0)
            first = await engine.housekeep_superseded(
                _scope(adapter), max_records=2, required_free_bytes=100
            )
            second = await engine.housekeep_superseded(
                _scope(adapter), max_records=2, required_free_bytes=100
            )
            third = await engine.housekeep_superseded(
                _scope(adapter), max_records=2, required_free_bytes=100
            )
            assert first["compacted_records"] == 2
            assert second["compacted_records"] == 1
            assert third["outcome"] == "NO_ELIGIBLE_RECORDS_CAPACITY_REQUIREMENT_NOT_MET"
            historical = 0
            for old, _ in lineages:
                historical += (await engine.get_record(old["memory_id"], _scope(adapter), history=True))[0]["lifecycle_state"] == "HISTORICAL"
            assert historical == 3
        finally:
            await adapter.close()

    asyncio.run(run())


def test_maintenance_truth_survives_restart(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        lineages = []
        try:
            lineages = [await _superseded(adapter, f"restart-{index}") for index in range(2)]
            _patch_free_space(monkeypatch, 0, 1000)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=2, required_free_bytes=100
            )
            assert result["compacted_records"] == 2
        finally:
            await adapter.close()

        reopened, reopened_adapter = await _open_adapter(tmp_path)
        try:
            status = await reopened.status(_scope(reopened_adapter))
            assert status["counts"]["HISTORICAL"] == 2
            assert status["retention"]["compaction_ready"] == 0
            for old, _ in lineages:
                assert (await reopened.get_record(old["memory_id"], _scope(reopened_adapter), history=True))[0]["lifecycle_state"] == "HISTORICAL"
            _assert_no_orphans(reopened)
            assert reopened.conn is not None
            assert reopened.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            await reopened_adapter.close()

    asyncio.run(run())


def test_report_is_bounded_and_capacity_requirement_is_not_persisted(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            await _superseded(adapter, "report")
            _patch_free_space(monkeypatch, 0, 1000)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=1, required_free_bytes=100
            )
            serialized = json.dumps(result, sort_keys=True)
            assert "record_id" not in serialized
            assert "native_content_hash" not in serialized
            assert "source_provenance" not in serialized
            assert engine.db_path not in serialized
            assert result["required_free_bytes"] == 100
            assert result["max_records"] == 1
            assert result["physical_file_shrink_claimed"] is False
            assert result["automatic_housekeeping"] is False
            assert engine.conn is not None
            metadata = dict(engine.conn.execute("SELECT key, value FROM adapter_meta").fetchall())
            assert not any("threshold" in key or "session" in key or "required_free" in key for key in metadata)
        finally:
            await adapter.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("health_after", "field", "value"),
    (
        ((1, 0, "ok", "ok"), "vector_orphans_after", 1),
        ((0, 1, "ok", "ok"), "current_lineage_forks_after", 1),
        ((0, 0, "not ok", "ok"), "quick_check_after", "not ok"),
        ((0, 0, "ok", "not ok"), "integrity_check_after", "not ok"),
    ),
)
def test_unhealthy_post_maintenance_truth_blocks_capacity_success(
    tmp_path,
    monkeypatch,
    health_after: tuple[int, int, str, str],
    field: str,
    value: int | str,
) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            old, _ = await _superseded(adapter, f"post-health-{field}")
            calls = 0

            async def staged_health(_scope) -> tuple[int, int, str, str]:
                nonlocal calls
                calls += 1
                return (0, 0, "ok", "ok") if calls == 1 else health_after

            monkeypatch.setattr(engine, "_housekeeping_health", staged_health, raising=False)
            _patch_free_space(monkeypatch, 0, 1000)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=1, required_free_bytes=100
            )
            assert result["mutation"] == "BOUNDED_COMPACTION"
            assert result["compacted_records"] == 1
            assert result["outcome"] == "BLOCKED_POST_MAINTENANCE_HEALTH"
            assert result["next_action"] == "REPAIR_POST_MAINTENANCE_HEALTH_BEFORE_MEMORY_MUTATION"
            assert result[field] == value
            assert (await engine.get_record(old["memory_id"], _scope(adapter), history=True))[0]["lifecycle_state"] == "HISTORICAL"
        finally:
            await adapter.close()

    asyncio.run(run())


def test_unknown_post_maintenance_capacity_is_not_reported_as_not_met(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            old, _ = await _superseded(adapter, "post-capacity")
            calls = 0

            def changing_capacity(_path: str) -> _Usage:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return _Usage(0)
                raise OSError("capacity observation unavailable after maintenance")

            monkeypatch.setattr(shutil, "disk_usage", changing_capacity)
            result = await engine.housekeep_superseded(
                _scope(adapter), max_records=1, required_free_bytes=100
            )
            assert result["mutation"] == "BOUNDED_COMPACTION"
            assert result["compacted_records"] == 1
            assert result["capacity_requirement_met_before"] is False
            assert result["capacity_requirement_met_after"] is None
            assert result["filesystem_free_bytes_after"] is None
            assert result["outcome"] == "BLOCKED_POST_MAINTENANCE_UNKNOWN_CAPACITY"
            assert result["next_action"] == "RESTORE_CAPACITY_OBSERVABILITY_BEFORE_MEMORY_MUTATION"
            assert (await engine.get_record(old["memory_id"], _scope(adapter), history=True))[0]["lifecycle_state"] == "HISTORICAL"
        finally:
            await adapter.close()

    asyncio.run(run())
