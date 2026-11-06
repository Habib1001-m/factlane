from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import pytest

from factlane.adapter import MemoryAdapter, trusted_write_context_for_profile
from factlane.contract import AdapterError, canonical_json
from factlane.embeddings import EmbeddingProfile
from factlane.owner_database import (
    LOGICAL_IMPORT_MAX_BYTES,
    apply_logical_import_to_adapter,
    build_logical_export,
    preview_logical_import,
    retrieve_logical_import_evidence,
    validate_logical_document,
)
from factlane.storage import SQLiteVecEngine


def _uuid(index: int) -> str:
    return str(uuid.UUID(int=index))


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="logical-import-test-256",
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
    def __init__(self) -> None:
        self.profile = _profile()
        self.document_calls = 0
        self.query_calls = 0
        self.fail_documents = False

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self.fail_documents:
            raise RuntimeError("embedding provider unavailable")
        self.document_calls += len(texts)
        return [[1.0] + [0.0] * 255 for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return [1.0] + [0.0] * 255


def _source_record(
    index: int,
    *,
    scope: dict[str, object] | None = None,
    fact: str | None = None,
    subject: str | None = None,
) -> dict[str, object]:
    scope = scope or {"scope": "PROJECT", "project_id": "factlane"}
    memory_type = "WORKFLOW_RULE" if scope["scope"] == "CROSS_PROJECT_WORKFLOW" else "PROJECT_LEARNED_FACT"
    subject = subject or f"import-{index}"
    return {
        "record_id": _uuid(10_000 + index),
        "memory_id": _uuid(20_000 + index),
        "revision": 1,
        "parent_record_id": None,
        **scope,
        "memory_type": memory_type,
        "fact": fact or f"Imported fact {index}.",
        "tags": [f"subject:{subject}"],
        "source_provenance": {
            "source_class": "TEST",
            "source_ref": f"fixture:{index}",
            "source_hash": f"{(index % 10)}" * 64,
            "review_ref": f"review:{index}",
            "extraction_method": "fixture",
        },
        "freshness_policy": {"kind": "manual"},
        "source_timestamp": "2026-11-05T20:00:00Z",
        "lifecycle_state": "VALIDATED_CURRENT",
        "verified_by": "OWNER",
        "supersedes": [],
        "active": True,
    }


def _payload(records: list[dict[str, object]]) -> bytes:
    return build_logical_export(
        records,
        factlane_version="0.1.7",
        storage_contract_version=3,
        embedding_profile_id=_profile().profile_id,
        exported_at="2026-11-05T20:30:00Z",
    )


def _adapter(engine: SQLiteVecEngine, provider: _Provider) -> MemoryAdapter:
    return MemoryAdapter(
        engine,
        provider,  # type: ignore[arg-type]
        trusted_write_context=trusted_write_context_for_profile(
            "owner-current",
            contributor_ref="logical-import-owner",
            grant_ref="logical-import-test-grant",
        ),
    )


def test_logical_import_production_path_requires_direct_owner_authority(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "delegated-import-denied.db"), _profile())
        await engine.open()
        adapter = MemoryAdapter(
            engine,
            _Provider(),  # type: ignore[arg-type]
            trusted_write_context=trusted_write_context_for_profile(
                "delegated-candidate",
                contributor_ref="delegated-importer",
            ),
        )
        try:
            with pytest.raises(AdapterError, match="direct Owner"):
                await apply_logical_import_to_adapter(
                    _payload([_source_record(1)]),
                    operation_id="logical-import-delegated-denied",
                    adapter=adapter,
                )
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 0
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_operations").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_logical_import_binding_can_cover_external_history_scopes_not_admitted(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "history-scope-import.db"), _profile())
        await engine.open()
        adapter = _adapter(engine, _Provider())
        active = _source_record(1, scope={"scope": "PROJECT", "project_id": "factlane"})
        historical = _source_record(2, scope={"scope": "GLOBAL_USER", "agent_id": "hermes"})
        historical["lifecycle_state"] = "REVOKED"
        historical["active"] = False
        try:
            result = await apply_logical_import_to_adapter(
                _payload([active, historical]),
                operation_id="logical-import-history-scope",
                adapter=adapter,
            )
            assert result["admitted"] == 1
            assert result["results"][0]["scope"] == {
                "scope": "PROJECT",
                "project_id": "factlane",
                "worktree_id": None,
                "workflow_id": None,
                "agent_id": None,
            }
        finally:
            await engine.close()

    asyncio.run(run())


def test_logical_import_durably_retains_non_authoritative_active_and_terminal_source_evidence(tmp_path) -> None:
    async def run() -> None:
        db_path = str(tmp_path / "evidence-retention-import.db")
        engine = SQLiteVecEngine(db_path, _profile())
        await engine.open()
        provider = _Provider()
        adapter = _adapter(engine, provider)

        active = _source_record(
            1,
            scope={
                "scope": "WORKFLOW",
                "project_id": "factlane",
                "worktree_id": "wt-a",
                "workflow_id": "review",
                "agent_id": "hermes",
            },
        )
        revoked = _source_record(2, scope={"scope": "GLOBAL_USER", "agent_id": "hermes"})
        revoked["lifecycle_state"] = "REVOKED"
        revoked["active"] = False
        rejected = _source_record(3, scope={"scope": "PROJECT", "project_id": "factlane"})
        rejected["lifecycle_state"] = "REJECTED"
        rejected["active"] = False
        superseded = _source_record(4, scope={"scope": "CROSS_PROJECT_WORKFLOW"})
        superseded["lifecycle_state"] = "SUPERSEDED"
        superseded["active"] = False
        superseded["supersedes"] = [revoked["memory_id"], rejected["memory_id"]]

        payload = _payload([active, revoked, rejected, superseded])
        normalized = validate_logical_document(payload)["records"]
        expected_by_id = {record["source_record_id"]: record for record in normalized}
        try:
            first = await apply_logical_import_to_adapter(
                payload,
                operation_id="logical-import-evidence-retention",
                adapter=adapter,
            )
            assert first["admitted"] == 1
            assert first["duplicates"] == 0
            assert len(first["source_evidence"]) == 4
            assert first["evidence_policy"] == "NON_AUTHORITATIVE_IMPORT_EVIDENCE"
            assert first["active_source_evidence_count"] == 1
            assert first["external_history_count"] == 3

            evidence_by_id = {
                member["source_record"]["source_record_id"]: member
                for member in first["source_evidence"]
            }
            assert set(evidence_by_id) == set(expected_by_id)
            for source_record_id, expected in expected_by_id.items():
                assert evidence_by_id[source_record_id]["source_record"] == expected
                assert evidence_by_id[source_record_id]["evidence_class"] == "NON_AUTHORITATIVE_SOURCE_RECORD"
                assert evidence_by_id[source_record_id]["authority_effect"] == "NONE"

            active_member = evidence_by_id[normalized[0]["source_record_id"]]
            assert active_member["status"] == "ADMITTED"
            assert active_member["destination_memory_id"] is not None
            assert active_member["destination_record_id"] is not None
            assert active_member["destination_lifecycle_state"] == "CANDIDATE"
            assert active_member["destination_verified_by"] == "UNVERIFIED"
            assert active_member["destination_authority_role"] == "UNRESOLVED"

            for history_record in normalized[1:]:
                member = evidence_by_id[history_record["source_record_id"]]
                assert member["status"] == "HISTORY_ONLY"
                assert member["destination_memory_id"] is None
                assert member["destination_record_id"] is None
                assert member["destination_lifecycle_state"] is None
                assert member["destination_verified_by"] is None
                assert member["destination_authority_role"] is None

            assert evidence_by_id[normalized[1]["source_record_id"]]["source_record"]["source_lifecycle"] == "REVOKED"
            assert evidence_by_id[normalized[2]["source_record_id"]]["source_record"]["source_lifecycle"] == "REJECTED"
            assert evidence_by_id[normalized[3]["source_record_id"]]["source_record"]["source_lifecycle"] == "SUPERSEDED"
            assert evidence_by_id[normalized[3]["source_record_id"]]["source_record"]["supersedes_source_memory_ids"] == [
                normalized[1]["source_memory_id"],
                normalized[2]["source_memory_id"],
            ]
            assert evidence_by_id[normalized[0]["source_record_id"]]["source_record"]["scope"] == {
                "scope": "WORKFLOW",
                "project_id": "factlane",
                "worktree_id": "wt-a",
                "workflow_id": "review",
                "agent_id": "hermes",
            }
            assert evidence_by_id[normalized[3]["source_record_id"]]["source_record"]["scope"] == {
                "scope": "CROSS_PROJECT_WORKFLOW"
            }

            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 1
            only = engine.conn.execute(
                "SELECT lifecycle_state,verified_by,authority_role FROM adapter_records"
            ).fetchone()
            assert only == ("CANDIDATE", "UNVERIFIED", "UNRESOLVED")

            provider.fail_documents = True
            replay = await apply_logical_import_to_adapter(
                payload,
                operation_id="logical-import-evidence-retention",
                adapter=adapter,
            )
            assert replay["idempotent_replay"] is True
            assert replay["source_evidence"] == first["source_evidence"]
            assert replay["external_history"] == first["external_history"]

            retrieved = await retrieve_logical_import_evidence(
                adapter=adapter,
                operation_id="logical-import-evidence-retention",
            )
            assert retrieved["evidence_policy"] == "NON_AUTHORITATIVE_IMPORT_EVIDENCE"
            assert retrieved["source_evidence"] == first["source_evidence"]
            assert retrieved["active_source_evidence"] == first["active_source_evidence"]
            assert retrieved["external_history"] == first["external_history"]
            assert retrieved["admitted"] == 1
        finally:
            await engine.close()

        reopened_engine = SQLiteVecEngine(db_path, _profile())
        await reopened_engine.open()
        try:
            reopened_adapter = _adapter(reopened_engine, _Provider())
            reopened = await retrieve_logical_import_evidence(
                adapter=reopened_adapter,
                operation_id="logical-import-evidence-retention",
            )
            assert reopened["source_evidence"] == first["source_evidence"]
            assert reopened["external_history"] == first["external_history"]
            assert reopened_engine.conn is not None
            assert reopened_engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 1
        finally:
            await reopened_engine.close()

    asyncio.run(run())


def test_logical_import_production_path_preserves_1000_record_and_10mib_bounds_before_provider_work(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "bounded-import.db"), _profile())
        await engine.open()
        provider = _Provider()
        adapter = _adapter(engine, provider)
        valid = _payload([_source_record(1)])
        try:
            too_many = json.loads(valid)
            too_many["records"] = too_many["records"] * 1001
            with pytest.raises(AdapterError, match="1000"):
                await apply_logical_import_to_adapter(
                    json.dumps(too_many, sort_keys=True, separators=(",", ":")).encode(),
                    operation_id="logical-import-too-many",
                    adapter=adapter,
                )

            oversized = valid + b" " * (LOGICAL_IMPORT_MAX_BYTES - len(valid) + 1)
            with pytest.raises(AdapterError, match="10 MiB"):
                await apply_logical_import_to_adapter(
                    oversized,
                    operation_id="logical-import-too-large",
                    adapter=adapter,
                )
            assert provider.document_calls == 0
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 0
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_operations").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_logical_import_production_path_admits_multi_scope_candidates_under_one_receipt(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "atomic-import.db"), _profile())
        await engine.open()
        provider = _Provider()
        adapter = _adapter(engine, provider)
        payload = _payload(
            [
                _source_record(1, scope={"scope": "GLOBAL_USER", "agent_id": "hermes"}),
                _source_record(
                    2,
                    scope={
                        "scope": "PROJECT",
                        "project_id": "factlane",
                        "worktree_id": "wt-a",
                        "agent_id": "hermes",
                    },
                ),
                _source_record(
                    3,
                    scope={
                        "scope": "WORKFLOW",
                        "project_id": "factlane",
                        "worktree_id": "wt-a",
                        "workflow_id": "review",
                        "agent_id": "hermes",
                    },
                ),
                _source_record(
                    4,
                    scope={
                        "scope": "TOOL_ENVIRONMENT",
                        "project_id": "factlane",
                        "worktree_id": "wt-a",
                        "workflow_id": "review",
                        "agent_id": "hermes",
                    },
                ),
                _source_record(5, scope={"scope": "CROSS_PROJECT_WORKFLOW"}),
            ]
        )
        try:
            first = await apply_logical_import_to_adapter(
                payload,
                operation_id="logical-import-op-1",
                adapter=adapter,
            )
            assert first["status"] == "OK"
            assert first["admitted"] == 5
            assert first["idempotent_replay"] is False
            assert first["operation_id"] == "logical-import-op-1"
            assert {item["status"] for item in first["results"]} == {"ADMITTED"}
            assert provider.document_calls == 5

            assert engine.conn is not None
            rows = engine.conn.execute(
                "SELECT scope,project_id,worktree_id,workflow_id,agent_id,lifecycle_state,verified_by,authority_role,source_provenance "
                "FROM adapter_records ORDER BY scope"
            ).fetchall()
            assert len(rows) == 5
            assert all(row[5:8] == ("CANDIDATE", "UNVERIFIED", "UNRESOLVED") for row in rows)
            scopes = {(row[0], row[1], row[2], row[3], row[4]) for row in rows}
            assert scopes == {
                ("GLOBAL_USER", None, None, None, "hermes"),
                ("PROJECT", "factlane", "wt-a", None, "hermes"),
                ("WORKFLOW", "factlane", "wt-a", "review", "hermes"),
                ("TOOL_ENVIRONMENT", "factlane", "wt-a", "review", "hermes"),
                ("CROSS_PROJECT_WORKFLOW", None, None, None, None),
            }
            assert all(json.loads(row[8])["source_class"] == "LOGICAL_IMPORT" for row in rows)
            receipt = engine.conn.execute(
                "SELECT operation_kind,idempotency_key,operation_id,affected_members_json,replay_status "
                "FROM lifecycle_operations"
            ).fetchone()
            assert receipt is not None
            assert receipt[0] == "LOGICAL_IMPORT"
            assert receipt[1] == first["idempotency_key"]
            assert receipt[2] == "logical-import-op-1"
            assert len(json.loads(receipt[3])) == 5
            assert receipt[4] == "COMMITTED"

            provider.fail_documents = True
            replay = await apply_logical_import_to_adapter(
                payload,
                operation_id="logical-import-op-1",
                adapter=adapter,
            )
            assert replay["idempotent_replay"] is True
            assert replay["results"] == first["results"]
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 5
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_operations").fetchone()[0] == 1
        finally:
            await engine.close()

    asyncio.run(run())


def test_logical_import_rechecks_duplicate_and_contradiction_inside_atomic_transaction(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "recheck-import.db"), _profile())
        await engine.open()
        provider = _Provider()
        adapter = _adapter(engine, provider)
        provenance = {
            "source_class": "TEST",
            "source_ref": "fixture:seed",
            "source_hash": "a" * 64,
            "review_ref": "review:seed",
            "extraction_method": "fixture",
        }
        try:
            seeded = await adapter.store(
                fact="Existing destination fact.",
                scope="PROJECT",
                project_id="factlane",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance=provenance,
                freshness_policy={"kind": "manual"},
                idempotency_key="seed-destination-candidate",
                source_timestamp="2026-11-05T20:00:00Z",
                tags=["subject:shared-import-subject"],
                requested_lifecycle_state="CANDIDATE",
            )
            seeded_record = seeded["results"][0]

            payload = _payload(
                [
                    _source_record(
                        1,
                        fact="Existing destination fact.",
                        subject="shared-import-subject",
                    ),
                    _source_record(
                        2,
                        fact="Conflicting imported fact.",
                        subject="shared-import-subject",
                    ),
                ]
            )
            result = await apply_logical_import_to_adapter(
                payload,
                operation_id="logical-import-recheck-1",
                adapter=adapter,
            )
            assert result["admitted"] == 1
            assert result["duplicates"] == 1
            by_source = {item["source_record_id"]: item for item in result["results"]}
            assert by_source[_uuid(10_001)]["status"] == "EXACT_DUPLICATE"
            assert by_source[_uuid(10_002)]["status"] == "ADMITTED"
            assert by_source[_uuid(10_002)]["contradiction_state"] == "UNRESOLVED"

            assert engine.conn is not None
            seed_state = engine.conn.execute(
                "SELECT contradiction_state FROM adapter_records WHERE record_id=?",
                (seeded_record["record_id"],),
            ).fetchone()
            assert seed_state == ("UNRESOLVED",)
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 2
        finally:
            await engine.close()

    asyncio.run(run())


def test_logical_import_rolls_back_every_record_and_receipt_on_mid_batch_storage_failure(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "rollback-import.db"), _profile())
        await engine.open()
        adapter = _adapter(engine, _Provider())
        payload = _payload([_source_record(1), _source_record(2)])
        original_insert = engine._insert_materialized_record
        inserted = 0

        def fail_second(record, embedding):
            nonlocal inserted
            inserted += 1
            if inserted == 2:
                raise RuntimeError("simulated batch storage failure")
            return original_insert(record, embedding)

        engine._insert_materialized_record = fail_second  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="simulated batch storage failure"):
                await apply_logical_import_to_adapter(
                    payload,
                    operation_id="logical-import-rollback-1",
                    adapter=adapter,
                )
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 0
            assert engine.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
            assert engine.conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 0
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_operations").fetchone()[0] == 0
        finally:
            engine._insert_materialized_record = original_insert  # type: ignore[method-assign]
            await engine.close()

    asyncio.run(run())


def test_logical_import_operation_id_reuse_and_global_idempotency_collisions_fail_closed(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "idempotency-import.db"), _profile())
        await engine.open()
        provider = _Provider()
        adapter = _adapter(engine, provider)
        payload = _payload([_source_record(1)])
        try:
            first = await apply_logical_import_to_adapter(
                payload,
                operation_id="logical-import-idempotency-1",
                adapter=adapter,
            )
            changed = json.loads(payload)
            changed["records"][0]["fact"] = "Different imported bytes."
            changed_payload = json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()
            provider.fail_documents = True
            with pytest.raises(AdapterError, match="idempotency|operation identity|another governed operation"):
                await apply_logical_import_to_adapter(
                    changed_payload,
                    operation_id="logical-import-idempotency-1",
                    adapter=adapter,
                )
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 1
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_operations").fetchone()[0] == 1

            provider.fail_documents = False
            collision_payload = _payload([_source_record(2)])
            preview = preview_logical_import(collision_payload)
            identity = {
                "operation_id": "logical-import-global-collision",
                "file_digest": preview["file_digest"],
                "scope_identity_digest": preview["scope_identity_digest"],
            }
            collision_key = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
            await adapter.store(
                fact="Reserve the logical import batch key.",
                scope="PROJECT",
                project_id="factlane",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance={
                    "source_class": "TEST",
                    "source_ref": "fixture:collision",
                    "source_hash": "b" * 64,
                    "review_ref": "review:collision",
                    "extraction_method": "fixture",
                },
                freshness_policy={"kind": "manual"},
                idempotency_key=collision_key,
                source_timestamp="2026-11-05T20:00:00Z",
                tags=["subject:global-collision"],
                requested_lifecycle_state="CANDIDATE",
            )
            provider.fail_documents = True
            with pytest.raises(AdapterError, match="idempotency|reserved"):
                await apply_logical_import_to_adapter(
                    collision_payload,
                    operation_id="logical-import-global-collision",
                    adapter=adapter,
                )
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_operations").fetchone()[0] == 1
        finally:
            await engine.close()

    asyncio.run(run())
