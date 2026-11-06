from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter, trusted_write_context_for_profile
from factlane.contract import AdapterError, ScopeContext, canonical_json
from factlane.embeddings import EmbeddingProfile
from factlane.owner_database import build_logical_export, validate_logical_document
from factlane.storage import SQLiteVecEngine


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="logical-export-selector-256",
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


class _NoCallProvider:
    def __init__(self) -> None:
        self.profile = _profile()
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, _texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        raise AssertionError("logical export selection must not call the embedding provider")

    def embed_query(self, _text: str) -> list[float]:
        self.query_calls += 1
        raise AssertionError("logical export selection must not call the embedding provider")


_COLUMNS = (
    "record_id",
    "memory_id",
    "revision",
    "parent_record_id",
    "scope",
    "project_id",
    "worktree_id",
    "workflow_id",
    "agent_id",
    "memory_type",
    "fact",
    "source_provenance",
    "source_timestamp",
    "created_at",
    "last_verified_at",
    "verified_by",
    "authority_role",
    "contribution_origin",
    "freshness_policy",
    "supersedes",
    "contradiction_key",
    "contradiction_state",
    "confidence",
    "tags",
    "lifecycle_state",
    "native_content_hash",
    "payload_fingerprint",
    "idempotency_key",
    "embedding_profile_id",
    "embedding_model_digest",
    "embedding_output_dimension",
)


def _uuid(index: int) -> str:
    return str(uuid.UUID(int=index))


def _record(
    index: int,
    scope: ScopeContext,
    lifecycle_state: str,
    *,
    memory_id: str | None = None,
    revision: int = 1,
    parent_record_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    authority = {
        "GLOBAL_USER": "OWNER_CURRENT",
        "PROJECT": "PROJECT_CURRENT",
        "WORKFLOW": "WORKFLOW_CURRENT",
        "CROSS_PROJECT_WORKFLOW": "WORKFLOW_CURRENT",
        "TOOL_ENVIRONMENT": "TOOL_ENV_CURRENT",
    }[scope.scope]
    record_id = _uuid(100_000 + index)
    memory_id = memory_id or _uuid(200_000 + index)
    subject = f"logical-export-{index}"
    provenance = {
        "source_class": "TEST",
        "source_ref": f"logical-export:{index}",
        "source_hash": hashlib.sha256(f"source:{index}".encode()).hexdigest(),
        "review_ref": f"logical-export-review:{index}",
        "extraction_method": "fixture",
    }
    return {
        "record_id": record_id,
        "memory_id": memory_id,
        "revision": revision,
        "parent_record_id": parent_record_id,
        "scope": scope.scope,
        "project_id": scope.project_id,
        "worktree_id": scope.worktree_id,
        "workflow_id": scope.workflow_id,
        "agent_id": scope.agent_id,
        "memory_type": "WORKFLOW_RULE" if scope.scope == "CROSS_PROJECT_WORKFLOW" else "PROJECT_LEARNED_FACT",
        "fact": f"Logical export fixture fact {index}.",
        "source_provenance": canonical_json(provenance),
        "source_timestamp": "2026-11-05T20:00:00Z",
        "created_at": created_at or f"2026-11-05T20:{index % 60:02d}:00Z",
        "last_verified_at": "2026-11-05T20:30:00Z",
        "verified_by": "OWNER",
        "authority_role": authority if lifecycle_state == "VALIDATED_CURRENT" else "HISTORICAL",
        "contribution_origin": canonical_json({"contributor_class": "OWNER", "contributor_ref": "selector-test"}),
        "freshness_policy": canonical_json({"kind": "manual"}),
        "supersedes": canonical_json([]),
        "contradiction_key": hashlib.sha256(f"subject:{subject}".encode()).hexdigest(),
        "contradiction_state": "RESOLVED" if lifecycle_state in {"SUPERSEDED", "REVOKED", "REJECTED", "HISTORICAL"} else "NONE",
        "confidence": 0.75,
        "tags": canonical_json([f"subject:{subject}", "logical-export:test"]),
        "lifecycle_state": lifecycle_state,
        "native_content_hash": hashlib.sha256(f"native:{index}".encode()).hexdigest(),
        "payload_fingerprint": hashlib.sha256(f"payload:{index}".encode()).hexdigest(),
        "idempotency_key": f"logical-export-selector-{index}",
        "embedding_profile_id": _profile().profile_id,
        "embedding_model_digest": _profile().model_digest,
        "embedding_output_dimension": _profile().output_dimension,
    }


def _insert_records(engine: SQLiteVecEngine, records: list[dict[str, Any]]) -> None:
    assert engine.conn is not None
    placeholders = ",".join("?" for _ in _COLUMNS)
    engine.conn.executemany(
        f"INSERT INTO adapter_records ({','.join(_COLUMNS)}) VALUES ({placeholders})",
        [[record[column] for column in _COLUMNS] for record in records],
    )
    engine.conn.commit()


def _adapter(engine: SQLiteVecEngine) -> tuple[MemoryAdapter, _NoCallProvider]:
    provider = _NoCallProvider()
    return (
        MemoryAdapter(
            engine,
            provider,  # type: ignore[arg-type]
            trusted_write_context=trusted_write_context_for_profile(
                "owner-current",
                contributor_ref="logical-export-selector-test",
            ),
        ),
        provider,
    )


@pytest.mark.parametrize(
    ("scope", "near_miss"),
    [
        (
            ScopeContext("GLOBAL_USER", agent_id="owner-a"),
            ScopeContext("GLOBAL_USER", agent_id="owner-b"),
        ),
        (
            ScopeContext("PROJECT", project_id="project-a", worktree_id="worktree-a", agent_id="agent-a"),
            ScopeContext("PROJECT", project_id="project-a", worktree_id="worktree-b", agent_id="agent-a"),
        ),
        (
            ScopeContext(
                "WORKFLOW",
                project_id="project-a",
                worktree_id="worktree-a",
                workflow_id="workflow-a",
                agent_id="agent-a",
            ),
            ScopeContext(
                "WORKFLOW",
                project_id="project-a",
                worktree_id="worktree-a",
                workflow_id="workflow-a",
                agent_id="agent-b",
            ),
        ),
        (
            ScopeContext(
                "TOOL_ENVIRONMENT",
                project_id="project-a",
                worktree_id="worktree-a",
                workflow_id="workflow-a",
                agent_id="agent-a",
            ),
            ScopeContext(
                "TOOL_ENVIRONMENT",
                project_id="project-a",
                worktree_id="worktree-a",
                workflow_id="workflow-b",
                agent_id="agent-a",
            ),
        ),
        (
            ScopeContext("CROSS_PROJECT_WORKFLOW"),
            ScopeContext("PROJECT", project_id="project-a"),
        ),
    ],
)
def test_logical_export_selector_matches_all_five_scope_fields_exactly(
    tmp_path,
    scope: ScopeContext,
    near_miss: ScopeContext,
) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / f"{scope.scope}.db"), _profile())
        await engine.open()
        try:
            selected = _record(1, scope, "CANDIDATE")
            excluded = _record(2, near_miss, "CANDIDATE")
            _insert_records(engine, [excluded, selected])
            adapter, provider = _adapter(engine)
            before_changes = engine.conn.total_changes if engine.conn is not None else -1

            rows = await adapter.logical_export_records(
                scope,
                lifecycle_states=("CANDIDATE",),
                max_records=1000,
            )

            assert [row["record_id"] for row in rows] == [selected["record_id"]]
            assert {
                field: rows[0][field]
                for field in ("scope", "project_id", "worktree_id", "workflow_id", "agent_id")
            } == {
                "scope": scope.scope,
                "project_id": scope.project_id,
                "worktree_id": scope.worktree_id,
                "workflow_id": scope.workflow_id,
                "agent_id": scope.agent_id,
            }
            assert rows[0]["fact"] == selected["fact"]
            assert json.loads(rows[0]["tags"])[0] == "subject:logical-export-1"
            assert provider.document_calls == 0
            assert provider.query_calls == 0
            assert engine.conn is not None and engine.conn.total_changes == before_changes
        finally:
            await engine.close()

    asyncio.run(run())


def test_logical_export_selector_preserves_mixed_lifecycle_history_and_is_deterministic(tmp_path) -> None:
    async def run() -> None:
        scope = ScopeContext("PROJECT", project_id="project-a")
        engine = SQLiteVecEngine(str(tmp_path / "mixed-history.db"), _profile())
        await engine.open()
        try:
            lineage_memory_id = _uuid(900_000)
            first = _record(30, scope, "SUPERSEDED", memory_id=lineage_memory_id, revision=1)
            second = _record(
                31,
                scope,
                "REVOKED",
                memory_id=lineage_memory_id,
                revision=2,
                parent_record_id=first["record_id"],
            )
            records = [
                _record(12, scope, "HISTORICAL"),
                _record(6, scope, "VALIDATED_CURRENT"),
                second,
                _record(5, scope, "CANDIDATE"),
                _record(11, scope, "REJECTED"),
                first,
                _record(14, scope, "QUARANTINED"),
                _record(13, scope, "STALE"),
            ]
            _insert_records(engine, records)
            adapter, provider = _adapter(engine)
            lifecycle_states = (
                "REVOKED",
                "CANDIDATE",
                "HISTORICAL",
                "VALIDATED_CURRENT",
                "REJECTED",
                "SUPERSEDED",
                "STALE",
                "QUARANTINED",
            )

            first_read = await adapter.logical_export_records(
                scope,
                lifecycle_states=lifecycle_states,
                max_records=1000,
            )
            second_read = await adapter.logical_export_records(
                scope,
                lifecycle_states=tuple(reversed(lifecycle_states)),
                max_records=1000,
            )

            expected_order = sorted(records, key=lambda item: (item["memory_id"], item["revision"], item["record_id"]))
            assert [row["record_id"] for row in first_read] == [row["record_id"] for row in expected_order]
            assert second_read == first_read
            assert {row["lifecycle_state"] for row in first_read} == set(lifecycle_states)
            lineage = [row for row in first_read if row["memory_id"] == lineage_memory_id]
            assert [(row["revision"], row["lifecycle_state"]) for row in lineage] == [
                (1, "SUPERSEDED"),
                (2, "REVOKED"),
            ]
            for row in first_read:
                assert set(_COLUMNS).issubset(row)
                assert isinstance(json.loads(row["source_provenance"]), dict)
                assert json.loads(row["freshness_policy"]) == {"kind": "manual"}
                assert json.loads(row["tags"])[0].startswith("subject:")
                assert row["contradiction_key"]
                assert row["confidence"] == 0.75

            exported = build_logical_export(
                first_read,
                factlane_version="0.1.7",
                storage_contract_version=3,
                embedding_profile_id=_profile().profile_id,
                exported_at="2026-11-05T21:00:00Z",
            )
            document = validate_logical_document(exported)
            assert len(document["records"]) == len(records)
            wire_by_id = {item["source_record_id"]: item for item in document["records"]}
            assert wire_by_id[first["record_id"]]["active"] is False
            assert wire_by_id[second["record_id"]]["active"] is False
            assert provider.document_calls == 0
            assert provider.query_calls == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_logical_export_selector_fails_closed_over_limit_without_truncation(tmp_path) -> None:
    async def run() -> None:
        scope = ScopeContext("PROJECT", project_id="project-limit")
        engine = SQLiteVecEngine(str(tmp_path / "limit.db"), _profile())
        await engine.open()
        try:
            _insert_records(engine, [_record(index, scope, "CANDIDATE") for index in range(1, 1002)])
            adapter, provider = _adapter(engine)
            before_changes = engine.conn.total_changes if engine.conn is not None else -1

            with pytest.raises(AdapterError) as exceeded:
                await adapter.logical_export_records(
                    scope,
                    lifecycle_states=("CANDIDATE",),
                    max_records=1000,
                )
            assert exceeded.value.code == "LIMIT_EXCEEDED"

            with pytest.raises(AdapterError) as invalid_bound:
                await adapter.logical_export_records(
                    scope,
                    lifecycle_states=("CANDIDATE",),
                    max_records=1001,
                )
            assert invalid_bound.value.code == "INVALID_ENVELOPE"

            with pytest.raises(AdapterError) as invalid_lifecycle:
                await adapter.logical_export_records(
                    scope,
                    lifecycle_states=("CANDIDATE", "DELETED"),
                    max_records=1000,
                )
            assert invalid_lifecycle.value.code == "INVALID_ENUM"

            assert provider.document_calls == 0
            assert provider.query_calls == 0
            assert engine.conn is not None and engine.conn.total_changes == before_changes
        finally:
            await engine.close()

    asyncio.run(run())
