from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from factlane.adapter import MemoryAdapter
from factlane.embeddings import EmbeddingProfile

PROFILE = EmbeddingProfile(
    profile_id="s6b4c05-test-256",
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


class ProbeProvider:
    def __init__(
        self,
        *,
        query_barrier: threading.Barrier | None = None,
        query_delay: float = 0.0,
        document_delay: float = 0.0,
        status_delay: float = 0.0,
    ) -> None:
        self.profile = PROFILE
        self.document_calls = 0
        self.query_calls = 0
        self.query_barrier = query_barrier
        self.query_delay = query_delay
        self.document_delay = document_delay
        self.status_delay = status_delay
        self._lock = threading.Lock()
        self._active = {"query": 0, "document": 0, "status": 0}
        self.max_active = {"query": 0, "document": 0, "status": 0}
        self.thread_ids = {"query": set(), "document": set(), "status": set()}
        self.query_barrier_broken = False

    def _enter(self, kind: str) -> None:
        with self._lock:
            self._active[kind] += 1
            self.max_active[kind] = max(self.max_active[kind], self._active[kind])
            self.thread_ids[kind].add(threading.get_ident())

    def _leave(self, kind: str) -> None:
        with self._lock:
            self._active[kind] -= 1

    def is_active(self, kind: str) -> bool:
        with self._lock:
            return self._active[kind] > 0

    @staticmethod
    def _vector() -> list[float]:
        return [0.0] * (PROFILE.output_dimension - 1) + [1.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._enter("document")
        try:
            self.document_calls += len(texts)
            if self.document_delay:
                time.sleep(self.document_delay)
            return [self._vector() for _ in texts]
        finally:
            self._leave("document")

    def embed_query(self, text: str) -> list[float]:
        self._enter("query")
        try:
            self.query_calls += 1
            if self.query_barrier is not None:
                try:
                    self.query_barrier.wait()
                except threading.BrokenBarrierError:
                    self.query_barrier_broken = True
            if self.query_delay:
                time.sleep(self.query_delay)
            return self._vector()
        finally:
            self._leave("query")

    def provider_status(self) -> dict[str, object]:
        self._enter("status")
        try:
            if self.status_delay:
                time.sleep(self.status_delay)
            return {"available": True, "profile_id": self.profile.profile_id}
        finally:
            self._leave("status")


class QueryEngine:
    async def vector_candidates(
        self,
        vector: list[float],
        scope: Any,
        *,
        limit: int,
        history: bool,
    ) -> list[tuple[dict[str, Any], float]]:
        return []

    async def contradiction_summary(self, scope: Any) -> list[dict[str, Any]]:
        return []

    async def status(self, scope: Any) -> dict[str, object]:
        return {"available": True}


class MemoryStoreEngine(QueryEngine):
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def find_idempotency(self, key: str) -> dict[str, Any] | None:
        return next((row for row in self.rows if row["idempotency_key"] == key), None)

    async def find_exact(self, native_content_hash: str) -> dict[str, Any] | None:
        return None

    async def find_contradictions(self, key: str, scope: Any) -> list[dict[str, Any]]:
        return []

    async def write_record(
        self,
        record: dict[str, Any],
        embedding: list[float],
        supersede_record_id: str | None = None,
    ) -> None:
        del embedding, supersede_record_id
        self.rows.append(
            {
                **record,
                "source_provenance": json.dumps(record["source_provenance"]),
                "freshness_policy": json.dumps(record["freshness_policy"]),
                "supersedes": json.dumps(record["supersedes"]),
                "tags": json.dumps(record["tags"]),
            }
        )

    async def get_record(self, memory_id: str, scope: Any, history: bool = False) -> list[dict[str, Any]]:
        del scope, history
        return [row for row in self.rows if row["memory_id"] == memory_id]


def _semantic_search(adapter: MemoryAdapter, query: str) -> Any:
    return adapter.search(
        query=query,
        intent_class="PROJECT_DESIGN_RATIONALE",
        scope="PROJECT",
        project_id="factlane",
        retrieval_mode_kind="SEMANTIC",
    )


async def _heartbeat_until(task: asyncio.Task[Any], provider: ProbeProvider, kind: str) -> int:
    active_ticks = 0
    while not task.done():
        if provider.is_active(kind):
            active_ticks += 1
        await asyncio.sleep(0.005)
    return active_ticks


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def test_concurrent_semantic_queries_overlap_off_event_loop() -> None:
    async def run() -> tuple[ProbeProvider, int]:
        provider = ProbeProvider(query_barrier=threading.Barrier(2, timeout=0.2))
        adapter = MemoryAdapter(QueryEngine(), provider)  # type: ignore[arg-type]
        loop_thread_id = threading.get_ident()
        await asyncio.gather(
            _semantic_search(adapter, "first query"),
            _semantic_search(adapter, "second query"),
        )
        return provider, loop_thread_id

    provider, loop_thread_id = _run(run())

    assert not provider.query_barrier_broken, (
        "CONCURRENT_QUERY_EMBEDDING_OVERLAP=NO: "
        "the bounded query barrier broke before a peer call arrived"
    )
    assert provider.max_active["query"] >= 2, "CONCURRENT_QUERY_EMBEDDING_OVERLAP=NO"
    assert not provider.thread_ids["query"].intersection({loop_thread_id}), (
        "EVENT_LOOP_BLOCKED_BY_QUERY_EMBEDDING=YES"
    )


def test_event_loop_progresses_while_query_embedding_is_pending() -> None:
    async def run() -> tuple[ProbeProvider, int, int]:
        provider = ProbeProvider(query_delay=0.15)
        adapter = MemoryAdapter(QueryEngine(), provider)  # type: ignore[arg-type]
        loop_thread_id = threading.get_ident()
        search_task = asyncio.create_task(_semantic_search(adapter, "slow query"))
        await asyncio.sleep(0)
        active_ticks = await _heartbeat_until(search_task, provider, "query")
        await search_task
        return provider, active_ticks, loop_thread_id

    provider, active_ticks, loop_thread_id = _run(run())

    assert active_ticks > 0, "EVENT_LOOP_PROGRESS_DURING_EMBEDDING=NO"
    assert not provider.thread_ids["query"].intersection({loop_thread_id}), (
        "EVENT_LOOP_BLOCKED_BY_QUERY_EMBEDDING=YES"
    )


def test_event_loop_progresses_while_document_embedding_is_pending() -> None:
    async def run() -> tuple[ProbeProvider, int, int]:
        provider = ProbeProvider(document_delay=0.15)
        adapter = MemoryAdapter(MemoryStoreEngine(), provider)  # type: ignore[arg-type]
        loop_thread_id = threading.get_ident()
        store_task = asyncio.create_task(
            adapter.store(
                fact="A document embedding remains local and bounded.",
                scope="PROJECT",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance={
                    "source_class": "CURRENT_REPO",
                    "source_ref": "s6b4c05-document",
                    "source_hash": "a" * 64,
                    "review_ref": "s6b4c05",
                    "extraction_method": "AUTOMATED_CHECK",
                },
                freshness_policy={"kind": "manual"},
                idempotency_key="s6b4c05-document",
                project_id="factlane",
                source_timestamp="2026-08-30T00:00:00Z",
                last_verified_at="2026-08-30T00:00:00Z",
                verified_by="OWNER",
                requested_lifecycle_state="VALIDATED_CURRENT",
                confidence=0.95,
                tags=["subject:async-embedding", "s6b4c05"],
            )
        )
        await asyncio.sleep(0)
        active_ticks = await _heartbeat_until(store_task, provider, "document")
        await store_task
        return provider, active_ticks, loop_thread_id

    provider, active_ticks, loop_thread_id = _run(run())

    assert active_ticks > 0, "EVENT_LOOP_BLOCKED_BY_DOCUMENT_EMBEDDING=YES"
    assert not provider.thread_ids["document"].intersection({loop_thread_id}), (
        "EVENT_LOOP_BLOCKED_BY_DOCUMENT_EMBEDDING=YES"
    )


def test_event_loop_progresses_while_provider_status_is_pending() -> None:
    async def run() -> tuple[ProbeProvider, int, int]:
        provider = ProbeProvider(status_delay=0.15)
        adapter = MemoryAdapter(QueryEngine(), provider)  # type: ignore[arg-type]
        loop_thread_id = threading.get_ident()
        status_task = asyncio.create_task(adapter.status(scope="PROJECT", project_id="factlane"))
        await asyncio.sleep(0)
        active_ticks = await _heartbeat_until(status_task, provider, "status")
        await status_task
        return provider, active_ticks, loop_thread_id

    provider, active_ticks, loop_thread_id = _run(run())

    assert active_ticks > 0, "EVENT_LOOP_BLOCKED_BY_PROVIDER_STATUS=YES"
    assert not provider.thread_ids["status"].intersection({loop_thread_id}), (
        "EVENT_LOOP_BLOCKED_BY_PROVIDER_STATUS=YES"
    )
