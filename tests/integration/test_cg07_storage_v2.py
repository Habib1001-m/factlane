from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import sqlite3
import threading
import uuid

import pytest

from factlane.adapter import MemoryAdapter, trusted_write_context_for_profile
from factlane.contract import AdapterError, ScopeContext, validate_scope
from factlane.embeddings import EmbeddingProfile
from factlane.storage import SQLiteVecEngine


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="cg07-test-256",
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

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        return [[1.0] + [0.0] * 255 for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return [1.0] + [0.0] * 255

    def provider_status(self) -> dict[str, object]:
        return {"local_only": True}


class _BlockingProvider(_Provider):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("blocking provider was not released")
        return super().embed_documents(texts)


def _record(*, lifecycle: str, marker: str, fact: str, created_at: str) -> dict[str, object]:
    return {
        "record_id": str(uuid.uuid4()),
        "memory_id": str(uuid.uuid4()),
        "revision": 1,
        "parent_record_id": None,
        "scope": "CROSS_PROJECT_WORKFLOW",
        "project_id": None,
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "memory_type": "WORKFLOW_RULE",
        "fact": fact,
        "source_provenance": {
            "source_class": "TEST",
            "source_ref": marker,
            "source_hash": marker * 64,
            "review_ref": marker,
            "extraction_method": "test",
        },
        "source_timestamp": "2026-09-26T00:00:00Z" if lifecycle == "VALIDATED_CURRENT" else None,
        "created_at": created_at,
        "last_verified_at": "2026-09-26T00:00:00Z" if lifecycle == "VALIDATED_CURRENT" else None,
        "verified_by": "AUTOMATED_CHECK" if lifecycle == "VALIDATED_CURRENT" else "UNVERIFIED",
        "authority_role": "WORKFLOW_CURRENT" if lifecycle == "VALIDATED_CURRENT" else "UNRESOLVED",
        "contribution_origin": {"contributor_class": "AUTOMATION", "contributor_ref": marker},
        "freshness_policy": {"kind": "manual", "ttl_seconds": None, "recheck_ref": None, "source_fingerprint": None},
        "supersedes": [],
        "contradiction_key": (marker * 64)[:64],
        "contradiction_state": "NONE",
        "confidence": 1.0,
        "tags": ["cg07"],
        "lifecycle_state": lifecycle,
        "native_content_hash": (marker * 64)[:64],
        "payload_fingerprint": ((marker.upper() if marker.isalpha() else marker) * 64)[:64],
        "idempotency_key": f"cg07-{marker}-{lifecycle}",
        "embedding_profile_id": _profile().profile_id,
        "embedding_model_digest": _profile().model_digest,
        "embedding_output_dimension": 256,
    }


_V1_ADAPTER_COLUMNS = (
    "record_id", "memory_id", "revision", "parent_record_id", "scope", "project_id", "worktree_id",
    "workflow_id", "agent_id", "memory_type", "fact", "source_provenance", "source_timestamp", "created_at",
    "last_verified_at", "verified_by", "authority_role", "freshness_policy", "supersedes", "contradiction_key",
    "contradiction_state", "confidence", "tags", "lifecycle_state", "native_content_hash", "payload_fingerprint",
    "idempotency_key", "embedding_profile_id", "embedding_model_digest", "embedding_output_dimension",
)


_V1_ADAPTER_DDL = """
CREATE TABLE adapter_records (
    record_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_record_id TEXT,
    scope TEXT NOT NULL,
    project_id TEXT,
    worktree_id TEXT,
    workflow_id TEXT,
    agent_id TEXT,
    memory_type TEXT NOT NULL,
    fact TEXT NOT NULL,
    source_provenance TEXT NOT NULL,
    source_timestamp TEXT,
    created_at TEXT NOT NULL,
    last_verified_at TEXT,
    verified_by TEXT NOT NULL,
    authority_role TEXT NOT NULL,
    freshness_policy TEXT NOT NULL,
    supersedes TEXT NOT NULL,
    contradiction_key TEXT NOT NULL,
    contradiction_state TEXT NOT NULL,
    confidence REAL NOT NULL,
    tags TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    native_content_hash TEXT NOT NULL UNIQUE,
    payload_fingerprint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    embedding_profile_id TEXT NOT NULL,
    embedding_model_digest TEXT NOT NULL,
    embedding_output_dimension INTEGER NOT NULL,
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
)
"""


def _open_vec_connection(db_path: str) -> sqlite3.Connection:
    import sqlite_vec

    raw = sqlite3.connect(db_path)
    raw.enable_load_extension(True)
    sqlite_vec.load(raw)
    raw.enable_load_extension(False)
    return raw


def _database_snapshot(db_path: str) -> dict[str, object]:
    raw = _open_vec_connection(db_path)
    try:
        native_columns = tuple(row[1] for row in raw.execute("PRAGMA table_info(memories)"))
        adapter_columns = tuple(row[1] for row in raw.execute("PRAGMA table_info(adapter_records)"))
        v1_projection = tuple(column for column in _V1_ADAPTER_COLUMNS if column in adapter_columns)
        return {
            "adapter_schema": tuple(
                raw.execute(
                    "SELECT type,name,sql FROM sqlite_master "
                    "WHERE name='adapter_records' OR tbl_name='adapter_records' ORDER BY type,name"
                ).fetchall()
            ),
            "adapter_columns": adapter_columns,
            "adapter_rows_v1": tuple(
                raw.execute(f"SELECT {','.join(v1_projection)} FROM adapter_records ORDER BY record_id").fetchall()
            ),
            "native_columns": native_columns,
            "native_rows": tuple(
                raw.execute(f"SELECT {','.join(native_columns)} FROM memories ORDER BY id").fetchall()
            ),
            "vector_rows": tuple(
                raw.execute("SELECT rowid,hex(content_embedding),store FROM memory_embeddings ORDER BY rowid").fetchall()
            ),
            "meta": tuple(raw.execute("SELECT key,value FROM adapter_meta ORDER BY key").fetchall()),
        }
    finally:
        raw.close()


def test_open_establishes_storage_contract_v2_and_contribution_origin(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "storage-v2.db"), _profile())
        await engine.open()
        try:
            assert engine.conn is not None
            version = engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='contract_version'"
            ).fetchone()
            assert version is not None and version[0] == "2"
            columns = {row[1] for row in engine.conn.execute("PRAGMA table_info(adapter_records)")}
            assert "contribution_origin" in columns
            triggers = {
                row[0]
                for row in engine.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='adapter_records'"
                )
            }
            assert {
                "factlane_v2_adapter_insert_fence",
                "factlane_v2_adapter_update_fence",
                "factlane_v2_adapter_delete_fence",
            }.issubset(triggers)
        finally:
            await engine.close()

    asyncio.run(run())


def test_current_get_excludes_expired_ttl_but_review_history_returns_it(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "current-get-freshness.db"), _profile())
        await engine.open()
        try:
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile("owner-current"),
            )
            stored = await adapter.store(
                fact="The deployment target is stale.example.",
                scope="PROJECT",
                project_id="project-a",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance={
                    "source_class": "TEST",
                    "source_ref": "current-get-freshness",
                    "source_hash": "a" * 64,
                    "review_ref": "current-get-freshness",
                    "extraction_method": "test",
                },
                freshness_policy={"kind": "ttl", "ttl_seconds": 1},
                idempotency_key="current-get-freshness-store",
                source_timestamp="2020-01-01T00:00:00Z",
                last_verified_at="2020-01-01T00:00:00Z",
                verified_by="OWNER",
                requested_lifecycle_state="VALIDATED_CURRENT",
            )
            memory_id = stored["results"][0]["memory_id"]

            searched = await adapter.search(
                query="deployment target",
                intent_class="CURRENT_PROJECT_STATE",
                scope="PROJECT",
                project_id="project-a",
                retrieval_mode="CURRENT",
                retrieval_mode_kind="KEYWORD",
            )
            assert searched["status"] == "DEGRADED"
            assert searched["degradation"] == "STALE_ONLY"
            assert searched["results"] == []

            current = await adapter.get(
                memory_id=memory_id,
                scope="PROJECT",
                project_id="project-a",
                retrieval_mode="CURRENT",
            )
            assert current["status"] == "DEGRADED"
            assert current["degradation"] == "STALE_ONLY"
            assert current["results"] == []

            history = await adapter.get(
                memory_id=memory_id,
                scope="PROJECT",
                project_id="project-a",
                retrieval_mode="REVIEW_HISTORY",
            )
            assert history["status"] == "OK"
            assert [row["memory_id"] for row in history["results"]] == [memory_id]
        finally:
            await engine.close()

    asyncio.run(run())


def test_current_keyword_limit_is_applied_after_candidate_exclusion(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "keyword-current.db"), _profile())
        await engine.open()
        try:
            scope = validate_scope("CROSS_PROJECT_WORKFLOW")
            current = _record(lifecycle="VALIDATED_CURRENT", marker="a", fact="shared workflow doctrine", created_at="2026-09-26T00:00:00Z")
            candidate = _record(lifecycle="CANDIDATE", marker="b", fact="shared workflow doctrine", created_at="2026-09-26T01:00:00Z")
            await engine.write_record(current, [0.0, 1.0] + [0.0] * 254)
            await engine.write_record(candidate, [1.0, 0.0] + [0.0] * 254)
            rows = await engine.keyword_candidates("shared workflow doctrine", scope, limit=1, history=False, exact=True)
            assert [row["record_id"] for row in rows] == [current["record_id"]]
        finally:
            await engine.close()
    asyncio.run(run())


def test_current_knn_constrains_eligible_rowids_before_k(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "knn-current.db"), _profile())
        await engine.open()
        try:
            scope = validate_scope("CROSS_PROJECT_WORKFLOW")
            for index in range(8):
                marker = chr(ord("b") + index)
                candidate = _record(lifecycle="CANDIDATE", marker=marker, fact=f"candidate {index}", created_at=f"2026-09-26T00:0{index}:00Z")
                await engine.write_record(candidate, [1.0, 0.0] + [0.0] * 254)
            current = _record(lifecycle="VALIDATED_CURRENT", marker="a", fact="farther current doctrine", created_at="2026-09-26T02:00:00Z")
            await engine.write_record(current, [0.0, 1.0] + [0.0] * 254)
            rows = await engine.vector_candidates([1.0, 0.0] + [0.0] * 254, scope, limit=1, history=False)
            assert len(rows) == 1
            assert rows[0][0]["record_id"] == current["record_id"]
        finally:
            await engine.close()
    asyncio.run(run())


def test_storage_v2_raw_legacy_writer_cannot_mutate_adapter_records(tmp_path) -> None:
    async def seed() -> tuple[str, str]:
        db_path = str(tmp_path / "writer-fence.db")
        engine = SQLiteVecEngine(db_path, _profile())
        await engine.open()
        try:
            current = _record(lifecycle="VALIDATED_CURRENT", marker="a", fact="protected current", created_at="2026-09-26T00:00:00Z")
            await engine.write_record(current, [0.0, 1.0] + [0.0] * 254)
            return db_path, str(current["record_id"])
        finally:
            await engine.close()
    db_path, record_id = asyncio.run(seed())
    before = _database_snapshot(db_path)
    raw = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in raw.execute("PRAGMA table_info(adapter_records)")]
        insert_values = []
        for column in columns:
            if column == "record_id":
                insert_values.append("'raw-insert-record'")
            elif column == "native_content_hash":
                insert_values.append("'" + "f" * 64 + "'")
            elif column == "idempotency_key":
                insert_values.append("'raw-insert-idempotency'")
            else:
                insert_values.append(column)
        with pytest.raises(sqlite3.OperationalError, match="factlane_contract_v2_writer"):
            raw.execute(
                f"INSERT INTO adapter_records ({','.join(columns)}) SELECT {','.join(insert_values)} "
                "FROM adapter_records WHERE record_id=?",
                (record_id,),
            )
        for sql, params in (("UPDATE adapter_records SET fact='stale-writer' WHERE record_id=?", (record_id,)), ("DELETE FROM adapter_records WHERE record_id=?", (record_id,))):
            with pytest.raises(sqlite3.OperationalError, match="factlane_contract_v2_writer"):
                raw.execute(sql, params)
    finally:
        raw.close()
    after = _database_snapshot(db_path)
    assert after == before


def test_owner_reverify_promotes_candidate_same_memory_and_preserves_origin(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "promote.db"), _profile())
        await engine.open()
        provider = _Provider()
        delegated = MemoryAdapter(engine, provider, trusted_write_context=trusted_write_context_for_profile("delegated-candidate", contributor_ref="delegated-a"))
        owner = MemoryAdapter(engine, provider, trusted_write_context=trusted_write_context_for_profile("owner-current", contributor_ref="owner-a"))
        try:
            candidate = (await delegated.dispatch("memory_store", {
                "fact": "Cross-project workflow doctrine is reviewed before becoming current.",
                "scope": "CROSS_PROJECT_WORKFLOW",
                "memory_type": "WORKFLOW_RULE",
                "source_provenance": {"source_class": "OWNER_INPUT", "source_ref": "cg07-promotion", "source_hash": "a" * 64, "review_ref": "cg07-promotion", "extraction_method": "direct-input"},
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "cg07-promotion-candidate",
            }))["results"][0]
            promoted = (await owner.dispatch("memory_update", {
                "memory_id": candidate["memory_id"],
                "expected_record_id": candidate["record_id"],
                "scope": "CROSS_PROJECT_WORKFLOW",
                "expected_revision": 1,
                "mode": "REVERIFY",
                "idempotency_key": "cg07-promotion-owner",
                "verification": {"source_timestamp": "2026-09-26T00:00:00Z", "last_verified_at": "2026-09-26T01:00:00Z", "verified_by": "OWNER"},
            }))["results"][0]
            assert promoted["memory_id"] == candidate["memory_id"]
            assert promoted["revision"] == 2
            assert promoted["parent_record_id"] == candidate["record_id"]
            assert promoted["lifecycle_state"] == "VALIDATED_CURRENT"
            assert promoted["verified_by"] == "OWNER"
            assert promoted["contribution_origin"] == {"contributor_class": "DELEGATED_AGENT", "contributor_ref": "delegated-a"}
            history = await engine.get_record(candidate["memory_id"], validate_scope("CROSS_PROJECT_WORKFLOW"), history=True)
            assert sorted((row["revision"], row["lifecycle_state"]) for row in history) == [(1, "SUPERSEDED"), (2, "VALIDATED_CURRENT")]
        finally:
            await owner.close()
    asyncio.run(run())


def test_candidate_reverify_rejects_memory_type_reclassification_before_promotion(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "reverify-type-identity.db"), _profile())
        await engine.open()
        delegated = MemoryAdapter(
            engine,
            _Provider(),
            trusted_write_context=trusted_write_context_for_profile("delegated-candidate"),
        )
        owner = MemoryAdapter(
            engine,
            _Provider(),
            trusted_write_context=trusted_write_context_for_profile("owner-current"),
        )
        try:
            current = (await owner.dispatch("memory_store", {
                "fact": "The existing preference says blue.",
                "scope": "PROJECT",
                "project_id": "factlane",
                "memory_type": "PREFERENCE",
                "source_provenance": {
                    "source_class": "OWNER_INPUT",
                    "source_ref": "reverify-type-current",
                    "source_hash": "1" * 64,
                    "review_ref": "reverify-type-current",
                    "extraction_method": "direct-input",
                },
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "reverify-type-current",
                "requested_lifecycle_state": "VALIDATED_CURRENT",
                "source_timestamp": "2026-10-02T00:00:00Z",
                "last_verified_at": "2026-10-02T00:00:00Z",
                "verified_by": "OWNER",
                "subject": "same-subject",
            }))["results"][0]
            candidate = (await delegated.dispatch("memory_store", {
                "fact": "The candidate user fact says red.",
                "scope": "PROJECT",
                "project_id": "factlane",
                "memory_type": "USER_FACT",
                "source_provenance": {
                    "source_class": "OWNER_INPUT",
                    "source_ref": "reverify-type-candidate",
                    "source_hash": "2" * 64,
                    "review_ref": "reverify-type-candidate",
                    "extraction_method": "direct-input",
                },
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "reverify-type-candidate",
                "subject": "same-subject",
            }))["results"][0]

            with pytest.raises(AdapterError) as error:
                await owner.dispatch("memory_update", {
                    "memory_id": candidate["memory_id"],
                    "expected_record_id": candidate["record_id"],
                    "scope": "PROJECT",
                    "project_id": "factlane",
                    "expected_revision": candidate["revision"],
                    "mode": "REVERIFY",
                    "idempotency_key": "reverify-type-promote",
                    "verification": {
                        "source_timestamp": "2026-10-02T01:00:00Z",
                        "last_verified_at": "2026-10-02T01:00:00Z",
                        "verified_by": "OWNER",
                        "memory_type": "PREFERENCE",
                        "subject": "same-subject",
                    },
                })
            assert error.value.code == "INVALID_ENVELOPE"

            scope = validate_scope("PROJECT", project_id="factlane")
            current_rows = await engine.find_contradictions(
                (await engine.get_record(current["memory_id"], scope, history=False))[0]["contradiction_key"],
                scope,
            )
            assert [row["record_id"] for row in current_rows] == [current["record_id"]]
            candidate_history = await engine.get_record(candidate["memory_id"], scope, history=True)
            assert [(row["revision"], row["lifecycle_state"]) for row in candidate_history] == [(1, "CANDIDATE")]
        finally:
            await owner.close()

    asyncio.run(run())


def test_reverify_subject_and_subject_tag_cannot_change_contradiction_identity(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "reverify-subject-identity.db"), _profile())
        await engine.open()
        owner = MemoryAdapter(
            engine,
            _Provider(),
            trusted_write_context=trusted_write_context_for_profile("owner-current"),
        )
        try:
            current = (await owner.dispatch("memory_store", {
                "fact": "The subject identity stays stable across verification refresh.",
                "scope": "PROJECT",
                "project_id": "factlane",
                "memory_type": "PROJECT_LEARNED_FACT",
                "source_provenance": {
                    "source_class": "CURRENT_REPO",
                    "source_ref": "reverify-subject",
                    "source_hash": "3" * 64,
                    "review_ref": "reverify-subject",
                    "extraction_method": "AUTOMATED_CHECK",
                },
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "reverify-subject-current",
                "requested_lifecycle_state": "VALIDATED_CURRENT",
                "source_timestamp": "2026-10-02T00:00:00Z",
                "last_verified_at": "2026-10-02T00:00:00Z",
                "verified_by": "OWNER",
                "tags": ["subject:stable-subject", "keep"],
                "subject": "stable-subject",
            }))["results"][0]

            for suffix, verification in (
                ("explicit", {"subject": "different-subject"}),
                ("tag", {"tags": ["subject:different-subject", "keep"]}),
                ("removed-tag", {"tags": ["keep"]}),
            ):
                with pytest.raises(AdapterError) as error:
                    await owner.dispatch("memory_update", {
                        "memory_id": current["memory_id"],
                        "expected_record_id": current["record_id"],
                        "scope": "PROJECT",
                        "project_id": "factlane",
                        "expected_revision": current["revision"],
                        "mode": "REVERIFY",
                        "idempotency_key": f"reverify-subject-{suffix}",
                        "verification": {
                            "source_timestamp": "2026-10-02T01:00:00Z",
                            "last_verified_at": "2026-10-02T01:00:00Z",
                            "verified_by": "OWNER",
                            **verification,
                        },
                    })
                assert error.value.code == "INVALID_ENVELOPE"

            scope = validate_scope("PROJECT", project_id="factlane")
            history = await engine.get_record(current["memory_id"], scope, history=True)
            assert [(row["revision"], row["lifecycle_state"]) for row in history] == [(1, "VALIDATED_CURRENT")]
        finally:
            await owner.close()

    asyncio.run(run())


def test_reverify_accepts_same_identity_assertions_and_unrelated_tag_refresh(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "reverify-same-identity.db"), _profile())
        await engine.open()
        owner = MemoryAdapter(
            engine,
            _Provider(),
            trusted_write_context=trusted_write_context_for_profile("owner-current"),
        )
        try:
            current = (await owner.dispatch("memory_store", {
                "fact": "Same identity assertions remain backward compatible.",
                "scope": "PROJECT",
                "project_id": "factlane",
                "memory_type": "PROJECT_LEARNED_FACT",
                "source_provenance": {
                    "source_class": "CURRENT_REPO",
                    "source_ref": "reverify-same",
                    "source_hash": "4" * 64,
                    "review_ref": "reverify-same",
                    "extraction_method": "AUTOMATED_CHECK",
                },
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "reverify-same-current",
                "requested_lifecycle_state": "VALIDATED_CURRENT",
                "source_timestamp": "2026-10-02T00:00:00Z",
                "last_verified_at": "2026-10-02T00:00:00Z",
                "verified_by": "OWNER",
                "tags": ["old-tag"],
                "subject": "stable-subject",
            }))["results"][0]
            refreshed = (await owner.dispatch("memory_update", {
                "memory_id": current["memory_id"],
                "expected_record_id": current["record_id"],
                "scope": "PROJECT",
                "project_id": "factlane",
                "expected_revision": current["revision"],
                "mode": "REVERIFY",
                "idempotency_key": "reverify-same-refresh",
                "verification": {
                    "source_timestamp": "2026-10-02T01:00:00Z",
                    "last_verified_at": "2026-10-02T01:00:00Z",
                    "verified_by": "OWNER",
                    "memory_type": "PROJECT_LEARNED_FACT",
                    "subject": "stable-subject",
                    "tags": ["subject:stable-subject", "new-tag"],
                },
            }))["results"][0]
            assert refreshed["memory_type"] == current["memory_type"]
            assert refreshed["tags"] == ["subject:stable-subject", "new-tag"]
            assert refreshed["revision"] == current["revision"] + 1
        finally:
            await owner.close()

    asyncio.run(run())


def test_reverify_preserves_legacy_non_authoritative_subject_tag_during_unrelated_tag_refresh(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "reverify-legacy-subject-tag.db"), _profile())
        await engine.open()
        owner = MemoryAdapter(
            engine,
            _Provider(),
            trusted_write_context=trusted_write_context_for_profile("owner-current"),
        )
        try:
            current = (await owner.dispatch("memory_store", {
                "fact": "Legacy explicit subject remains the contradiction identity.",
                "scope": "PROJECT",
                "project_id": "factlane",
                "memory_type": "PROJECT_LEARNED_FACT",
                "source_provenance": {
                    "source_class": "CURRENT_REPO",
                    "source_ref": "reverify-legacy-subject-tag",
                    "source_hash": "5" * 64,
                    "review_ref": "reverify-legacy-subject-tag",
                    "extraction_method": "AUTOMATED_CHECK",
                },
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "reverify-legacy-subject-tag-current",
                "requested_lifecycle_state": "VALIDATED_CURRENT",
                "source_timestamp": "2026-10-02T00:00:00Z",
                "last_verified_at": "2026-10-02T00:00:00Z",
                "verified_by": "OWNER",
                "tags": ["subject:display-alias", "old-tag"],
                "subject": "canonical-subject",
            }))["results"][0]

            refreshed = (await owner.dispatch("memory_update", {
                "memory_id": current["memory_id"],
                "expected_record_id": current["record_id"],
                "scope": "PROJECT",
                "project_id": "factlane",
                "expected_revision": current["revision"],
                "mode": "REVERIFY",
                "idempotency_key": "reverify-legacy-subject-tag-refresh",
                "verification": {
                    "source_timestamp": "2026-10-02T01:00:00Z",
                    "last_verified_at": "2026-10-02T01:00:00Z",
                    "verified_by": "OWNER",
                    "tags": ["subject:display-alias", "new-tag"],
                },
            }))["results"][0]
            assert refreshed["tags"] == ["subject:display-alias", "new-tag"]
            assert refreshed["revision"] == current["revision"] + 1

            with pytest.raises(AdapterError) as error:
                await owner.dispatch("memory_update", {
                    "memory_id": refreshed["memory_id"],
                    "expected_record_id": refreshed["record_id"],
                    "scope": "PROJECT",
                    "project_id": "factlane",
                    "expected_revision": refreshed["revision"],
                    "mode": "REVERIFY",
                    "idempotency_key": "reverify-legacy-subject-tag-second-mismatch",
                    "verification": {
                        "source_timestamp": "2026-10-02T01:30:00Z",
                        "last_verified_at": "2026-10-02T01:30:00Z",
                        "verified_by": "OWNER",
                        "tags": [
                            "subject:display-alias",
                            "subject:different-display-alias",
                            "new-tag",
                        ],
                    },
                })
            assert error.value.code == "INVALID_ENVELOPE"

            removed = (await owner.dispatch("memory_update", {
                "memory_id": refreshed["memory_id"],
                "expected_record_id": refreshed["record_id"],
                "scope": "PROJECT",
                "project_id": "factlane",
                "expected_revision": refreshed["revision"],
                "mode": "REVERIFY",
                "idempotency_key": "reverify-legacy-subject-tag-remove",
                "verification": {
                    "source_timestamp": "2026-10-02T02:00:00Z",
                    "last_verified_at": "2026-10-02T02:00:00Z",
                    "verified_by": "OWNER",
                    "tags": ["new-tag"],
                },
            }))["results"][0]
            assert removed["tags"] == ["new-tag"]

            corrected = (await owner.dispatch("memory_update", {
                "memory_id": removed["memory_id"],
                "expected_record_id": removed["record_id"],
                "scope": "PROJECT",
                "project_id": "factlane",
                "expected_revision": removed["revision"],
                "mode": "REVERIFY",
                "idempotency_key": "reverify-legacy-subject-tag-correct",
                "verification": {
                    "source_timestamp": "2026-10-02T03:00:00Z",
                    "last_verified_at": "2026-10-02T03:00:00Z",
                    "verified_by": "OWNER",
                    "tags": ["subject:canonical-subject", "new-tag"],
                },
            }))["results"][0]
            assert corrected["tags"] == ["subject:canonical-subject", "new-tag"]

            with pytest.raises(AdapterError) as error:
                await owner.dispatch("memory_update", {
                    "memory_id": corrected["memory_id"],
                    "expected_record_id": corrected["record_id"],
                    "scope": "PROJECT",
                    "project_id": "factlane",
                    "expected_revision": corrected["revision"],
                    "mode": "REVERIFY",
                    "idempotency_key": "reverify-legacy-subject-tag-drift",
                    "verification": {
                        "source_timestamp": "2026-10-02T04:00:00Z",
                        "last_verified_at": "2026-10-02T04:00:00Z",
                        "verified_by": "OWNER",
                        "tags": ["subject:different-display-alias", "new-tag"],
                    },
                })
            assert error.value.code == "INVALID_ENVELOPE"
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    [
        {"memory_type": "DECISION_RATIONALE"},
        {"fact": "A different fact cannot be promoted by REVERIFY."},
        {"contradiction_key": "9" * 64},
        {"scope": "PROJECT", "project_id": "wrong-scope"},
        {"lifecycle_state": "CANDIDATE"},
        {"authority_role": "UNRESOLVED"},
        {"verified_by": "UNVERIFIED"},
    ],
    ids=[
        "memory-type",
        "fact",
        "contradiction-key",
        "scope",
        "lifecycle",
        "authority-role",
        "verified-by",
    ],
)
def test_storage_candidate_promotion_rejects_invalid_current_successor_metadata(
    tmp_path,
    mutation: dict[str, object],
) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "promotion-identity-defense.db"), _profile())
        await engine.open()
        try:
            candidate = _record(
                lifecycle="CANDIDATE",
                marker="6",
                fact="Candidate identity is immutable during promotion.",
                created_at="2026-10-02T00:00:00Z",
            )
            await engine.write_record(candidate, [0.0, 1.0] + [0.0] * 254)
            successor = deepcopy(candidate)
            successor.update({
                "record_id": str(uuid.uuid4()),
                "revision": 2,
                "parent_record_id": candidate["record_id"],
                "source_timestamp": "2026-10-02T01:00:00Z",
                "created_at": "2026-10-02T01:00:00Z",
                "last_verified_at": "2026-10-02T01:00:00Z",
                "verified_by": "OWNER",
                "authority_role": "WORKFLOW_CURRENT",
                "lifecycle_state": "VALIDATED_CURRENT",
                "native_content_hash": "7" * 64,
                "payload_fingerprint": "8" * 64,
                "idempotency_key": "promotion-identity-defense-successor",
            })
            successor.update(mutation)
            with pytest.raises(AdapterError) as error:
                await engine.promote_candidate(
                    successor,
                    [0.0, 1.0] + [0.0] * 254,
                    validate_scope("CROSS_PROJECT_WORKFLOW"),
                    expected_record_id=str(candidate["record_id"]),
                    expected_revision=1,
                )
            assert error.value.code == "INVALID_ENVELOPE"
            history = await engine.get_record(
                str(candidate["memory_id"]),
                validate_scope("CROSS_PROJECT_WORKFLOW"),
                history=True,
            )
            assert [(row["revision"], row["lifecycle_state"]) for row in history] == [(1, "CANDIDATE")]
        finally:
            await engine.close()

    asyncio.run(run())


def test_candidate_replace_is_denied(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "replace-denied.db"), _profile())
        await engine.open()
        provider = _Provider()
        delegated = MemoryAdapter(engine, provider, trusted_write_context=trusted_write_context_for_profile("delegated-candidate"))
        owner = MemoryAdapter(engine, provider, trusted_write_context=trusted_write_context_for_profile("owner-current"))
        try:
            candidate = (await delegated.dispatch("memory_store", {
                "fact": "Candidate replacement is denied.",
                "scope": "CROSS_PROJECT_WORKFLOW",
                "memory_type": "WORKFLOW_RULE",
                "source_provenance": {"source_class": "TEST", "source_ref": "replace-denied", "source_hash": "b" * 64, "review_ref": "replace-denied", "extraction_method": "test"},
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "replace-denied-candidate",
            }))["results"][0]
            with pytest.raises(AdapterError) as error:
                await owner.dispatch("memory_update", {
                    "memory_id": candidate["memory_id"],
                    "expected_record_id": candidate["record_id"],
                    "scope": "CROSS_PROJECT_WORKFLOW",
                    "expected_revision": 1,
                    "mode": "REPLACE",
                    "idempotency_key": "replace-denied-owner",
                    "replacement": {},
                })
            assert error.value.code == "CANDIDATE_REPLACE_DENIED"
        finally:
            await owner.close()
    asyncio.run(run())


def _downgrade_adapter_schema_to_v1(db_path: str) -> dict[str, object]:
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("BEGIN IMMEDIATE")
        for trigger in (
            "factlane_v2_adapter_insert_fence",
            "factlane_v2_adapter_update_fence",
            "factlane_v2_adapter_delete_fence",
        ):
            raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for index in ("idx_adapter_scope_current", "idx_adapter_memory_id", "idx_adapter_contradiction"):
            raw.execute(f"DROP INDEX IF EXISTS {index}")
        raw.execute("ALTER TABLE adapter_records RENAME TO adapter_records_v2_source")
        raw.execute(_V1_ADAPTER_DDL)
        raw.execute(
            f"INSERT INTO adapter_records ({','.join(_V1_ADAPTER_COLUMNS)}) "
            f"SELECT {','.join(_V1_ADAPTER_COLUMNS)} FROM adapter_records_v2_source"
        )
        raw.execute("DROP TABLE adapter_records_v2_source")
        raw.execute(
            "CREATE INDEX idx_adapter_scope_current "
            "ON adapter_records(scope, project_id, worktree_id, workflow_id, agent_id, lifecycle_state)"
        )
        raw.execute("CREATE INDEX idx_adapter_memory_id ON adapter_records(memory_id, revision)")
        raw.execute("CREATE INDEX idx_adapter_contradiction ON adapter_records(contradiction_key, lifecycle_state)")
        raw.execute("DELETE FROM adapter_meta WHERE key='contract_version'")
        raw.execute("INSERT INTO adapter_meta(key,value) VALUES('contract_version','1')")
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    snapshot = _database_snapshot(db_path)
    assert snapshot["adapter_columns"] == _V1_ADAPTER_COLUMNS
    return snapshot


@pytest.mark.parametrize("checkpoint", ["after_add_field", "after_backfill"])
def test_storage_v1_to_v2_migration_failure_rolls_back_exact_v1(tmp_path, checkpoint: str) -> None:
    db_path = str(tmp_path / f"migration-{checkpoint}.db")

    async def seed_v2() -> None:
        engine = SQLiteVecEngine(db_path, _profile())
        await engine.open()
        try:
            current = _record(lifecycle="VALIDATED_CURRENT", marker="a", fact="migration survivor", created_at="2026-09-26T00:00:00Z")
            await engine.write_record(current, [0.0, 1.0] + [0.0] * 254)
        finally:
            await engine.close()

    asyncio.run(seed_v2())
    before = _downgrade_adapter_schema_to_v1(db_path)

    class FailingMigrationEngine(SQLiteVecEngine):
        def _migration_checkpoint(self, name: str) -> None:
            if name == checkpoint:
                raise RuntimeError(f"injected-{checkpoint}")

    async def attempt() -> None:
        engine = FailingMigrationEngine(db_path, _profile())
        with pytest.raises(AdapterError) as error:
            await engine.open()
        assert error.value.code == "BACKEND_UNAVAILABLE"

    asyncio.run(attempt())
    after = _database_snapshot(db_path)
    assert after == before


def test_unknown_future_storage_contract_version_fails_closed(tmp_path) -> None:
    db_path = str(tmp_path / "future-version.db")

    async def prepare() -> None:
        engine = SQLiteVecEngine(db_path, _profile())
        await engine.open()
        await engine.close()

    asyncio.run(prepare())
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE adapter_meta SET value='3' WHERE key='contract_version'")
    raw.commit()
    raw.close()

    async def reopen() -> None:
        engine = SQLiteVecEngine(db_path, _profile())
        with pytest.raises(AdapterError) as error:
            await engine.open()
        assert error.value.code == "SCHEMA_MISMATCH"

    asyncio.run(reopen())


def test_concurrent_candidate_promotion_has_exactly_one_winner(tmp_path) -> None:
    async def run() -> None:
        db_path = str(tmp_path / "promotion-race.db")
        engine_a = SQLiteVecEngine(db_path, _profile())
        engine_b = SQLiteVecEngine(db_path, _profile())
        await engine_a.open()
        await engine_b.open()
        delegated = MemoryAdapter(engine_a, _Provider(), trusted_write_context=trusted_write_context_for_profile("delegated-candidate"))
        owner_a = MemoryAdapter(engine_a, _Provider(), trusted_write_context=trusted_write_context_for_profile("owner-current"))
        owner_b = MemoryAdapter(engine_b, _Provider(), trusted_write_context=trusted_write_context_for_profile("owner-current"))
        try:
            candidate = (await delegated.dispatch("memory_store", {
                "fact": "A candidate promotion has one transactional winner.",
                "scope": "CROSS_PROJECT_WORKFLOW",
                "memory_type": "WORKFLOW_RULE",
                "source_provenance": {"source_class": "TEST", "source_ref": "race", "source_hash": "c" * 64, "review_ref": "race", "extraction_method": "test"},
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "promotion-race-candidate",
            }))["results"][0]
            async def promote(adapter: MemoryAdapter, key: str):
                return await adapter.dispatch("memory_update", {
                    "memory_id": candidate["memory_id"], "expected_record_id": candidate["record_id"],
                    "scope": "CROSS_PROJECT_WORKFLOW", "expected_revision": 1, "mode": "REVERIFY",
                    "idempotency_key": key,
                    "verification": {"source_timestamp": "2026-09-26T00:00:00Z", "verified_by": "OWNER"},
                })
            outcomes = await asyncio.gather(
                promote(owner_a, "promotion-race-a"), promote(owner_b, "promotion-race-b"),
                return_exceptions=True,
            )
            assert len([item for item in outcomes if isinstance(item, dict)]) == 1
            assert len([item for item in outcomes if isinstance(item, AdapterError) and item.code == "VERSION_CONFLICT"]) == 1
        finally:
            await owner_b.close()
            await owner_a.close()
    asyncio.run(run())


def test_concurrent_current_admission_cannot_race_past_promotion_contradiction_check(tmp_path) -> None:
    async def run() -> None:
        db_path = str(tmp_path / "current-admission-race.db")
        engine_a = SQLiteVecEngine(db_path, _profile())
        engine_b = SQLiteVecEngine(db_path, _profile())
        await engine_a.open()
        await engine_b.open()
        started = threading.Event()
        release = threading.Event()
        delegated = MemoryAdapter(
            engine_a,
            _Provider(),
            trusted_write_context=trusted_write_context_for_profile("delegated-candidate"),
        )
        owner_promote = MemoryAdapter(
            engine_a,
            _Provider(),
            trusted_write_context=trusted_write_context_for_profile("owner-current", contributor_ref="owner-promote"),
        )
        owner_store = MemoryAdapter(
            engine_b,
            _BlockingProvider(started, release),
            trusted_write_context=trusted_write_context_for_profile("owner-current", contributor_ref="owner-store"),
        )
        try:
            candidate = (await delegated.dispatch("memory_store", {
                "fact": "Candidate wins the serialized contradiction race.",
                "scope": "CROSS_PROJECT_WORKFLOW",
                "memory_type": "WORKFLOW_RULE",
                "source_provenance": {
                    "source_class": "TEST", "source_ref": "current-race-candidate", "source_hash": "5" * 64,
                    "review_ref": "current-race-candidate", "extraction_method": "test",
                },
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "current-race-candidate",
                "subject": "current-race-subject",
            }))["results"][0]

            store_task = asyncio.create_task(owner_store.dispatch("memory_store", {
                "fact": "Conflicting current store must be rejected after promotion commits.",
                "scope": "CROSS_PROJECT_WORKFLOW",
                "memory_type": "WORKFLOW_RULE",
                "source_provenance": {
                    "source_class": "OWNER_INPUT", "source_ref": "current-race-store", "source_hash": "4" * 64,
                    "review_ref": "current-race-store", "extraction_method": "direct-input",
                },
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "current-race-store",
                "requested_lifecycle_state": "VALIDATED_CURRENT",
                "source_timestamp": "2026-09-26T03:00:00Z",
                "last_verified_at": "2026-09-26T03:00:00Z",
                "verified_by": "OWNER",
                "subject": "current-race-subject",
            }))
            assert await asyncio.to_thread(started.wait, 5)

            promoted = (await owner_promote.dispatch("memory_update", {
                "memory_id": candidate["memory_id"],
                "expected_record_id": candidate["record_id"],
                "scope": "CROSS_PROJECT_WORKFLOW",
                "expected_revision": 1,
                "mode": "REVERIFY",
                "idempotency_key": "current-race-promote",
                "verification": {"source_timestamp": "2026-09-26T02:00:00Z", "verified_by": "OWNER"},
            }))["results"][0]
            release.set()

            with pytest.raises(AdapterError) as error:
                await store_task
            assert error.value.code == "CONTRADICTION"

            scope = validate_scope("CROSS_PROJECT_WORKFLOW")
            promoted_row = (await engine_a.get_record(promoted["memory_id"], scope, history=False))[0]
            conflicts = await engine_a.find_contradictions(promoted_row["contradiction_key"], scope)
            assert [row["record_id"] for row in conflicts] == [promoted["record_id"]]
        finally:
            release.set()
            await owner_store.close()
            await owner_promote.close()

    asyncio.run(run())


def test_candidate_promotion_rechecks_current_contradiction_inside_transaction(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "promotion-contradiction.db"), _profile())
        await engine.open()
        delegated = MemoryAdapter(engine, _Provider(), trusted_write_context=trusted_write_context_for_profile("delegated-candidate"))
        owner = MemoryAdapter(engine, _Provider(), trusted_write_context=trusted_write_context_for_profile("owner-current"))
        try:
            candidate = (await delegated.dispatch("memory_store", {
                "fact": "The shared workflow rule is candidate A.", "scope": "CROSS_PROJECT_WORKFLOW",
                "memory_type": "WORKFLOW_RULE",
                "source_provenance": {"source_class": "TEST", "source_ref": "contradiction", "source_hash": "d" * 64, "review_ref": "contradiction", "extraction_method": "test"},
                "freshness_policy": {"kind": "manual"}, "idempotency_key": "promotion-contradiction-candidate",
                "subject": "shared-rule",
            }))["results"][0]
            candidate_row = (await engine.get_record(candidate["memory_id"], validate_scope("CROSS_PROJECT_WORKFLOW"), history=True))[0]
            current = _record(lifecycle="VALIDATED_CURRENT", marker="e", fact="The shared workflow rule is current B.", created_at="2026-09-26T01:00:00Z")
            current["contradiction_key"] = candidate_row["contradiction_key"]
            await engine.write_record(current, [0.0, 1.0] + [0.0] * 254)
            with pytest.raises(AdapterError) as error:
                await owner.dispatch("memory_update", {
                    "memory_id": candidate["memory_id"], "expected_record_id": candidate["record_id"],
                    "scope": "CROSS_PROJECT_WORKFLOW", "expected_revision": 1, "mode": "REVERIFY",
                    "idempotency_key": "promotion-contradiction-owner",
                    "verification": {"source_timestamp": "2026-09-26T00:00:00Z", "verified_by": "OWNER"},
                })
            assert error.value.code == "CONTRADICTION"
            history = await engine.get_record(candidate["memory_id"], validate_scope("CROSS_PROJECT_WORKFLOW"), history=True)
            assert [(row["revision"], row["lifecycle_state"]) for row in history] == [(1, "CANDIDATE")]
        finally:
            await owner.close()
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["scope", "lifecycle"])
def test_write_record_rejects_invalid_superseding_successor_boundary(tmp_path, mutation: str) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / f"write-successor-{mutation}.db"), _profile())
        await engine.open()
        try:
            parent = _record(
                lifecycle="VALIDATED_CURRENT",
                marker="a",
                fact="The existing current record remains authoritative.",
                created_at="2026-09-26T00:00:00Z",
            )
            await engine.write_record(parent, [1.0] + [0.0] * 255)

            successor = _record(
                lifecycle="VALIDATED_CURRENT",
                marker="b",
                fact="A direct storage successor must preserve scope and current lifecycle.",
                created_at="2026-09-26T01:00:00Z",
            )
            successor["parent_record_id"] = parent["record_id"]
            if mutation == "scope":
                successor["scope"] = "PROJECT"
                successor["project_id"] = "other-project"
            else:
                successor["lifecycle_state"] = "CANDIDATE"

            with pytest.raises(AdapterError) as error:
                await engine.write_record(
                    successor,
                    [0.0, 1.0] + [0.0] * 254,
                    supersede_record_id=str(parent["record_id"]),
                )
            assert error.value.code == "INVALID_ENVELOPE"

            history = await engine.get_record(
                str(parent["memory_id"]),
                validate_scope("CROSS_PROJECT_WORKFLOW"),
                history=True,
            )
            assert [(row["record_id"], row["lifecycle_state"]) for row in history] == [
                (parent["record_id"], "VALIDATED_CURRENT")
            ]
        finally:
            await engine.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    ["same_memory_bad_revision", "replacement_bad_revision", "replacement_missing_supersedes"],
)
def test_write_record_rejects_invalid_superseding_lineage_shape(tmp_path, mutation: str) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / f"write-lineage-{mutation}.db"), _profile())
        await engine.open()
        try:
            parent = _record(
                lifecycle="VALIDATED_CURRENT",
                marker="c",
                fact="The current lineage parent has a valid revision shape.",
                created_at="2026-09-26T00:00:00Z",
            )
            await engine.write_record(parent, [1.0] + [0.0] * 255)

            successor = _record(
                lifecycle="VALIDATED_CURRENT",
                marker="d",
                fact="A direct storage successor must preserve a valid lineage shape.",
                created_at="2026-09-26T01:00:00Z",
            )
            successor["parent_record_id"] = parent["record_id"]
            if mutation == "same_memory_bad_revision":
                successor["memory_id"] = parent["memory_id"]
                successor["revision"] = 99
            elif mutation == "replacement_bad_revision":
                successor["revision"] = 2
                successor["supersedes"] = [parent["memory_id"]]
            else:
                successor["revision"] = 1
                successor["supersedes"] = []

            with pytest.raises(AdapterError) as error:
                await engine.write_record(
                    successor,
                    [0.0, 1.0] + [0.0] * 254,
                    supersede_record_id=str(parent["record_id"]),
                )
            assert error.value.code == "INVALID_ENVELOPE"

            history = await engine.get_record(
                str(parent["memory_id"]),
                validate_scope("CROSS_PROJECT_WORKFLOW"),
                history=True,
            )
            assert [(row["revision"], row["lifecycle_state"]) for row in history] == [
                (1, "VALIDATED_CURRENT")
            ]
        finally:
            await engine.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    ["revision", "parent_record_id", "supersedes"],
)
def test_write_record_rejects_invalid_initial_lineage_shape(tmp_path, mutation: str) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / f"write-initial-lineage-{mutation}.db"), _profile())
        await engine.open()
        try:
            record = _record(
                lifecycle="VALIDATED_CURRENT",
                marker="i",
                fact="An initial storage record must have a root lineage shape.",
                created_at="2026-10-02T05:00:00Z",
            )
            if mutation == "revision":
                record["revision"] = 7
            elif mutation == "parent_record_id":
                record["parent_record_id"] = str(uuid.uuid4())
            else:
                record["supersedes"] = [str(uuid.uuid4())]

            with pytest.raises(AdapterError) as error:
                await engine.write_record(record, [1.0] + [0.0] * 255)
            assert error.value.code == "INVALID_ENVELOPE"
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_write_record_rejects_empty_supersede_record_id_boundary(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "write-empty-supersede-id.db"), _profile())
        await engine.open()
        try:
            record = _record(
                lifecycle="VALIDATED_CURRENT",
                marker="j",
                fact="An empty supersede identifier cannot bypass root or successor lineage validation.",
                created_at="2026-10-02T05:30:00Z",
            )
            record["revision"] = 7
            record["parent_record_id"] = str(uuid.uuid4())
            record["supersedes"] = [str(uuid.uuid4())]

            with pytest.raises(AdapterError) as error:
                await engine.write_record(
                    record,
                    [1.0] + [0.0] * 255,
                    supersede_record_id="",
                )
            assert error.value.code == "VERSION_CONFLICT"
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    ["authority_role", "verified_by", "last_verified_at", "source_timestamp"],
)
def test_write_record_rejects_invalid_current_authority_metadata(tmp_path, mutation: str) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / f"write-authority-{mutation}.db"), _profile())
        await engine.open()
        try:
            parent = _record(
                lifecycle="VALIDATED_CURRENT",
                marker="e",
                fact="Validated current storage requires structurally valid authority metadata.",
                created_at="2026-09-26T00:00:00Z",
            )
            await engine.write_record(parent, [1.0] + [0.0] * 255)

            successor = _record(
                lifecycle="VALIDATED_CURRENT",
                marker="f",
                fact=parent["fact"],
                created_at="2026-09-26T01:00:00Z",
            )
            successor.update(
                {
                    "memory_id": parent["memory_id"],
                    "revision": 2,
                    "parent_record_id": parent["record_id"],
                    "memory_type": parent["memory_type"],
                    "fact": parent["fact"],
                    "contradiction_key": parent["contradiction_key"],
                }
            )
            if mutation == "authority_role":
                successor["authority_role"] = "UNRESOLVED"
            elif mutation == "verified_by":
                successor["verified_by"] = "UNVERIFIED"
            elif mutation == "last_verified_at":
                successor["last_verified_at"] = None
            else:
                successor["source_timestamp"] = None

            with pytest.raises(AdapterError) as error:
                await engine.write_record(
                    successor,
                    [0.0, 1.0] + [0.0] * 254,
                    supersede_record_id=str(parent["record_id"]),
                )
            expected_code = (
                "INVALID_ENVELOPE"
                if mutation in {"authority_role", "verified_by"}
                else "INVALID_TIMESTAMP"
            )
            assert error.value.code == expected_code

            history = await engine.get_record(
                str(parent["memory_id"]),
                validate_scope("CROSS_PROJECT_WORKFLOW"),
                history=True,
            )
            assert [
                (row["revision"], row["lifecycle_state"], row["authority_role"], row["verified_by"])
                for row in history
            ] == [
                (1, "VALIDATED_CURRENT", "WORKFLOW_CURRENT", "AUTOMATED_CHECK")
            ]
        finally:
            await engine.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"scope": "PROJECT", "project_id": None, "authority_role": "PROJECT_CURRENT"}, "UNKNOWN_PROJECT_ID"),
        (
            {"scope": "WORKFLOW", "project_id": "project-a", "workflow_id": None, "authority_role": "WORKFLOW_CURRENT"},
            "UNKNOWN_PROJECT_ID",
        ),
        ({"scope": "TOOL_ENVIRONMENT", "agent_id": None, "authority_role": "TOOL_ENV_CURRENT"}, "UNKNOWN_AGENT"),
        (
            {"scope": "CROSS_PROJECT_WORKFLOW", "project_id": "project-a", "authority_role": "WORKFLOW_CURRENT"},
            "CROSS_SCOPE_DENIED",
        ),
    ],
    ids=["project-missing-id", "workflow-missing-id", "tool-environment-missing-agent", "cross-project-carries-id"],
)
def test_write_record_rejects_invalid_current_scope_identity(
    tmp_path,
    mutation: dict[str, object],
    expected_code: str,
) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "write-invalid-current-scope.db"), _profile())
        await engine.open()
        try:
            current = _record(
                lifecycle="VALIDATED_CURRENT",
                marker="g",
                fact="Validated current storage requires a valid exact scope identity tuple.",
                created_at="2026-09-26T02:00:00Z",
            )
            current.update(mutation)

            with pytest.raises(AdapterError) as error:
                await engine.write_record(current, [1.0] + [0.0] * 255)
            assert error.value.code == expected_code
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"scope": "PROJECT", "project_id": None}, "UNKNOWN_PROJECT_ID"),
        ({"scope": "WORKFLOW", "project_id": "project-a", "workflow_id": None}, "UNKNOWN_PROJECT_ID"),
        ({"scope": "TOOL_ENVIRONMENT", "agent_id": None}, "UNKNOWN_AGENT"),
        ({"scope": "CROSS_PROJECT_WORKFLOW", "project_id": "project-a"}, "CROSS_SCOPE_DENIED"),
    ],
    ids=["project-missing-id", "workflow-missing-id", "tool-environment-missing-agent", "cross-project-carries-id"],
)
def test_write_record_rejects_invalid_candidate_scope_identity(
    tmp_path,
    mutation: dict[str, object],
    expected_code: str,
) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "write-invalid-candidate-scope.db"), _profile())
        await engine.open()
        try:
            candidate = _record(
                lifecycle="CANDIDATE",
                marker="q",
                fact="Candidate storage requires a valid exact scope identity tuple.",
                created_at="2026-10-03T05:00:00Z",
            )
            candidate.update(mutation)

            with pytest.raises(AdapterError) as error:
                await engine.write_record(candidate, [1.0] + [0.0] * 255)
            assert error.value.code == expected_code
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 0
            assert engine.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
            assert engine.conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_storage_candidate_promotion_rejects_invalid_current_scope_identity(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "promotion-invalid-current-scope.db"), _profile())
        await engine.open()
        try:
            candidate = _record(
                lifecycle="CANDIDATE",
                marker="h",
                fact="Candidate promotion requires a valid exact current scope identity tuple.",
                created_at="2026-09-26T03:00:00Z",
            )
            candidate.update({"scope": "PROJECT", "project_id": None})

            def seed_legacy_invalid_candidate() -> None:
                assert engine.conn is not None
                engine.conn.execute("BEGIN IMMEDIATE")
                try:
                    engine._insert_materialized_record(candidate, [1.0] + [0.0] * 255)
                    engine.conn.commit()
                except Exception:
                    engine.conn.rollback()
                    raise

            await engine._run(seed_legacy_invalid_candidate)

            successor = deepcopy(candidate)
            successor.update({
                "record_id": str(uuid.uuid4()),
                "revision": 2,
                "parent_record_id": candidate["record_id"],
                "source_timestamp": "2026-09-26T04:00:00Z",
                "created_at": "2026-09-26T04:00:00Z",
                "last_verified_at": "2026-09-26T04:00:00Z",
                "verified_by": "OWNER",
                "authority_role": "PROJECT_CURRENT",
                "lifecycle_state": "VALIDATED_CURRENT",
                "native_content_hash": "9" * 64,
                "payload_fingerprint": "a" * 64,
                "idempotency_key": "promotion-invalid-current-scope-successor",
            })

            with pytest.raises(AdapterError) as error:
                await engine.promote_candidate(
                    successor,
                    [0.0, 1.0] + [0.0] * 254,
                    ScopeContext("PROJECT"),
                    expected_record_id=str(candidate["record_id"]),
                    expected_revision=1,
                )
            assert error.value.code == "UNKNOWN_PROJECT_ID"
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records WHERE lifecycle_state='VALIDATED_CURRENT'").fetchone()[0] == 0
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records WHERE lifecycle_state='CANDIDATE'").fetchone()[0] == 1
        finally:
            await engine.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("lifecycle", "case", "expected_code"),
    [
        ("VALIDATED_CURRENT", "memory-type", "SCOPE_TYPE_MISMATCH"),
        ("VALIDATED_CURRENT", "ttl-freshness", "INVALID_FRESHNESS"),
        ("CANDIDATE", "provenance-fingerprint", "SCOPE_FRESHNESS_MISMATCH"),
        ("CANDIDATE", "manual-recheck", "INVALID_FRESHNESS"),
    ],
)
def test_write_record_rejects_invalid_cross_project_semantics(
    tmp_path,
    lifecycle: str,
    case: str,
    expected_code: str,
) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / f"cross-project-semantics-{case}.db"), _profile())
        await engine.open()
        try:
            marker = {"memory-type": "a", "ttl-freshness": "b", "provenance-fingerprint": "c", "manual-recheck": "d"}[case]
            record = _record(
                lifecycle=lifecycle,
                marker=marker,
                fact=f"Cross-project storage semantic admission case: {case}.",
                created_at="2026-10-03T00:00:00Z",
            )
            if case == "memory-type":
                record["memory_type"] = "USER_FACT"
            elif case == "ttl-freshness":
                record["freshness_policy"] = {
                    "kind": "ttl",
                    "ttl_seconds": 60,
                    "recheck_ref": None,
                    "source_fingerprint": None,
                }
            elif case == "provenance-fingerprint":
                provenance = dict(record["source_provenance"])
                provenance["source_fingerprint"] = marker * 64
                record["source_provenance"] = provenance
            else:
                record["freshness_policy"] = {
                    "kind": "manual",
                    "ttl_seconds": None,
                    "recheck_ref": "repo-state",
                    "source_fingerprint": None,
                }

            with pytest.raises(AdapterError) as error:
                await engine.write_record(record, [1.0] + [0.0] * 255)
            assert error.value.code == expected_code
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 0
            assert engine.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
            assert engine.conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_storage_candidate_promotion_rejects_invalid_cross_project_freshness(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "promotion-cross-project-freshness.db"), _profile())
        await engine.open()
        try:
            candidate = _record(
                lifecycle="CANDIDATE",
                marker="e",
                fact="Candidate promotion preserves cross-project semantic admission invariants.",
                created_at="2026-10-03T00:00:00Z",
            )
            await engine.write_record(candidate, [1.0] + [0.0] * 255)

            successor = deepcopy(candidate)
            successor.update({
                "record_id": str(uuid.uuid4()),
                "revision": 2,
                "parent_record_id": candidate["record_id"],
                "source_timestamp": "2026-10-03T01:00:00Z",
                "created_at": "2026-10-03T01:00:00Z",
                "last_verified_at": "2026-10-03T01:00:00Z",
                "verified_by": "OWNER",
                "authority_role": "WORKFLOW_CURRENT",
                "freshness_policy": {
                    "kind": "ttl",
                    "ttl_seconds": 60,
                    "recheck_ref": None,
                    "source_fingerprint": None,
                },
                "lifecycle_state": "VALIDATED_CURRENT",
                "native_content_hash": "f" * 64,
                "payload_fingerprint": "1" * 64,
                "idempotency_key": "promotion-cross-project-invalid-freshness",
            })

            with pytest.raises(AdapterError) as error:
                await engine.promote_candidate(
                    successor,
                    [0.0, 1.0] + [0.0] * 254,
                    validate_scope("CROSS_PROJECT_WORKFLOW"),
                    expected_record_id=str(candidate["record_id"]),
                    expected_revision=1,
                )
            assert error.value.code == "INVALID_FRESHNESS"
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records WHERE lifecycle_state='CANDIDATE'").fetchone()[0] == 1
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records WHERE lifecycle_state='VALIDATED_CURRENT'").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_write_record_accepts_valid_cross_project_on_change_semantics(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "cross-project-valid-on-change.db"), _profile())
        await engine.open()
        try:
            current = _record(
                lifecycle="VALIDATED_CURRENT",
                marker="f",
                fact="Valid cross-project on-change freshness remains admissible at the storage boundary.",
                created_at="2026-10-03T02:00:00Z",
            )
            current["freshness_policy"] = {
                "kind": "on_change",
                "ttl_seconds": None,
                "recheck_ref": "repo-state",
                "source_fingerprint": "f" * 64,
            }

            await engine.write_record(current, [1.0] + [0.0] * 255)
            rows = await engine.get_record(
                str(current["memory_id"]),
                validate_scope("CROSS_PROJECT_WORKFLOW"),
                history=False,
            )
            assert len(rows) == 1
            assert json.loads(rows[0]["freshness_policy"]) == current["freshness_policy"]
        finally:
            await engine.close()

    asyncio.run(run())


def test_cross_project_semantics_do_not_override_write_record_version_conflict(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "cross-project-version-precedence.db"), _profile())
        await engine.open()
        try:
            successor = _record(
                lifecycle="VALIDATED_CURRENT",
                marker="a",
                fact="A stale successor must fail CAS before semantic admission checks.",
                created_at="2026-10-03T03:00:00Z",
            )
            successor.update({
                "memory_type": "USER_FACT",
                "revision": 2,
                "parent_record_id": str(uuid.uuid4()),
            })

            with pytest.raises(AdapterError) as error:
                await engine.write_record(
                    successor,
                    [1.0] + [0.0] * 255,
                    supersede_record_id=str(successor["parent_record_id"]),
                )
            assert error.value.code == "VERSION_CONFLICT"
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_cross_project_semantics_do_not_override_promotion_version_conflict(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "cross-project-promotion-version-precedence.db"), _profile())
        await engine.open()
        try:
            candidate = _record(
                lifecycle="CANDIDATE",
                marker="b",
                fact="Candidate promotion keeps stale-parent CAS precedence over semantic checks.",
                created_at="2026-10-03T03:00:00Z",
            )
            await engine.write_record(candidate, [1.0] + [0.0] * 255)
            stale_parent = str(uuid.uuid4())
            successor = deepcopy(candidate)
            successor.update({
                "record_id": str(uuid.uuid4()),
                "revision": 2,
                "parent_record_id": stale_parent,
                "source_timestamp": "2026-10-03T04:00:00Z",
                "created_at": "2026-10-03T04:00:00Z",
                "last_verified_at": "2026-10-03T04:00:00Z",
                "verified_by": "OWNER",
                "authority_role": "WORKFLOW_CURRENT",
                "freshness_policy": {
                    "kind": "ttl",
                    "ttl_seconds": 60,
                    "recheck_ref": None,
                    "source_fingerprint": None,
                },
                "lifecycle_state": "VALIDATED_CURRENT",
                "native_content_hash": "c" * 64,
                "payload_fingerprint": "d" * 64,
                "idempotency_key": "cross-project-promotion-stale-parent",
            })

            with pytest.raises(AdapterError) as error:
                await engine.promote_candidate(
                    successor,
                    [0.0, 1.0] + [0.0] * 254,
                    validate_scope("CROSS_PROJECT_WORKFLOW"),
                    expected_record_id=stale_parent,
                    expected_revision=1,
                )
            assert error.value.code == "VERSION_CONFLICT"
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records WHERE lifecycle_state='CANDIDATE'").fetchone()[0] == 1
            assert engine.conn.execute("SELECT COUNT(*) FROM adapter_records WHERE lifecycle_state='VALIDATED_CURRENT'").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_equivalent_current_returns_already_current_without_promoting_candidate(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "promotion-equivalent.db"), _profile())
        await engine.open()
        delegated = MemoryAdapter(
            engine, _Provider(),
            trusted_write_context=trusted_write_context_for_profile("delegated-candidate"),
        )
        owner = MemoryAdapter(
            engine, _Provider(),
            trusted_write_context=trusted_write_context_for_profile("owner-current"),
        )
        try:
            candidate = (await delegated.dispatch("memory_store", {
                "fact": "Equivalent current doctrine remains authoritative without candidate mutation.",
                "scope": "CROSS_PROJECT_WORKFLOW",
                "memory_type": "WORKFLOW_RULE",
                "source_provenance": {
                    "source_class": "TEST", "source_ref": "equivalent", "source_hash": "f" * 64,
                    "review_ref": "equivalent", "extraction_method": "test",
                },
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "equivalent-candidate",
                "subject": "equivalent-doctrine",
            }))["results"][0]
            scope = validate_scope("CROSS_PROJECT_WORKFLOW")
            candidate_row = (await engine.get_record(candidate["memory_id"], scope, history=True))[0]
            current = _record(
                lifecycle="VALIDATED_CURRENT", marker="e",
                fact=candidate["fact"], created_at="2026-09-26T01:00:00Z",
            )
            current["contradiction_key"] = candidate_row["contradiction_key"]
            await engine.write_record(current, [0.0, 1.0] + [0.0] * 254)
            response = await owner.dispatch("memory_update", {
                "memory_id": candidate["memory_id"],
                "expected_record_id": candidate["record_id"],
                "scope": "CROSS_PROJECT_WORKFLOW",
                "expected_revision": 1,
                "mode": "REVERIFY",
                "idempotency_key": "equivalent-owner",
                "verification": {"source_timestamp": "2026-09-26T00:00:00Z", "verified_by": "OWNER"},
            })
            assert response["status"] == "ALREADY_CURRENT"
            assert response["results"][0]["record_id"] == current["record_id"]
            history = await engine.get_record(candidate["memory_id"], scope, history=True)
            assert [(row["revision"], row["lifecycle_state"]) for row in history] == [(1, "CANDIDATE")]
        finally:
            await owner.close()
    asyncio.run(run())


def test_successful_v1_to_v2_migration_preserves_materialized_state_and_backfills_legacy_origin(tmp_path) -> None:
    db_path = str(tmp_path / "migration-success.db")

    async def seed_v2() -> None:
        engine = SQLiteVecEngine(db_path, _profile())
        await engine.open()
        try:
            current = _record(
                lifecycle="VALIDATED_CURRENT",
                marker="a",
                fact="legacy migration preserves materialized state",
                created_at="2026-09-26T00:00:00Z",
            )
            await engine.write_record(current, [0.0, 1.0] + [0.0] * 254)
        finally:
            await engine.close()

    asyncio.run(seed_v2())
    before = _downgrade_adapter_schema_to_v1(db_path)

    async def migrate() -> list[dict[str, object]]:
        engine = SQLiteVecEngine(db_path, _profile())
        await engine.open()
        try:
            assert engine.conn is not None
            assert engine.conn.execute("SELECT value FROM adapter_meta WHERE key='contract_version'").fetchone()[0] == "2"
            origins = [json.loads(row[0]) for row in engine.conn.execute("SELECT contribution_origin FROM adapter_records")]
            return origins
        finally:
            await engine.close()

    origins = asyncio.run(migrate())
    after = _database_snapshot(db_path)
    assert "contribution_origin" in after["adapter_columns"]
    assert after["adapter_rows_v1"] == before["adapter_rows_v1"]
    assert after["native_columns"] == before["native_columns"]
    assert after["native_rows"] == before["native_rows"]
    assert after["vector_rows"] == before["vector_rows"]
    assert tuple(item for item in after["meta"] if item[0] != "contract_version") == tuple(
        item for item in before["meta"] if item[0] != "contract_version"
    )
    assert origins == [{"contributor_class": "LEGACY_UNKNOWN", "contributor_ref": None}]


def test_review_history_discovers_candidate_while_current_retrieval_hides_it(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "history-candidate.db"), _profile())
        await engine.open()
        adapter = MemoryAdapter(
            engine,
            _Provider(),
            trusted_write_context=trusted_write_context_for_profile("delegated-candidate"),
        )
        try:
            fact = "Candidate workflow doctrine remains hidden from normal current recall."
            candidate = (await adapter.dispatch("memory_store", {
                "fact": fact,
                "scope": "CROSS_PROJECT_WORKFLOW",
                "memory_type": "WORKFLOW_RULE",
                "source_provenance": {
                    "source_class": "TEST", "source_ref": "history-candidate", "source_hash": "9" * 64,
                    "review_ref": "history-candidate", "extraction_method": "test",
                },
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "history-candidate-store",
            }))["results"][0]
            current = await adapter.dispatch("memory_search", {
                "query": fact,
                "intent_class": "WORKFLOW_RULE",
                "scope": "CROSS_PROJECT_WORKFLOW",
                "retrieval_mode": "CURRENT",
                "retrieval_mode_kind": "EXACT",
            })
            review = await adapter.dispatch("memory_search", {
                "query": fact,
                "intent_class": "WORKFLOW_RULE",
                "scope": "CROSS_PROJECT_WORKFLOW",
                "retrieval_mode": "REVIEW_HISTORY",
                "retrieval_mode_kind": "EXACT",
            })
            assert current["results"] == []
            assert [row["record_id"] for row in review["results"]] == [candidate["record_id"]]
            assert review["results"][0]["lifecycle_state"] == "CANDIDATE"
        finally:
            await adapter.close()

    asyncio.run(run())


def test_privileged_idempotency_key_is_not_a_bearer_token_for_delegated_writer(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "idempotency-authority.db"), _profile())
        await engine.open()
        owner = MemoryAdapter(
            engine,
            _Provider(),
            trusted_write_context=trusted_write_context_for_profile("owner-current", contributor_ref="owner-a"),
        )
        delegated = MemoryAdapter(
            engine,
            _Provider(),
            trusted_write_context=trusted_write_context_for_profile("delegated-candidate", contributor_ref="delegated-a"),
        )
        request = {
            "fact": "A privileged idempotency key cannot grant current authority.",
            "scope": "CROSS_PROJECT_WORKFLOW",
            "memory_type": "WORKFLOW_RULE",
            "source_provenance": {
                "source_class": "OWNER_INPUT", "source_ref": "idempotency-authority", "source_hash": "8" * 64,
                "review_ref": "idempotency-authority", "extraction_method": "direct-input",
            },
            "freshness_policy": {"kind": "manual"},
            "idempotency_key": "privileged-idempotency-key",
            "requested_lifecycle_state": "VALIDATED_CURRENT",
            "source_timestamp": "2026-09-26T00:00:00Z",
            "last_verified_at": "2026-09-26T00:00:00Z",
            "verified_by": "OWNER",
        }
        try:
            owner_result = await owner.dispatch("memory_store", request)
            with pytest.raises(AdapterError) as error:
                await delegated.dispatch("memory_store", request)
            assert error.value.code == "WRITE_AUTHORIZATION_DENIED"
            scope = validate_scope("CROSS_PROJECT_WORKFLOW")
            rows = await engine.get_record(owner_result["results"][0]["memory_id"], scope, history=True)
            assert len(rows) == 1
            assert rows[0]["verified_by"] == "OWNER"
        finally:
            await owner.close()

    asyncio.run(run())


def test_replace_records_the_replacing_contributor_origin(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "replace-origin.db"), _profile())
        await engine.open()
        owner_context = trusted_write_context_for_profile("owner-current", contributor_ref="owner-a")
        repo_context = trusted_write_context_for_profile("repo-verifier", contributor_ref="repo-a")
        owner = MemoryAdapter(engine, _Provider(), trusted_write_context=owner_context)
        repo = MemoryAdapter(engine, _Provider(), trusted_write_context=repo_context)
        try:
            current = (await owner.dispatch("memory_store", {
                "fact": "Original current workflow doctrine.",
                "scope": "CROSS_PROJECT_WORKFLOW",
                "memory_type": "WORKFLOW_RULE",
                "source_provenance": {
                    "source_class": "OWNER_INPUT", "source_ref": "replace-origin-owner", "source_hash": "7" * 64,
                    "review_ref": "replace-origin-owner", "extraction_method": "direct-input",
                },
                "freshness_policy": {"kind": "manual"},
                "idempotency_key": "replace-origin-owner-store",
                "requested_lifecycle_state": "VALIDATED_CURRENT",
                "source_timestamp": "2026-09-26T00:00:00Z",
                "last_verified_at": "2026-09-26T00:00:00Z",
                "verified_by": "OWNER",
                "subject": "replace-origin",
            }))["results"][0]
            replaced = (await repo.dispatch("memory_update", {
                "memory_id": current["memory_id"],
                "expected_record_id": current["record_id"],
                "scope": "CROSS_PROJECT_WORKFLOW",
                "expected_revision": 1,
                "mode": "REPLACE",
                "idempotency_key": "replace-origin-repo",
                "replacement": {
                    "fact": "Replacement current workflow doctrine.",
                    "source_provenance": {
                        "source_class": "REPO", "source_ref": "replace-origin-repo", "source_hash": "6" * 64,
                        "review_ref": "replace-origin-repo", "extraction_method": "repo-check",
                    },
                    "freshness_policy": {"kind": "manual"},
                    "source_timestamp": "2026-09-26T02:00:00Z",
                    "last_verified_at": "2026-09-26T02:00:00Z",
                    "verified_by": "CURRENT_REPO_CHECK",
                    "subject": "replace-origin",
                },
            }))["results"][0]
            assert replaced["memory_id"] != current["memory_id"]
            assert replaced["contribution_origin"] == {
                "contributor_class": repo_context.contributor_class,
                "contributor_ref": "repo-a",
            }
            assert replaced["verified_by"] == "CURRENT_REPO_CHECK"
        finally:
            await repo.close()

    asyncio.run(run())
