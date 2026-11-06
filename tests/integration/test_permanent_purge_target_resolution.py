from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter, _establish_embedding_compatibility, trusted_write_context_for_profile
from factlane.contract import AdapterError, ScopeContext
from factlane.embeddings import EmbeddingProfile
from factlane.recovery import PM0, PM1
from factlane.storage import SQLiteVecEngine


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="purge-target-resolution-256",
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


class _Provider:
    def __init__(self, profile: EmbeddingProfile) -> None:
        self.profile = profile
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        return [[0.0] * (self.profile.output_dimension - 1) + [1.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        del text
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


async def _open_adapter(tmp_path: Path) -> tuple[SQLiteVecEngine, MemoryAdapter, _Provider]:
    profile = _profile()
    engine = SQLiteVecEngine(str(tmp_path / "memory.db"), profile)
    await engine.open()
    provider = _Provider(profile)
    await _establish_embedding_compatibility(engine, provider, _provider_status(profile))
    adapter = MemoryAdapter(
        engine,
        provider,  # type: ignore[arg-type]
        trusted_write_context=trusted_write_context_for_profile(
            "owner-current",
            contributor_ref="purge-target-resolution-tests",
        ),
    )
    return engine, adapter, provider


async def _store(adapter: MemoryAdapter, index: int, *, project_id: str = "project-a") -> dict[str, Any]:
    stamp = "2026-11-05T20:00:00Z"
    result = await adapter.store(
        fact=f"Permanent purge resolution fixture {index}.",
        scope="PROJECT",
        project_id=project_id,
        memory_type="PROJECT_LEARNED_FACT",
        source_provenance={
            "source_class": "TEST",
            "source_ref": f"purge-target:{index}",
            "source_hash": f"{index % 10}" * 64,
            "review_ref": "purge-target-resolution-tests",
            "extraction_method": "fixture",
        },
        freshness_policy={"kind": "manual"},
        idempotency_key=f"purge-target-resolution-{index}",
        source_timestamp=stamp,
        last_verified_at=stamp,
        verified_by="OWNER",
        requested_lifecycle_state="VALIDATED_CURRENT",
        confidence=0.9,
        tags=[f"subject:purge-target-{index}"],
    )
    return result["results"][0]


def _identity(
    row: dict[str, Any],
    *,
    scope: ScopeContext | None = None,
    revision: int | None = None,
    record_id: str | None = None,
    memory_id: str | None = None,
) -> dict[str, Any]:
    return {
        "scope": scope or ScopeContext(
            row["scope"]["scope"] if isinstance(row["scope"], dict) else row["scope"],
            project_id=(row["scope"].get("project_id") if isinstance(row["scope"], dict) else row.get("project_id")),
            worktree_id=(row["scope"].get("worktree_id") if isinstance(row["scope"], dict) else row.get("worktree_id")),
            workflow_id=(row["scope"].get("workflow_id") if isinstance(row["scope"], dict) else row.get("workflow_id")),
            agent_id=(row["scope"].get("agent_id") if isinstance(row["scope"], dict) else row.get("agent_id")),
        ),
        "memory_id": memory_id or row["memory_id"],
        "record_id": record_id or row["record_id"],
        "revision": row["revision"] if revision is None else revision,
    }


def test_resolve_permanent_purge_targets_preserves_order_and_all_lifecycles_without_side_effects(tmp_path: Path) -> None:
    async def run() -> None:
        engine, adapter, provider = await _open_adapter(tmp_path)
        lifecycles = (
            "CANDIDATE",
            "VALIDATED_CURRENT",
            "SUPERSEDED",
            "STALE",
            "QUARANTINED",
            "HISTORICAL",
            "REVOKED",
            "REJECTED",
        )
        try:
            rows: list[dict[str, Any]] = []
            assert engine.conn is not None
            for index, lifecycle in enumerate(lifecycles, start=1):
                public = await _store(adapter, index)
                engine.conn.execute(
                    "UPDATE adapter_records SET lifecycle_state=? WHERE record_id=?",
                    (lifecycle, public["record_id"]),
                )
                engine.conn.commit()
                stored = (
                    await engine.get_record(
                        public["memory_id"],
                        ScopeContext("PROJECT", project_id="project-a"),
                        history=True,
                    )
                )[0]
                rows.append(stored)
            before_provider = (provider.document_calls, provider.query_calls)
            before_changes = engine.conn.total_changes
            requested = [_identity(row) for row in reversed(rows)]

            resolved = await adapter.resolve_permanent_purge_targets(requested)

            assert [item["record_id"] for item in resolved] == [row["record_id"] for row in reversed(rows)]
            assert [item["lifecycle_state"] for item in resolved] == list(reversed(lifecycles))
            assert all(item["materialization_class"] == PM1 for item in resolved)
            assert all(item["native_materialized"] is True for item in resolved)
            for item, row in zip(resolved, reversed(rows), strict=True):
                assert item["native_content_hash"] == row["native_content_hash"]
                assert item["memory_id"] == row["memory_id"]
                assert item["revision"] == row["revision"]
                assert item["scope"] == "PROJECT"
                assert item["project_id"] == "project-a"
                assert item["worktree_id"] is None
                assert item["workflow_id"] is None
                assert item["agent_id"] is None
            assert (provider.document_calls, provider.query_calls) == before_provider
            assert engine.conn.total_changes == before_changes
        finally:
            await engine.close()

    asyncio.run(run())


def test_resolve_permanent_purge_target_classifies_compacted_historical_as_pm0(tmp_path: Path) -> None:
    async def run() -> None:
        engine, adapter, provider = await _open_adapter(tmp_path)
        try:
            public = await _store(adapter, 20)
            assert engine.conn is not None
            row = (
                await engine.get_record(
                    public["memory_id"], ScopeContext("PROJECT", project_id="project-a"), history=True
                )
            )[0]
            native_id = engine.conn.execute(
                "SELECT id FROM memories WHERE content_hash=?",
                (row["native_content_hash"],),
            ).fetchone()[0]
            engine.conn.execute(
                "UPDATE adapter_records SET lifecycle_state='HISTORICAL' WHERE record_id=?",
                (row["record_id"],),
            )
            engine.conn.execute("DELETE FROM memory_embeddings WHERE rowid=?", (native_id,))
            engine.conn.execute("DELETE FROM memories WHERE id=?", (native_id,))
            engine.conn.commit()
            before_provider = (provider.document_calls, provider.query_calls)
            before_changes = engine.conn.total_changes

            resolved = await adapter.resolve_permanent_purge_targets([_identity(row)])

            assert resolved == [
                {
                    "scope": "PROJECT",
                    "project_id": "project-a",
                    "worktree_id": None,
                    "workflow_id": None,
                    "agent_id": None,
                    "memory_id": row["memory_id"],
                    "record_id": row["record_id"],
                    "revision": row["revision"],
                    "lifecycle_state": "HISTORICAL",
                    "materialization_class": PM0,
                    "native_materialized": False,
                    "native_content_hash": row["native_content_hash"],
                }
            ]
            assert (provider.document_calls, provider.query_calls) == before_provider
            assert engine.conn.total_changes == before_changes
        finally:
            await engine.close()

    asyncio.run(run())


@pytest.mark.parametrize("partial_kind", ["missing_vector", "orphan_vector", "missing_native_nonhistorical"])
def test_resolve_permanent_purge_target_rejects_unsafe_materialization(tmp_path: Path, partial_kind: str) -> None:
    async def run() -> None:
        engine, adapter, provider = await _open_adapter(tmp_path)
        try:
            public = await _store(adapter, 30)
            scope = ScopeContext("PROJECT", project_id="project-a")
            row = (await engine.get_record(public["memory_id"], scope, history=True))[0]
            assert engine.conn is not None
            native_id = engine.conn.execute(
                "SELECT id FROM memories WHERE content_hash=?",
                (row["native_content_hash"],),
            ).fetchone()[0]
            if partial_kind == "missing_vector":
                engine.conn.execute("DELETE FROM memory_embeddings WHERE rowid=?", (native_id,))
            elif partial_kind == "orphan_vector":
                engine.conn.execute("DELETE FROM memories WHERE id=?", (native_id,))
            else:
                engine.conn.execute("DELETE FROM memory_embeddings WHERE rowid=?", (native_id,))
                engine.conn.execute("DELETE FROM memories WHERE id=?", (native_id,))
            engine.conn.commit()
            before_provider = (provider.document_calls, provider.query_calls)
            before_changes = engine.conn.total_changes

            with pytest.raises(AdapterError) as caught:
                await adapter.resolve_permanent_purge_targets([_identity(row)])
            assert caught.value.code == "PURGE_TARGET_MATERIALIZATION_UNSUPPORTED"
            assert (provider.document_calls, provider.query_calls) == before_provider
            assert engine.conn.total_changes == before_changes
        finally:
            await engine.close()

    asyncio.run(run())


def test_resolve_permanent_purge_targets_fail_closed_on_scope_revision_missing_duplicate_and_bound_drift(tmp_path: Path) -> None:
    async def run() -> None:
        engine, adapter, provider = await _open_adapter(tmp_path)
        try:
            public = await _store(adapter, 40)
            scope = ScopeContext("PROJECT", project_id="project-a")
            row = (await engine.get_record(public["memory_id"], scope, history=True))[0]
            before_provider = (provider.document_calls, provider.query_calls)
            assert engine.conn is not None
            before_changes = engine.conn.total_changes

            for drifted in (
                _identity(row, scope=ScopeContext("PROJECT", project_id="project-b")),
                _identity(
                    row,
                    scope=ScopeContext("PROJECT", project_id="project-a", worktree_id="other-worktree"),
                ),
                _identity(row, memory_id="different-memory"),
                _identity(row, revision=row["revision"] + 1),
                _identity(row, record_id="missing-record"),
            ):
                with pytest.raises(AdapterError) as caught:
                    await adapter.resolve_permanent_purge_targets([drifted])
                assert caught.value.code == "PURGE_TARGET_DRIFT"

            exact = _identity(row)
            with pytest.raises(AdapterError) as duplicate:
                await adapter.resolve_permanent_purge_targets([exact, exact])
            assert duplicate.value.code == "PURGE_TARGET_INVALID"

            oversized = [
                {
                    "scope": scope,
                    "memory_id": f"memory-{index}",
                    "record_id": f"record-{index}",
                    "revision": 1,
                }
                for index in range(51)
            ]
            with pytest.raises(AdapterError) as too_many:
                await adapter.resolve_permanent_purge_targets(oversized)
            assert too_many.value.code == "PURGE_TARGET_INVALID"

            assert (provider.document_calls, provider.query_calls) == before_provider
            assert engine.conn.total_changes == before_changes
        finally:
            await engine.close()

    asyncio.run(run())
