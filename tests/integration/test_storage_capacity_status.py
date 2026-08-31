from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter
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
        "review_ref": "storage-capacity-tests",
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
        tags=["subject:storage-capacity"],
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
            "source_timestamp": "2026-08-30T00:00:00Z",
            "verified_by": "OWNER",
            "tags": ["subject:storage-capacity"],
        },
    )
    return result["results"][0]


async def _superseded(adapter: MemoryAdapter, key: str = "lineage") -> tuple[dict[str, Any], dict[str, Any]]:
    old = await _store_current(
        adapter,
        key=f"{key}-old",
        fact="The old record is superseded but fully materialized.",
        marker="a",
    )
    current = await _replace_current(
        adapter,
        old,
        key=f"{key}-current",
        fact="The replacement record remains current.",
        marker="b",
    )
    return old, current


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except FileNotFoundError:
        return 0


def _keys(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        result.update(value)
        for child in value.values():
            result.update(_keys(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_keys(child))
    return result


def test_status_adds_retention_and_capacity_without_dropping_backend_keys(tmp_path) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            status = await engine.status(_scope(adapter))
            assert {"available", "backend", "profile", "counts", "native_columns"} <= status.keys()
            assert set(status) == {"available", "backend", "profile", "counts", "native_columns", "retention", "capacity"}
            assert status["retention"]["policy_version"] == 1
            assert status["retention"]["automatic_housekeeping"] is False
        finally:
            await adapter.close()

    asyncio.run(run())


def test_known_capacity_reports_sqlite_file_and_filesystem_facts(tmp_path) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            status = await engine.status(_scope(adapter))
            capacity = status["capacity"]
            page_size = capacity["page_size_bytes"]
            assert capacity["observation"] == "KNOWN"
            assert page_size > 0
            assert capacity["page_count"] > 0
            assert capacity["freelist_count"] >= 0
            assert capacity["database_allocated_bytes"] == page_size * capacity["page_count"]
            assert capacity["freelist_bytes"] == page_size * capacity["freelist_count"]
            assert capacity["database_file_bytes"] == _size(engine.db_path)
            assert capacity["wal_file_bytes"] == _size(f"{engine.db_path}-wal")
            assert capacity["shm_file_bytes"] == _size(f"{engine.db_path}-shm")
            assert capacity["filesystem_free_bytes"] == shutil.disk_usage(engine.db_path).free
            assert capacity["pressure_threshold_bytes"] is None
            assert capacity["pressure_evaluation"] == "REQUIRES_BOUNDED_OPERATION_REQUIREMENT"
            assert capacity["mutation_preflight"] == "REQUIRES_BOUNDED_OPERATION_REQUIREMENT"
            assert capacity["next_action"] == "COMPARE_FILESYSTEM_FREE_BYTES_TO_BOUNDED_OPERATION_REQUIREMENT"
        finally:
            await adapter.close()

    asyncio.run(run())


def test_status_never_exposes_absolute_database_path(tmp_path) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            status = await engine.status(_scope(adapter))
            assert engine.db_path not in json.dumps(status, sort_keys=True)
            assert "db_path" not in _keys(status)
        finally:
            await adapter.close()

    asyncio.run(run())


def test_lifecycle_inventory_is_bound_to_requested_scope(tmp_path) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            await _store_current(adapter, key="scope-a", fact="Scope A fact.", marker="a", project_id="scope-a")
            await _store_current(adapter, key="scope-b", fact="Scope B fact.", marker="b", project_id="scope-b")
            status = await engine.status(_scope(adapter, "scope-a"))
            assert status["counts"]["VALIDATED_CURRENT"] == 1
            assert status["retention"]["validated_current_total"] == 1
            assert status["retention"]["historical_total"] == 0
        finally:
            await adapter.close()

    asyncio.run(run())


def test_fully_materialized_superseded_record_is_compaction_ready(tmp_path) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            await _superseded(adapter)
            retention = (await engine.status(_scope(adapter)))["retention"]
            assert retention["superseded_total"] == 1
            assert retention["compaction_ready"] == 1
            assert retention["compaction_blocked_partial"] == 0
            assert retention["historical_total"] == 0
            assert retention["validated_current_total"] == 1
        finally:
            await adapter.close()

    asyncio.run(run())


def test_compacted_record_is_historical_and_not_ready(tmp_path) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            old, _ = await _superseded(adapter, "compacted")
            assert await engine.compact_superseded_record(old["record_id"]) is True
            retention = (await engine.status(_scope(adapter)))["retention"]
            assert retention["superseded_total"] == 0
            assert retention["compaction_ready"] == 0
            assert retention["compaction_blocked_partial"] == 0
            assert retention["historical_total"] == 1
            assert retention["validated_current_total"] == 1
        finally:
            await adapter.close()

    asyncio.run(run())


def test_partial_superseded_record_is_blocked_and_status_does_not_mutate(tmp_path) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            old, _ = await _superseded(adapter, "partial")
            assert engine.conn is not None
            content_hash = engine.conn.execute(
                "SELECT native_content_hash FROM adapter_records WHERE record_id = ?",
                (old["record_id"],),
            ).fetchone()[0]
            native_id = engine.conn.execute(
                "SELECT id FROM memories WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()[0]
            engine.conn.execute("DELETE FROM memory_embeddings WHERE rowid = ?", (native_id,))
            engine.conn.commit()
            before = engine.conn.execute(
                "SELECT lifecycle_state FROM adapter_records WHERE record_id = ?",
                (old["record_id"],),
            ).fetchone()[0]
            retention = (await engine.status(_scope(adapter)))["retention"]
            after = engine.conn.execute(
                "SELECT lifecycle_state FROM adapter_records WHERE record_id = ?",
                (old["record_id"],),
            ).fetchone()[0]
            assert retention["compaction_ready"] == 0
            assert retention["compaction_blocked_partial"] == 1
            assert before == after == "SUPERSEDED"
            assert engine.conn.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE rowid = ?",
                (native_id,),
            ).fetchone()[0] == 0
        finally:
            await adapter.close()

    asyncio.run(run())


def test_current_authority_is_counted_and_never_eligible(tmp_path) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            await _store_current(adapter, key="current", fact="The current authority remains protected.", marker="a")
            retention = (await engine.status(_scope(adapter)))["retention"]
            assert retention["validated_current_total"] == 1
            assert retention["superseded_total"] == 0
            assert retention["compaction_ready"] == 0
            assert retention["current_authority_reclaim"] is False
        finally:
            await adapter.close()

    asyncio.run(run())


def test_duplicate_and_expired_reclaim_are_explicitly_not_applicable(tmp_path) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            retention = (await engine.status(_scope(adapter)))["retention"]
            assert retention["duplicate_reclaim"] == "NOT_APPLICABLE_IN_CURRENT_SCHEMA"
            assert retention["expired_reclaim"] == "NOT_APPLICABLE_IN_CURRENT_SCHEMA"
            assert retention["eligible_lifecycle_states"] == ["SUPERSEDED"]
            assert retention["trigger"] == "CAPACITY_PRESSURE_NOT_SESSION_COUNT"
        finally:
            await adapter.close()

    asyncio.run(run())


def test_status_has_no_session_count_field_or_pressure_threshold(tmp_path) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            status = await engine.status(_scope(adapter))
            keys = _keys(status)
            assert "session_count" not in keys
            assert "pressure_threshold" not in keys
            assert status["capacity"]["pressure_threshold_bytes"] is None
            assert status["retention"]["trigger"] == "CAPACITY_PRESSURE_NOT_SESSION_COUNT"
        finally:
            await adapter.close()

    asyncio.run(run())


def test_status_does_not_execute_compaction(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            await _superseded(adapter, "no-status-compaction")

            def fail(*args: Any, **kwargs: Any) -> None:
                raise AssertionError("status must not execute compaction")

            monkeypatch.setattr(engine, "compact_superseded_record", fail)
            retention = (await engine.status(_scope(adapter)))["retention"]
            assert retention["automatic_housekeeping"] is False
            assert retention["compaction_ready"] == 1
        finally:
            await adapter.close()

    asyncio.run(run())


def test_capacity_observation_failure_is_unknown_and_fail_closed(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            def unavailable(_path: str):
                raise OSError("filesystem capacity unavailable")

            monkeypatch.setattr(shutil, "disk_usage", unavailable)
            capacity = (await engine.status(_scope(adapter)))["capacity"]
            assert capacity["observation"] == "UNKNOWN"
            assert capacity["filesystem_free_bytes"] is None
            assert capacity["mutation_preflight"] == "BLOCKED_UNKNOWN_CAPACITY"
            assert capacity["next_action"] == "RESTORE_CAPACITY_OBSERVABILITY_BEFORE_MEMORY_MUTATION"
            assert capacity["database_file_bytes"] == _size(engine.db_path)
        finally:
            await adapter.close()

    asyncio.run(run())


def test_file_size_oserror_is_unknown_and_blocks_capacity_preflight(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter = await _open_adapter(tmp_path)
        try:
            original_getsize = os.path.getsize

            def unavailable_database_file(path: str) -> int:
                if os.fspath(path) == engine.db_path:
                    raise PermissionError("database file stat unavailable")
                return original_getsize(path)

            monkeypatch.setattr(os.path, "getsize", unavailable_database_file)
            capacity = (await engine.status(_scope(adapter)))["capacity"]
            assert capacity["observation"] == "UNKNOWN"
            assert capacity["database_file_bytes"] is None
            assert isinstance(capacity["filesystem_free_bytes"], int)
            assert capacity["mutation_preflight"] == "BLOCKED_UNKNOWN_CAPACITY"
            assert capacity["next_action"] == "RESTORE_CAPACITY_OBSERVABILITY_BEFORE_MEMORY_MUTATION"
            assert capacity["pressure_threshold_bytes"] is None
        finally:
            await adapter.close()

    asyncio.run(run())
