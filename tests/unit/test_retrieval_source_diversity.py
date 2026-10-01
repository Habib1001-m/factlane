from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from factlane.adapter import MemoryAdapter, trusted_write_context_for_profile
from factlane.contract import PUBLIC_CONTRACT_REVISION, PUBLIC_TOOL_NAMES
from factlane.public_contract import request_schema


class _Provider:
    document_calls = 0

    def __init__(self) -> None:
        self.query_calls = 0

    def embed_query(self, text: str) -> list[float]:
        assert text
        self.query_calls += 1
        return [1.0, 0.0]


class _Engine:
    def __init__(
        self,
        *,
        vector_rows: list[tuple[dict[str, Any], float]] | None = None,
        keyword_rows: list[dict[str, Any]] | None = None,
        exact_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.vector_rows = vector_rows or []
        self.keyword_rows = keyword_rows or []
        self.exact_rows = exact_rows or []

    async def vector_candidates(
        self,
        vector: list[float],
        scope: Any,
        *,
        limit: int,
        history: bool,
    ) -> list[tuple[dict[str, Any], float]]:
        del vector, scope, history
        return self.vector_rows[:limit]

    async def keyword_candidates(
        self,
        query_text: str,
        scope: Any,
        *,
        limit: int,
        history: bool,
        exact: bool = False,
    ) -> list[dict[str, Any]]:
        del query_text, scope, history
        rows = self.exact_rows if exact else self.keyword_rows
        return rows[:limit]

    async def contradiction_summary(self, scope: Any) -> list[dict[str, Any]]:
        del scope
        return []

    async def has_unmaterialized_history(self, scope: Any) -> bool:
        del scope
        return False


def _source_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _row(
    record_id: str,
    source_ref: str,
    *,
    source_hash: str | None = None,
    lifecycle_state: str = "VALIDATED_CURRENT",
) -> dict[str, Any]:
    provenance = {
        "source_class": "TEST",
        "source_ref": source_ref,
        "source_hash": source_hash or _source_hash(source_ref),
        "review_ref": "source-diversity-test",
        "extraction_method": "synthetic",
    }
    return {
        "record_id": record_id,
        "memory_id": f"memory-{record_id}",
        "revision": 1,
        "parent_record_id": None,
        "scope": "CROSS_PROJECT_WORKFLOW",
        "project_id": None,
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "memory_type": "WORKFLOW_RULE",
        "fact": f"Synthetic fact {record_id}",
        "source_provenance": json.dumps(provenance, sort_keys=True),
        "source_timestamp": "2026-09-01T00:00:00Z",
        "created_at": "2026-09-01T00:00:00Z",
        "last_verified_at": "2026-09-01T00:00:00Z",
        "verified_by": "OWNER",
        "authority_role": "WORKFLOW_CURRENT",
        "contribution_origin": json.dumps({"contributor_class": "OWNER", "contributor_ref": "owner"}),
        "freshness_policy": json.dumps({"kind": "never"}),
        "supersedes": json.dumps([]),
        "contradiction_key": f"key-{record_id}",
        "contradiction_state": "NONE",
        "confidence": 1.0,
        "tags": json.dumps([]),
        "lifecycle_state": lifecycle_state,
        "native_content_hash": _source_hash(f"content-{record_id}"),
        "payload_fingerprint": _source_hash(f"payload-{record_id}"),
        "idempotency_key": f"idempotency-{record_id}",
        "embedding_profile_id": "synthetic",
        "embedding_model_digest": _source_hash("synthetic-model"),
        "embedding_output_dimension": 2,
    }


def _adapter(engine: _Engine) -> MemoryAdapter:
    return MemoryAdapter(
        engine,  # type: ignore[arg-type]
        _Provider(),  # type: ignore[arg-type]
        trusted_write_context=trusted_write_context_for_profile("read-only"),
    )


def _search(
    adapter: MemoryAdapter,
    *,
    retrieval_mode: str = "CURRENT",
    retrieval_mode_kind: str = "SEMANTIC",
    max_memories: int = 3,
    max_bytes: int = 6000,
) -> dict[str, Any]:
    return asyncio.run(
        adapter.search(
            query="targeted synthetic query",
            intent_class="WORKFLOW_RULE",
            scope="CROSS_PROJECT_WORKFLOW",
            retrieval_mode=retrieval_mode,
            retrieval_mode_kind=retrieval_mode_kind,
            top_k=5,
            max_memories=max_memories,
            max_bytes=max_bytes,
        )
    )


def _crowded_vector_rows() -> list[tuple[dict[str, Any], float]]:
    return [
        (_row("a1", "source-a"), 0.10),
        (_row("a2", "source-a"), 0.12),
        (_row("a3", "source-a"), 0.14),
        (_row("b1", "source-b"), 0.40),
        (_row("c1", "source-c"), 0.60),
    ]


def test_current_semantic_selection_represents_distinct_sources_before_repeats() -> None:
    response = _search(_adapter(_Engine(vector_rows=_crowded_vector_rows())), max_memories=3)

    assert [item["record_id"] for item in response["results"]] == ["a1", "b1", "c1"]


def test_source_diversity_preserves_top1_and_original_scores_and_order() -> None:
    response = _search(_adapter(_Engine(vector_rows=_crowded_vector_rows())), max_memories=3)

    assert response["results"][0]["record_id"] == "a1"
    assert [item["relevance_score"] for item in response["results"]] == [0.95, 0.8, 0.7]
    assert [item["retrieval_rank"] for item in response["results"]] == [1, 2, 3]


def test_source_identity_uses_exact_stored_provenance_not_source_ref_alone() -> None:
    shared_ref = "same-ref"
    rows = [
        (_row("a1", shared_ref, source_hash=_source_hash("source-a")), 0.10),
        (_row("a2", shared_ref, source_hash=_source_hash("source-a")), 0.12),
        (_row("b1", shared_ref, source_hash=_source_hash("source-b")), 0.30),
    ]

    response = _search(_adapter(_Engine(vector_rows=rows)), max_memories=2)

    assert [item["record_id"] for item in response["results"]] == ["a1", "b1"]


def test_single_source_falls_back_to_existing_ranked_prefix() -> None:
    rows = [
        (_row("a1", "source-a"), 0.10),
        (_row("a2", "source-a"), 0.12),
        (_row("a3", "source-a"), 0.14),
    ]

    response = _search(_adapter(_Engine(vector_rows=rows)), max_memories=3)

    assert [item["record_id"] for item in response["results"]] == ["a1", "a2", "a3"]


def test_insufficient_distinct_sources_fill_remaining_slots_in_original_order() -> None:
    rows = [
        (_row("a1", "source-a"), 0.10),
        (_row("a2", "source-a"), 0.12),
        (_row("a3", "source-a"), 0.14),
        (_row("b1", "source-b"), 0.40),
    ]

    response = _search(_adapter(_Engine(vector_rows=rows)), max_memories=3)

    assert [item["record_id"] for item in response["results"]] == ["a1", "a2", "b1"]


def test_current_hybrid_uses_same_source_diversity_selection() -> None:
    rows = _crowded_vector_rows()
    response = _search(
        _adapter(_Engine(vector_rows=rows, keyword_rows=[row for row, _ in rows])),
        retrieval_mode_kind="HYBRID",
        max_memories=3,
    )

    assert [item["record_id"] for item in response["results"]] == ["a1", "b1", "c1"]


def test_diversity_deliberately_promotes_unseen_source_over_higher_scored_repeat() -> None:
    rows = [
        (_row("a1", "source-a"), 0.02),
        (_row("a2", "source-a"), 0.04),
        (_row("b1", "source-b"), 1.80),
    ]

    response = _search(_adapter(_Engine(vector_rows=rows)), max_memories=2)

    assert [item["record_id"] for item in response["results"]] == ["a1", "b1"]
    assert [item["relevance_score"] for item in response["results"]] == [0.99, 0.1]


def test_equal_score_selection_preserves_existing_record_id_tie_order() -> None:
    rows = [
        (_row("c1", "source-c"), 0.20),
        (_row("a1", "source-a"), 0.20),
        (_row("b1", "source-b"), 0.20),
    ]

    response = _search(_adapter(_Engine(vector_rows=rows)), max_memories=3)

    assert [item["record_id"] for item in response["results"]] == ["a1", "b1", "c1"]


def test_existing_budget_trimming_applies_after_diverse_selection() -> None:
    unrestricted = _search(_adapter(_Engine(vector_rows=_crowded_vector_rows())), max_memories=3)
    first_result_bytes = len(json.dumps([unrestricted["results"][0]], sort_keys=True, separators=(",", ":")).encode("utf-8"))
    two_result_bytes = len(json.dumps(unrestricted["results"][:2], sort_keys=True, separators=(",", ":")).encode("utf-8"))
    assert first_result_bytes < two_result_bytes

    response = _search(
        _adapter(_Engine(vector_rows=_crowded_vector_rows())),
        max_memories=3,
        max_bytes=two_result_bytes - 1,
    )

    assert response["budget"]["truncated"] is True
    assert [item["record_id"] for item in response["results"]] == ["a1"]


def test_exact_retrieval_is_unchanged() -> None:
    rows = [_row("a1", "source-a"), _row("a2", "source-a"), _row("b1", "source-b")]
    response = _search(
        _adapter(_Engine(exact_rows=rows)),
        retrieval_mode_kind="EXACT",
        max_memories=2,
    )

    assert [item["record_id"] for item in response["results"]] == ["a1", "a2"]


def test_review_history_semantic_retrieval_is_unchanged() -> None:
    rows = [
        (_row("h1", "source-a", lifecycle_state="HISTORICAL"), 0.10),
        (_row("h2", "source-a", lifecycle_state="HISTORICAL"), 0.12),
        (_row("h3", "source-b", lifecycle_state="HISTORICAL"), 0.30),
    ]
    response = _search(
        _adapter(_Engine(vector_rows=rows)),
        retrieval_mode="REVIEW_HISTORY",
        retrieval_mode_kind="SEMANTIC",
        max_memories=2,
    )

    assert [item["record_id"] for item in response["results"]] == ["h1", "h2"]


def test_keyword_only_retrieval_is_unchanged() -> None:
    rows = [_row("a1", "source-a"), _row("a2", "source-a"), _row("b1", "source-b")]
    response = _search(
        _adapter(_Engine(keyword_rows=rows)),
        retrieval_mode_kind="KEYWORD",
        max_memories=2,
    )

    assert [item["record_id"] for item in response["results"]] == ["a1", "a2"]


def test_no_public_diversity_knob_tool_or_contract_revision_change() -> None:
    search_schema = request_schema("memory_search")
    schema_text = json.dumps(search_schema, sort_keys=True)

    assert "divers" not in schema_text.casefold()
    assert tuple(PUBLIC_TOOL_NAMES) == (
        "memory_search",
        "memory_get",
        "memory_store",
        "memory_update",
        "memory_status",
    )
    assert PUBLIC_CONTRACT_REVISION == 2
