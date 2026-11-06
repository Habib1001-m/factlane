from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter, trusted_write_context_for_profile
from factlane.contract import AdapterError, validate_scope
from factlane.embeddings import EmbeddingProfile
from factlane.owner_control import OwnerControlService
from factlane.owner_overview import OwnerOverviewService
from factlane.storage import SQLiteVecEngine


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="owner-overview-test-256",
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
    def __init__(self, *, fail_query: bool = False) -> None:
        self.profile = _profile()
        self.fail_query = fail_query
        self.query_calls = 0
        self.document_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        return [[1.0] + [0.0] * 255 for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        if self.fail_query:
            raise AdapterError("EMBEDDING_UNAVAILABLE", "test provider unavailable")
        return [1.0] + [0.0] * 255

    def provider_status(self) -> dict[str, object]:
        return {"local_only": True}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record(
    *,
    marker: str,
    fact: str,
    scope: str = "PROJECT",
    project_id: str | None = "project-a",
    worktree_id: str | None = None,
    workflow_id: str | None = None,
    agent_id: str | None = None,
    lifecycle: str = "CANDIDATE",
    contradiction_key: str | None = None,
    created_at: str = "2026-11-05T20:00:00Z",
) -> dict[str, Any]:
    current = lifecycle == "VALIDATED_CURRENT"
    if scope == "CROSS_PROJECT_WORKFLOW":
        project_id = worktree_id = workflow_id = agent_id = None
        memory_type = "WORKFLOW_RULE"
        authority_role = "WORKFLOW_CURRENT" if current else "UNRESOLVED"
    elif scope == "WORKFLOW":
        memory_type = "WORKFLOW_RULE"
        authority_role = "WORKFLOW_CURRENT" if current else "UNRESOLVED"
    elif scope == "TOOL_ENVIRONMENT":
        memory_type = "TOOL_ENVIRONMENT_FACT"
        authority_role = "TOOL_ENV_CURRENT" if current else "UNRESOLVED"
    elif scope == "GLOBAL_USER":
        memory_type = "USER_FACT"
        authority_role = "OWNER_CURRENT" if current else "UNRESOLVED"
    else:
        memory_type = "PROJECT_LEARNED_FACT"
        authority_role = "PROJECT_CURRENT" if current else "UNRESOLVED"
    key = contradiction_key or f"key-{marker}"
    return {
        "record_id": str(uuid.uuid4()),
        "memory_id": str(uuid.uuid4()),
        "revision": 1,
        "parent_record_id": None,
        "scope": scope,
        "project_id": project_id,
        "worktree_id": worktree_id,
        "workflow_id": workflow_id,
        "agent_id": agent_id,
        "memory_type": memory_type,
        "fact": fact,
        "source_provenance": {
            "source_class": "TEST",
            "source_ref": f"source:{marker}",
            "source_hash": _sha(f"source:{marker}"),
            "review_ref": f"review:{marker}",
            "extraction_method": "test",
        },
        "source_timestamp": created_at if current else None,
        "created_at": created_at,
        "last_verified_at": created_at if current else None,
        "verified_by": "OWNER" if current else "UNVERIFIED",
        "authority_role": authority_role,
        "contribution_origin": {"contributor_class": "OWNER", "contributor_ref": "overview-test"},
        "freshness_policy": {"kind": "manual"},
        "supersedes": [],
        "contradiction_key": key,
        "contradiction_state": "NONE" if current else "UNRESOLVED",
        "confidence": 1.0 if current else 0.5,
        "tags": [f"subject:{key}"],
        "lifecycle_state": lifecycle,
        "native_content_hash": _sha(f"native:{marker}"),
        "payload_fingerprint": _sha(f"payload:{marker}"),
        "idempotency_key": f"overview-{marker}-{uuid.uuid4()}",
        "embedding_profile_id": _profile().profile_id,
        "embedding_model_digest": _profile().model_digest,
        "embedding_output_dimension": 256,
    }


async def _services(tmp_path, *, fail_query: bool = False):
    engine = SQLiteVecEngine(str(tmp_path / f"overview-{uuid.uuid4()}.db"), _profile())
    await engine.open()
    provider = _Provider(fail_query=fail_query)
    adapter = MemoryAdapter(
        engine,
        provider,
        trusted_write_context=trusted_write_context_for_profile(
            "owner-current",
            contributor_ref="overview-test",
        ),
    )
    owner = OwnerControlService(adapter)
    return engine, provider, owner, OwnerOverviewService(owner)


async def _write(engine: SQLiteVecEngine, row: dict[str, Any]) -> None:
    await engine.write_record(row, [1.0] + [0.0] * 255)


def test_overview_counts_database_and_exact_scope_breakdown_without_cross_project_fanout(tmp_path) -> None:
    async def run() -> None:
        engine, _, owner, overview = await _services(tmp_path)
        try:
            current = _record(
                marker="current-a",
                fact="Current project fact",
                lifecycle="VALIDATED_CURRENT",
                created_at="2026-11-05T20:00:01Z",
            )
            rows = [
                current,
                _record(
                    marker="candidate-a",
                    fact="Candidate in project A",
                    created_at="2026-11-05T20:00:02Z",
                ),
                _record(
                    marker="candidate-b",
                    fact="Candidate in project B",
                    project_id="project-b",
                    created_at="2026-11-05T20:00:03Z",
                ),
                _record(
                    marker="cross",
                    fact="Cross project workflow rule",
                    scope="CROSS_PROJECT_WORKFLOW",
                    created_at="2026-11-05T20:00:04Z",
                ),
            ]
            for row in rows:
                await _write(engine, row)

            scope = validate_scope("PROJECT", project_id="project-a")
            token = (await engine.group_observation(scope, current["contradiction_key"]))["public"][
                "observed_group_token"
            ]
            await owner.reverify(
                memory_id=current["memory_id"],
                expected_record_id=current["record_id"],
                expected_revision=1,
                observed_group_token=token,
                idempotency_key="overview-reverify-current-a",
                verification={"source_timestamp": "2026-11-05T20:00:01Z", "verified_by": "OWNER"},
                scope="PROJECT",
                project_id="project-a",
            )

            changes_before = engine.conn.total_changes if engine.conn is not None else -1
            first = await overview.overview(limit=2)
            changes_after = engine.conn.total_changes if engine.conn is not None else -1

            assert changes_after == changes_before
            assert first["totals"] == {
                "logical_memories": {"value": 4, "completeness": "EXACT", "reason": None},
                "revisions": {"value": 5, "completeness": "EXACT", "reason": None},
                "candidates": {"value": 3, "completeness": "EXACT", "reason": None},
                "conflicts": {"value": 3, "completeness": "EXACT", "reason": None},
            }
            assert first["scope_index"]["total"] == {
                "value": 3,
                "completeness": "EXACT",
                "reason": None,
            }
            assert first["scope_index"]["page_count"] == 2
            assert first["scope_index"]["next_cursor"] is not None

            second = await overview.overview(limit=2, cursor=first["scope_index"]["next_cursor"])
            all_scopes = first["scope_index"]["items"] + second["scope_index"]["items"]
            assert len(all_scopes) == 3
            assert len({tuple(item["scope"].items()) for item in all_scopes}) == 3

            cross = next(item for item in all_scopes if item["scope"]["scope"] == "CROSS_PROJECT_WORKFLOW")
            assert cross["scope"] == {
                "scope": "CROSS_PROJECT_WORKFLOW",
                "project_id": None,
                "worktree_id": None,
                "workflow_id": None,
                "agent_id": None,
            }
            assert cross["logical_memories"] == 1
            assert cross["revisions"] == 1
            assert cross["candidates"] == 1

            cross_search = await overview.search(
                query="project",
                search_mode="TEXT",
                scope="CROSS_PROJECT_WORKFLOW",
            )
            assert [item["memory_id"] for item in cross_search["items"]] == [rows[-1]["memory_id"]]
            assert cross_search["items"][0]["scope"] == cross["scope"]

            projects = {item["project_id"]: item for item in first["project_breakdown"]}
            assert projects["project-a"]["logical_memories"] == 2
            assert projects["project-a"]["revisions"] == 3
            assert projects["project-b"]["logical_memories"] == 1
            assert "CROSS_PROJECT_WORKFLOW" not in projects
        finally:
            await engine.close()

    asyncio.run(run())


def test_text_search_is_exact_scope_filtered_keyset_paginated_and_returns_detail_links(tmp_path) -> None:
    async def run() -> None:
        engine, _, _, overview = await _services(tmp_path)
        try:
            rows = [
                _record(
                    marker="needle-a1",
                    fact="Needle alpha",
                    created_at="2026-11-05T20:00:01Z",
                ),
                _record(
                    marker="needle-a2",
                    fact="Needle beta",
                    created_at="2026-11-05T20:00:02Z",
                ),
                _record(
                    marker="needle-a3",
                    fact="Needle gamma",
                    created_at="2026-11-05T20:00:03Z",
                ),
                _record(
                    marker="needle-b",
                    fact="Needle from another project",
                    project_id="project-b",
                    created_at="2026-11-05T20:00:04Z",
                ),
                _record(
                    marker="other-a",
                    fact="Different content",
                    created_at="2026-11-05T20:00:05Z",
                ),
            ]
            for row in rows:
                await _write(engine, row)

            first = await overview.search(
                query="needle",
                search_mode="TEXT",
                scope="PROJECT",
                project_id="project-a",
                lifecycle_state="CANDIDATE",
                limit=2,
            )
            assert first["status"] == "OK"
            assert first["search"] == {
                "requested_mode": "TEXT",
                "effective_mode": "TEXT",
                "degraded": False,
                "degradation": None,
            }
            assert first["counts"]["filtered_logical"] == {
                "value": 3,
                "completeness": "EXACT",
                "reason": None,
            }
            assert first["counts"]["page_rows"] == {
                "value": 2,
                "completeness": "EXACT",
                "reason": None,
            }
            assert first["next_cursor"] is not None
            assert all(item["scope"]["project_id"] == "project-a" for item in first["items"])
            assert all(
                item["detail_link"].startswith(f"/owner/memories/{item['memory_id']}?scope=PROJECT&project_id=project-a")
                for item in first["items"]
            )

            second = await overview.search(
                query="needle",
                search_mode="TEXT",
                scope="PROJECT",
                project_id="project-a",
                lifecycle_state="CANDIDATE",
                limit=2,
                cursor=first["next_cursor"],
            )
            assert len(second["items"]) == 1
            assert second["next_cursor"] is None
            assert {item["record_id"] for item in first["items"]}.isdisjoint(
                {item["record_id"] for item in second["items"]}
            )

            source_filtered = await overview.search(
                query="needle",
                search_mode="TEXT",
                scope="PROJECT",
                project_id="project-a",
                source_ref="source:needle-a2",
                created_after="2026-11-05T20:00:01Z",
                created_before="2026-11-05T20:00:03Z",
            )
            assert [item["fact"] for item in source_filtered["items"]] == ["Needle beta"]

            with pytest.raises(AdapterError) as exc_info:
                await overview.search(
                    query="needle",
                    search_mode="TEXT",
                    scope="PROJECT",
                    project_id="project-b",
                    lifecycle_state="CANDIDATE",
                    limit=2,
                    cursor=first["next_cursor"],
                )
            assert exc_info.value.code == "INVALID_CURSOR"
        finally:
            await engine.close()

    asyncio.run(run())


def test_semantic_provider_failure_degrades_to_exact_text_fallback_without_mutation(tmp_path) -> None:
    async def run() -> None:
        engine, provider, _, overview = await _services(tmp_path, fail_query=True)
        try:
            row = _record(marker="fallback", fact="fallback needle survives provider failure")
            await _write(engine, row)
            changes_before = engine.conn.total_changes if engine.conn is not None else -1

            result = await overview.search(
                query="fallback needle",
                search_mode="SEMANTIC",
                scope="PROJECT",
                project_id="project-a",
            )

            assert provider.query_calls == 1
            assert result["status"] == "DEGRADED"
            assert result["search"] == {
                "requested_mode": "SEMANTIC",
                "effective_mode": "TEXT",
                "degraded": True,
                "degradation": "EMBEDDING_UNAVAILABLE",
            }
            assert [item["memory_id"] for item in result["items"]] == [row["memory_id"]]
            assert result["counts"]["filtered_logical"]["completeness"] == "EXACT"
            changes_after = engine.conn.total_changes if engine.conn is not None else -1
            assert changes_after == changes_before
        finally:
            await engine.close()

    asyncio.run(run())


def test_search_preserves_all_five_exact_scope_fields(tmp_path) -> None:
    async def run() -> None:
        engine, _, _, overview = await _services(tmp_path)
        try:
            exact = _record(
                marker="workflow-exact",
                fact="Exact workflow needle",
                scope="WORKFLOW",
                project_id="project-a",
                worktree_id="worktree-a",
                workflow_id="workflow-a",
                agent_id="agent-a",
            )
            sibling = _record(
                marker="workflow-sibling",
                fact="Exact workflow needle sibling",
                scope="WORKFLOW",
                project_id="project-a",
                worktree_id="worktree-b",
                workflow_id="workflow-a",
                agent_id="agent-a",
            )
            await _write(engine, exact)
            await _write(engine, sibling)

            result = await overview.search(
                query="workflow needle",
                search_mode="TEXT",
                scope="WORKFLOW",
                project_id="project-a",
                worktree_id="worktree-a",
                workflow_id="workflow-a",
                agent_id="agent-a",
            )

            assert [item["memory_id"] for item in result["items"]] == [exact["memory_id"]]
            assert result["items"][0]["scope"] == {
                "scope": "WORKFLOW",
                "project_id": "project-a",
                "worktree_id": "worktree-a",
                "workflow_id": "workflow-a",
                "agent_id": "agent-a",
            }
            link = result["items"][0]["detail_link"]
            for field, value in (
                ("scope", "WORKFLOW"),
                ("project_id", "project-a"),
                ("worktree_id", "worktree-a"),
                ("workflow_id", "workflow-a"),
                ("agent_id", "agent-a"),
            ):
                assert f"{field}={value}" in link
        finally:
            await engine.close()

    asyncio.run(run())


def test_successful_semantic_search_marks_bounded_match_total_partial(tmp_path) -> None:
    async def run() -> None:
        engine, provider, _, overview = await _services(tmp_path)
        try:
            row = _record(marker="semantic", fact="semantic candidate")
            await _write(engine, row)

            result = await overview.search(
                query="semantic candidate",
                search_mode="SEMANTIC",
                scope="PROJECT",
                project_id="project-a",
            )

            assert provider.query_calls == 1
            assert result["status"] == "OK"
            assert result["search"]["effective_mode"] == "SEMANTIC"
            assert result["counts"]["filtered_logical"]["value"] == 1
            assert result["counts"]["filtered_logical"]["completeness"] == "PARTIAL"
            assert result["counts"]["filtered_logical"]["reason"] == "SEMANTIC_TOP_K_BOUNDED"
            assert result["items"][0]["memory_id"] == row["memory_id"]
        finally:
            await engine.close()

    asyncio.run(run())


def test_overview_requires_governed_owner_control_service() -> None:
    with pytest.raises(AdapterError) as exc_info:
        OwnerOverviewService(object())  # type: ignore[arg-type]
    assert exc_info.value.code == "WRITE_AUTHORIZATION_DENIED"
