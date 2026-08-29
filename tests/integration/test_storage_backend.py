from __future__ import annotations

import asyncio
import sqlite3

from factlane.embeddings import EmbeddingProfile
from factlane.storage import SQLiteVecEngine


def profile(dimension: int = 256) -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id=f"test-{dimension}",
        provider_kind="OLLAMA_LOCAL",
        base_model_identity="nomic-embed-text:latest",
        model_digest="0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
        source_dimension=768,
        output_dimension=dimension,
        normalization_policy="OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
        distance_metric="cosine",
        projection_version="ollama-dimensions-v1",
        document_prefix="search_document: ",
        query_prefix="search_query: ",
    )


def test_pinned_backend_exposes_required_sqlite_primitives(tmp_path) -> None:
    from mcp_memory_service.storage.sqlite_vec import SqliteVecMemoryStorage

    storage = SqliteVecMemoryStorage(str(tmp_path / "memory.db"))
    assert hasattr(storage, "_conn_lock")
    assert callable(storage._execute_with_retry)


class FakeStorage:
    def __init__(self) -> None:
        self.calls = 0

    async def _execute_with_retry(self, operation):
        self.calls += 1
        return operation()


def test_engine_delegates_db_execution_to_backend_retry() -> None:
    engine = SQLiteVecEngine("/tmp/factlane-delegation-test.sqlite", profile())
    engine.conn = sqlite3.connect(":memory:")
    engine.storage = FakeStorage()
    try:
        result = asyncio.run(engine._run(lambda value: value + 1, 41))
    finally:
        engine.conn.close()
    assert result == 42
    assert engine.storage.calls == 1


def test_open_reuses_backend_wal_and_busy_timeout(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "memory.db"), profile())
        await engine.open()
        try:
            assert engine.conn is not None
            assert engine.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert engine.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
            assert engine._read_dimension() == 256
            tables = {
                row[0]
                for row in engine.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "adapter_records" in tables
            assert "adapter_meta" in tables
        finally:
            await engine.close()

    asyncio.run(run())
