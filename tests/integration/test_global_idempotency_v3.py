from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from factlane.contract import AdapterError, ScopeContext, validate_scope
from factlane.embeddings import EmbeddingProfile
from factlane.storage import RecordUniquenessConflict, SQLiteVecEngine


_EMBEDDING = [1.0] + [0.0] * 255
_PROJECT_ID = "global-idempotency-v3"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="global-idempotency-v3-256",
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


def _scope() -> ScopeContext:
    return validate_scope("PROJECT", project_id=_PROJECT_ID)


def _profile_metadata() -> dict[str, object]:
    profile = _profile()
    return {
        "metadata_revision": 2,
        "store_key": profile.profile_id,
        "semantic_identity": profile.semantic_identity(),
        "compatibility_proof": {
            "state": "COMPATIBLE",
            "basis": "TEST_PINNED_PROFILE_FIXTURE",
            "reason": None,
            "anchor_bundle_id": None,
            "anchor_bundle_sha256": None,
            "minimum_document_cross_space_cosine": None,
            "minimum_query_match_cosine": None,
            "minimum_query_top1_margin": None,
        },
        "runtime_provenance": {
            "provider_kind": profile.provider_kind,
            "model": profile.base_model_identity,
            "model_digest": profile.model_digest,
            "ollama_version": "test-fixture",
            "semantic_family": profile.semantic_family,
            "native_dimension": profile.source_dimension,
            "output_dimension": profile.output_dimension,
            "effective_context_window": profile.minimum_context_window,
        },
        "runtime_fingerprint": _sha256("global-idempotency-v3-runtime"),
    }


async def _establish_test_profile_binding(engine: SQLiteVecEngine) -> None:
    metadata = _profile_metadata()
    if engine.embedding_profile_metadata is None:
        await engine.bind_embedding_profile(metadata, expect_missing=True)
        return
    assert engine.embedding_profile_metadata == metadata
    engine.set_embedding_compatibility(
        "COMPATIBLE",
        reason=None,
        store_key=str(metadata["store_key"]),
        metadata=metadata,
    )


def _record(marker: str, *, idempotency_key: str, contradiction_key: str | None = None) -> dict[str, Any]:
    fact = f"Global idempotency fixture {marker}."
    return {
        "record_id": str(uuid.uuid4()),
        "memory_id": str(uuid.uuid4()),
        "revision": 1,
        "parent_record_id": None,
        "scope": "PROJECT",
        "project_id": _PROJECT_ID,
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "memory_type": "PROJECT_LEARNED_FACT",
        "fact": fact,
        "source_provenance": {
            "source_class": "TEST",
            "source_ref": f"global-idempotency:{marker}",
            "source_hash": _sha256(f"source:{marker}"),
            "review_ref": "storage-v3-global-idempotency",
            "extraction_method": "integration-test",
        },
        "source_timestamp": None,
        "created_at": "2026-11-01T00:00:00Z",
        "last_verified_at": None,
        "verified_by": "UNVERIFIED",
        "authority_role": "UNRESOLVED",
        "contribution_origin": {"contributor_class": "OWNER", "contributor_ref": "storage-v3-test"},
        "freshness_policy": {"kind": "manual"},
        "supersedes": [],
        "contradiction_key": contradiction_key or f"global-idempotency:{marker}",
        "contradiction_state": "NONE",
        "confidence": 0.5,
        "tags": [f"subject:global-idempotency:{marker}"],
        "lifecycle_state": "CANDIDATE",
        "native_content_hash": _sha256(f"native:{marker}"),
        "payload_fingerprint": _sha256(f"payload:{marker}"),
        "idempotency_key": idempotency_key,
        "embedding_profile_id": _profile().profile_id,
        "embedding_model_digest": _profile().model_digest,
        "embedding_output_dimension": _profile().output_dimension,
    }


async def _reject_candidate(
    engine: SQLiteVecEngine,
    target: dict[str, Any],
    *,
    idempotency_key: str,
    observed_group_token: str | None = None,
) -> dict[str, Any]:
    scope = _scope()
    if observed_group_token is None:
        observed = await engine.group_observation(scope, str(target["contradiction_key"]))
        observed_group_token = str(observed["public"]["observed_group_token"])
    return await engine.terminal_disposition(
        scope,
        action="REJECT",
        memory_id=str(target["memory_id"]),
        expected_record_id=str(target["record_id"]),
        expected_revision=int(target["revision"]),
        observed_group_token=observed_group_token,
        idempotency_key=idempotency_key,
        request_fingerprint=_sha256(f"reject:{idempotency_key}"),
        request_id=f"request-{idempotency_key}",
        reason_code="INSUFFICIENT_EVIDENCE",
        actor_class="OWNER",
        actor_ref_digest=_sha256("storage-v3-test-owner"),
        grant_ref_digest=_sha256("storage-v3-test-grant"),
        committed_at="2026-11-01T00:00:01Z",
    )


def _claim_counts(db_path: Path, idempotency_key: str) -> tuple[int, int]:
    with sqlite3.connect(db_path) as conn:
        adapter_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM adapter_records WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()[0]
        )
        operation_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM lifecycle_operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()[0]
        )
    return adapter_count, operation_count


def test_adapter_first_blocks_same_key_lifecycle_operation(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "adapter-first.db"
        engine = SQLiteVecEngine(str(db_path), _profile())
        await engine.open()
        try:
            shared_key = "global-idempotency-adapter-first"
            adapter_claim = _record("adapter-first-claim", idempotency_key=shared_key)
            target = _record("adapter-first-target", idempotency_key="adapter-first-target-seed")
            await engine.write_record(adapter_claim, _EMBEDDING)
            await engine.write_record(target, _EMBEDDING)

            with pytest.raises(AdapterError) as exc_info:
                await _reject_candidate(engine, target, idempotency_key=shared_key)

            assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"
            assert _claim_counts(db_path, shared_key) == (1, 0)
            target_rows = await engine.get_record(str(target["memory_id"]), _scope(), history=True)
            assert target_rows[0]["lifecycle_state"] == "CANDIDATE"
        finally:
            await engine.close()

    asyncio.run(run())


def test_operation_first_blocks_same_key_adapter_record(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = tmp_path / "operation-first.db"
        engine = SQLiteVecEngine(str(db_path), _profile())
        await engine.open()
        try:
            shared_key = "global-idempotency-operation-first"
            target = _record("operation-first-target", idempotency_key="operation-first-target-seed")
            adapter_claim = _record("operation-first-adapter-claim", idempotency_key=shared_key)
            await engine.write_record(target, _EMBEDDING)
            result = await _reject_candidate(engine, target, idempotency_key=shared_key)
            assert result["action"] == "REJECT"

            with pytest.raises(RecordUniquenessConflict) as exc_info:
                await engine.write_record(adapter_claim, _EMBEDDING)

            assert exc_info.value.constraint == "global_idempotency"
            assert _claim_counts(db_path, shared_key) == (0, 1)
        finally:
            await engine.close()

    asyncio.run(run())


def test_concurrent_cross_connection_claim_has_one_durable_global_winner(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent-claims.db"
    shared_key = "global-idempotency-concurrent-claim"
    target = _record("concurrent-target", idempotency_key="concurrent-target-seed")
    adapter_claim = _record("concurrent-adapter-claim", idempotency_key=shared_key)

    async def prepare() -> str:
        engine = SQLiteVecEngine(str(db_path), _profile())
        await engine.open()
        try:
            await _establish_test_profile_binding(engine)
            await engine.write_record(target, _EMBEDDING)
            observed = await engine.group_observation(_scope(), str(target["contradiction_key"]))
            return str(observed["public"]["observed_group_token"])
        finally:
            await engine.close()

    observed_group_token = asyncio.run(prepare())
    barrier = threading.Barrier(2)

    def adapter_worker() -> tuple[str, str | None]:
        async def claim() -> tuple[str, str | None]:
            engine = SQLiteVecEngine(str(db_path), _profile())
            await engine.open()
            try:
                await _establish_test_profile_binding(engine)
                barrier.wait()
                try:
                    await engine.write_record(adapter_claim, _EMBEDDING)
                except RecordUniquenessConflict as exc:
                    return "CONFLICT", exc.constraint
                return "COMMITTED", None
            finally:
                await engine.close()

        return asyncio.run(claim())

    def operation_worker() -> tuple[str, str | None]:
        async def claim() -> tuple[str, str | None]:
            engine = SQLiteVecEngine(str(db_path), _profile())
            await engine.open()
            try:
                await _establish_test_profile_binding(engine)
                barrier.wait()
                try:
                    await _reject_candidate(
                        engine,
                        target,
                        idempotency_key=shared_key,
                        observed_group_token=observed_group_token,
                    )
                except AdapterError as exc:
                    return "CONFLICT", exc.code
                return "COMMITTED", None
            finally:
                await engine.close()

        return asyncio.run(claim())

    with ThreadPoolExecutor(max_workers=2) as pool:
        adapter_future = pool.submit(adapter_worker)
        operation_future = pool.submit(operation_worker)
        adapter_result = adapter_future.result()
        operation_result = operation_future.result()

    assert [adapter_result[0], operation_result[0]].count("COMMITTED") == 1
    assert [adapter_result[0], operation_result[0]].count("CONFLICT") == 1
    if adapter_result[0] == "CONFLICT":
        assert adapter_result[1] == "global_idempotency"
    if operation_result[0] == "CONFLICT":
        assert operation_result[1] == "IDEMPOTENCY_CONFLICT"

    adapter_count, operation_count = _claim_counts(db_path, shared_key)
    assert adapter_count + operation_count == 1
    assert (adapter_count, operation_count) in {(1, 0), (0, 1)}
