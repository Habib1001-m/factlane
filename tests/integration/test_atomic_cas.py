from __future__ import annotations

import asyncio
from typing import Any

from factlane.adapter import MemoryAdapter
from factlane.contract import AdapterError
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


class FakeProvider:
    def __init__(self, embedding_profile: EmbeddingProfile) -> None:
        self.profile = embedding_profile
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        vector = [0.0] * (self.profile.output_dimension - 1) + [1.0]
        return [list(vector) for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return [0.0] * (self.profile.output_dimension - 1) + [1.0]


async def _race_two_reverify_updates(tmp_path) -> None:
    db_path = str(tmp_path / "memory.db")
    embedding_profile = profile()
    engine_a = SQLiteVecEngine(db_path, embedding_profile)
    engine_b = SQLiteVecEngine(db_path, embedding_profile)
    await engine_a.open()
    await engine_b.open()
    adapter_a = MemoryAdapter(engine_a, FakeProvider(embedding_profile))  # type: ignore[arg-type]
    adapter_b = MemoryAdapter(engine_b, FakeProvider(embedding_profile))  # type: ignore[arg-type]

    try:
        stamp = "2026-08-30T00:00:00Z"
        seed = await adapter_a.store(
            fact="FactLane uses atomic compare-and-swap for current memory revisions.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            source_provenance={
                "source_class": "CURRENT_REPO",
                "source_ref": "s6b4c03-seed",
                "source_hash": "a" * 64,
                "review_ref": "s6b4c03",
                "extraction_method": "AUTOMATED_CHECK",
            },
            freshness_policy={"kind": "manual"},
            idempotency_key="s6b4c03-seed",
            project_id="factlane",
            source_timestamp=stamp,
            last_verified_at=stamp,
            verified_by="OWNER",
            requested_lifecycle_state="VALIDATED_CURRENT",
            confidence=0.95,
            tags=["subject:atomic-cas", "s6b4c03"],
        )
        memory_id = seed["results"][0]["memory_id"]

        original_write_a = engine_a.write_record
        original_write_b = engine_b.write_record
        both_ready = asyncio.Event()
        ready_lock = asyncio.Lock()
        ready_count = 0

        def gated(original):
            async def write(*args: Any, **kwargs: Any) -> None:
                nonlocal ready_count
                async with ready_lock:
                    ready_count += 1
                    if ready_count == 2:
                        both_ready.set()
                await asyncio.wait_for(both_ready.wait(), timeout=5)
                await original(*args, **kwargs)

            return write

        engine_a.write_record = gated(original_write_a)  # type: ignore[method-assign]
        engine_b.write_record = gated(original_write_b)  # type: ignore[method-assign]

        async def update(adapter: MemoryAdapter, key: str) -> dict[str, Any]:
            return await adapter.update(
                memory_id=memory_id,
                scope="PROJECT",
                project_id="factlane",
                expected_revision=1,
                mode="REVERIFY",
                idempotency_key=key,
                verification={
                    "source_provenance": {
                        "source_class": "CURRENT_REPO",
                        "source_ref": key,
                        "source_hash": ("b" if key.endswith("a") else "c") * 64,
                        "review_ref": "s6b4c03",
                        "extraction_method": "AUTOMATED_CHECK",
                    },
                    "source_timestamp": stamp,
                    "verified_by": "AUTOMATED_CHECK",
                },
            )

        outcomes = await asyncio.gather(
            update(adapter_a, "s6b4c03-update-a"),
            update(adapter_b, "s6b4c03-update-b"),
            return_exceptions=True,
        )

        successes = [outcome for outcome in outcomes if isinstance(outcome, dict)]
        conflicts = [
            outcome
            for outcome in outcomes
            if isinstance(outcome, AdapterError) and outcome.code == "VERSION_CONFLICT"
        ]
        assert len(successes) == 1
        assert len(conflicts) == 1

        current = await engine_a.get_record(memory_id, seed["results"][0]["scope"] and adapter_a._safe_scope("PROJECT", "factlane", None, None, None), history=False)
        history = await engine_a.get_record(memory_id, adapter_a._safe_scope("PROJECT", "factlane", None, None, None), history=True)
        assert len(current) == 1
        assert current[0]["revision"] == 2
        assert len(history) == 2
        assert sorted(row["revision"] for row in history) == [1, 2]
    finally:
        await adapter_a.close()
        await adapter_b.close()


def test_two_independent_clients_have_single_winner_for_same_revision(tmp_path) -> None:
    asyncio.run(_race_two_reverify_updates(tmp_path))
