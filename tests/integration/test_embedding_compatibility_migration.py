from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import json
import sqlite3
import struct
import uuid
from pathlib import Path

import pytest
import sqlite_vec

import factlane.adapter as adapter_module
from factlane.adapter import (
    MemoryAdapter,
    _establish_embedding_compatibility,
    trusted_write_context_for_profile,
)
from factlane.contract import AdapterError, canonical_json, validate_scope
from factlane.embedding_compatibility import ANCHOR_BUNDLE_PATH, parse_embedding_profile_metadata
from factlane.embeddings import EmbeddingProfile, OllamaLocalProvider
from factlane.storage import SQLiteVecEngine


LEGACY_DIGEST = "85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1"
NEW_DIGEST = "b" * 64


def _raw_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _profile(*, digest: str = NEW_DIGEST) -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="embeddinggemma-300m-768",
        provider_kind="OLLAMA_LOCAL",
        base_model_identity="embeddinggemma:300m",
        model_digest=digest,
        source_dimension=768,
        output_dimension=768,
        normalization_policy="OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
        distance_metric="cosine",
        projection_version="ollama-dimensions-v1",
        document_prefix="title: none | text: ",
        query_prefix="task: search result | query: ",
        semantic_family="gemma3",
        minimum_context_window=2048,
    )


def _legacy_profile_metadata() -> dict[str, object]:
    return {
        "profile_id": "embeddinggemma-300m-768",
        "provider_kind": "OLLAMA_LOCAL",
        "base_model_identity": "embeddinggemma:300m",
        "model_digest": LEGACY_DIGEST,
        "source_dimension": 768,
        "output_dimension": 768,
        "normalization_policy": "OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
        "distance_metric": "cosine",
        "projection_version": "ollama-dimensions-v1",
    }


def _record(*, fact: str = "Legacy durable fact retained across embedding migration.") -> dict[str, object]:
    marker = uuid.uuid4().hex
    return {
        "record_id": str(uuid.uuid4()),
        "memory_id": str(uuid.uuid4()),
        "revision": 1,
        "parent_record_id": None,
        "scope": "PROJECT",
        "project_id": "factlane",
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "memory_type": "PROJECT_FACT",
        "fact": fact,
        "source_provenance": {
            "source_class": "TEST",
            "source_ref": "legacy-fixture",
            "source_hash": marker * 2,
            "review_ref": "legacy-fixture",
            "extraction_method": "test",
        },
        "source_timestamp": "2026-09-30T00:00:00Z",
        "created_at": "2026-09-30T00:00:00Z",
        "last_verified_at": "2026-09-30T00:00:00Z",
        "verified_by": "CURRENT_REPO_CHECK",
        "authority_role": "PROJECT_CURRENT",
        "contribution_origin": {"contributor_class": "AUTOMATION", "contributor_ref": "legacy-fixture"},
        "freshness_policy": {
            "kind": "manual",
            "ttl_seconds": None,
            "recheck_ref": None,
            "source_fingerprint": None,
        },
        "supersedes": [],
        "contradiction_key": marker * 2,
        "contradiction_state": "NONE",
        "confidence": 1.0,
        "tags": ["legacy-fixture"],
        "lifecycle_state": "VALIDATED_CURRENT",
        "native_content_hash": marker * 2,
        "payload_fingerprint": marker[::-1] * 2,
        "idempotency_key": f"legacy-{marker}",
        "embedding_profile_id": "embeddinggemma-300m-768",
        "embedding_model_digest": LEGACY_DIGEST,
        "embedding_output_dimension": 768,
    }


def _anchor_maps() -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    bundle = json.loads(ANCHOR_BUNDLE_PATH.read_text(encoding="utf-8"))
    documents: dict[str, list[float]] = {}
    queries: dict[str, list[float]] = {}
    for anchor in bundle["anchors"]:
        raw = base64.b64decode(anchor["legacy_document_vector_f32le_base64"])
        vector = list(struct.unpack("<768f", raw))
        documents[anchor["document"]] = vector
        queries[anchor["query"]] = vector
    return documents, queries


class QualifiedProvider:
    def __init__(self) -> None:
        self.profile = _profile()
        self.documents, self.queries = _anchor_maps()
        self.document_calls = 0
        self.query_calls = 0
        self.document_inputs: list[str] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        self.document_inputs.extend(texts)
        return [self.documents[text][:] for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return self.queries[text][:]

    def provider_status(self) -> dict[str, object]:
        return _provider_status()


class NonRuntimeProvider:
    """Adapter test provider: no runtime status/anchor capability by design."""

    def __init__(self) -> None:
        self.profile = _profile()
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        return [[1.0] + [0.0] * 767 for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return [1.0] + [0.0] * 767

    def provider_status(self) -> dict[str, object]:
        return _provider_status()


class DriftOllamaProvider(OllamaLocalProvider):
    def __init__(self, *, qualified: bool) -> None:
        if qualified:
            super().__init__(
                model="embeddinggemma:300m",
                profile_id="embeddinggemma-300m-768",
                output_dimension=768,
                source_dimension=768,
                model_digest="a" * 64,
                document_prefix="title: none | text: ",
                query_prefix="task: search result | query: ",
                semantic_family="gemma3",
                minimum_context_window=2048,
            )
            self.documents, self.queries = _anchor_maps()
        else:
            super().__init__(
                model="nomic-embed-text:latest",
                profile_id="nomic-256",
                output_dimension=256,
                source_dimension=768,
                model_digest="a" * 64,
                document_prefix="search_document: ",
                query_prefix="search_query: ",
                semantic_family="nomic-bert",
                minimum_context_window=2048,
            )
            self.documents, self.queries = {}, {}
        self.current_digest = "a" * 64
        self.current_version = "0.34.2"
        self.drift_after_document_embed = False
        self.drift_after_query_embed = False

    def provider_status(self) -> dict[str, object]:
        self.profile = replace(self.profile, model_digest=self.current_digest)
        return {
            "local_only": True,
            "provider_kind": "OLLAMA_LOCAL",
            "model": self.profile.base_model_identity,
            "digest": self.current_digest,
            "ollama_version": self.current_version,
            "semantic_family": self.profile.semantic_family,
            "native_dimension": self.profile.source_dimension,
            "output_dimension": self.profile.output_dimension,
            "effective_context_window": self.profile.minimum_context_window,
        }

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        dimension = self.profile.output_dimension
        fallback = [1.0] + [0.0] * (dimension - 1)
        result = [self.documents.get(text, fallback)[:] for text in texts]
        if self.drift_after_document_embed:
            self.current_digest = "b" * 64
        return result

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        dimension = self.profile.output_dimension
        fallback = [1.0] + [0.0] * (dimension - 1)
        result = self.queries.get(text, fallback)[:]
        if self.drift_after_query_embed:
            self.current_digest = "b" * 64
        return result


class UnavailableCreateProvider(OllamaLocalProvider):
    def provider_status(self) -> dict[str, object]:
        raise AdapterError("EMBEDDING_UNAVAILABLE", "local embedding provider is unavailable")


def _provider_status() -> dict[str, object]:
    return {
        "local_only": True,
        "provider_kind": "OLLAMA_LOCAL",
        "model": "embeddinggemma:300m",
        "digest": NEW_DIGEST,
        "ollama_version": "0.34.2",
        "semantic_family": "gemma3",
        "native_dimension": 768,
        "output_dimension": 768,
        "effective_context_window": 2048,
    }


async def _seed_database(path: Path, *, install_legacy_profile: bool) -> tuple[dict[str, object], bytes]:
    seed_profile = _profile(digest=LEGACY_DIGEST)
    engine = SQLiteVecEngine(str(path), seed_profile)
    await engine.open()
    record = _record()
    vector = [1.0] + [0.0] * 767
    try:
        await engine.write_record(record, vector)
    finally:
        await engine.close()
    conn = _raw_connection(path)
    try:
        if install_legacy_profile:
            conn.execute(
                "INSERT INTO adapter_meta(key, value) VALUES (?, ?)",
                ("embedding_profile", canonical_json(_legacy_profile_metadata())),
            )
        for name, action in (
            ("factlane_v2_adapter_insert_fence", "INSERT"),
            ("factlane_v2_adapter_update_fence", "UPDATE"),
            ("factlane_v2_adapter_delete_fence", "DELETE"),
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute(
                f"CREATE TRIGGER {name} BEFORE {action} ON adapter_records "
                "WHEN factlane_contract_v2_writer() != 1 BEGIN "
                "SELECT RAISE(ABORT, 'FACTLANE_STORAGE_V2_WRITER_REQUIRED'); END"
            )
        rowid = conn.execute(
            "SELECT m.id FROM memories m JOIN adapter_records a ON a.native_content_hash=m.content_hash "
            "WHERE a.record_id=?",
            (record["record_id"],),
        ).fetchone()[0]
        vector_bytes = bytes(
            conn.execute("SELECT content_embedding FROM memory_embeddings WHERE rowid=?", (rowid,)).fetchone()[0]
        )
        conn.commit()
    finally:
        conn.close()
    return record, vector_bytes


def test_known_v013_legacy_database_migrates_metadata_only_and_fences_stale_writer(tmp_path: Path) -> None:
    async def run() -> tuple[dict[str, object], QualifiedProvider, bytes]:
        path = tmp_path / "legacy.db"
        record, before_vector = await _seed_database(path, install_legacy_profile=True)
        provider = QualifiedProvider()
        engine = SQLiteVecEngine(str(path), provider.profile)
        await engine.open()
        try:
            assert engine.embedding_compatibility_state == "UNPROVEN"
            fingerprint = await _establish_embedding_compatibility(engine, provider, _provider_status())
            assert len(fingerprint) == 64
            assert engine.embedding_compatibility_state == "COMPATIBLE"
            assert record["fact"] not in provider.document_inputs
            assert provider.document_calls == 4
            assert provider.query_calls == 4
            status = await engine.status(validate_scope("PROJECT", "factlane", None, None, None))
            assert status["embedding_compatibility"]["state"] == "COMPATIBLE"
        finally:
            await engine.close()
        return record, provider, before_vector

    record, _, before_vector = asyncio.run(run())
    path = tmp_path / "legacy.db"
    conn = _raw_connection(path)
    try:
        profile_json = conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone()[0]
        bound = parse_embedding_profile_metadata(json.loads(profile_json))
        assert bound is not None
        assert bound["store_key"] == "embeddinggemma-300m-768"
        assert bound["runtime_provenance"]["model_digest"] == NEW_DIGEST
        legacy_json = conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile_legacy_v1'").fetchone()[0]
        assert json.loads(legacy_json) == _legacy_profile_metadata()
        row = conn.execute(
            "SELECT embedding_profile_id, embedding_model_digest, embedding_output_dimension, native_content_hash "
            "FROM adapter_records WHERE record_id=?",
            (record["record_id"],),
        ).fetchone()
        assert row[:3] == ("embeddinggemma-300m-768", LEGACY_DIGEST, 768)
        rowid = conn.execute("SELECT id FROM memories WHERE content_hash=?", (row[3],)).fetchone()[0]
        after_vector = bytes(conn.execute("SELECT content_embedding FROM memory_embeddings WHERE rowid=?", (rowid,)).fetchone()[0])
        assert after_vector == before_vector
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='factlane_v2_adapter_update_fence'"
        ).fetchone()[0]
        assert "factlane_contract_v2_compat_writer" in trigger_sql
        conn.create_function("factlane_contract_v2_writer", 0, lambda: 1)
        with pytest.raises(sqlite3.OperationalError, match="no such function: factlane_contract_v2_compat_writer"):
            conn.execute("UPDATE adapter_records SET fact=fact WHERE record_id=?", (record["record_id"],))
    finally:
        conn.close()


def test_missing_legacy_profile_with_materialized_data_preserves_nonsemantic_reads_and_blocks_semantic_or_write(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "missing-profile.db"
        record, before_vector = await _seed_database(path, install_legacy_profile=False)
        engine = SQLiteVecEngine(str(path), _profile())
        await engine.open()
        provider = NonRuntimeProvider()
        adapter = MemoryAdapter(
            engine,
            provider,
            trusted_write_context=trusted_write_context_for_profile("delegated-candidate", contributor_ref="compat-test"),
        )
        try:
            assert engine.embedding_compatibility_state == "UNPROVEN"
            status = await adapter.status(scope="PROJECT", project_id="factlane")
            compatibility = status["backend_status"]["embedding_compatibility"]
            assert compatibility["state"] == "UNPROVEN"
            assert compatibility["binding"] is None
            assert compatibility["runtime_fingerprint_matches_bound"] is False
            scope = validate_scope("PROJECT", "factlane", None, None, None)
            rows = await engine.get_record(str(record["memory_id"]), scope, history=False)
            assert rows and rows[0]["fact"] == record["fact"]
            keyword = await adapter.search(
                query=str(record["fact"]),
                intent_class="CURRENT_PROJECT_STATE",
                scope="PROJECT",
                project_id="factlane",
                retrieval_mode="CURRENT",
                retrieval_mode_kind="EXACT",
            )
            assert keyword["results"][0]["fact"] == record["fact"]
            with pytest.raises(AdapterError) as semantic_error:
                await adapter.search(
                    query="legacy durable",
                    intent_class="CURRENT_PROJECT_STATE",
                    scope="PROJECT",
                    project_id="factlane",
                    retrieval_mode="CURRENT",
                    retrieval_mode_kind="SEMANTIC",
                )
            assert semantic_error.value.code == "PROFILE_MISMATCH"
            with pytest.raises(AdapterError) as write_error:
                await adapter.store(
                    fact="new candidate must not be embedded into unproven legacy space",
                    scope="PROJECT",
                    project_id="factlane",
                    memory_type="PROJECT_FACT",
                    source_provenance={
                        "source_class": "TEST",
                        "source_ref": "compat-test",
                        "source_hash": "a" * 64,
                        "review_ref": "compat-test",
                        "extraction_method": "test",
                    },
                    freshness_policy={"kind": "manual"},
                    idempotency_key="compat-missing-profile-write",
                )
            assert write_error.value.code == "PROFILE_MISMATCH"
        finally:
            await engine.close()
        conn = _raw_connection(path)
        try:
            assert conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone() is None
            row = conn.execute("SELECT id FROM memories WHERE content_hash=?", (record["native_content_hash"],)).fetchone()
            assert row is not None
            after_vector = bytes(
                conn.execute("SELECT content_embedding FROM memory_embeddings WHERE rowid=?", (row[0],)).fetchone()[0]
            )
            assert after_vector == before_vector
        finally:
            conn.close()

    asyncio.run(run())


@pytest.mark.parametrize("invalid_profile", ["{not-json", "[]"])
def test_invalid_profile_metadata_preserves_nonsemantic_reads_and_blocks_semantic_or_write(
    tmp_path: Path,
    invalid_profile: str,
) -> None:
    async def run() -> None:
        path = tmp_path / "invalid-profile.db"
        record, before_vector = await _seed_database(path, install_legacy_profile=True)
        conn = _raw_connection(path)
        try:
            conn.execute(
                "UPDATE adapter_meta SET value=? WHERE key='embedding_profile'",
                (invalid_profile,),
            )
            conn.commit()
        finally:
            conn.close()

        engine = SQLiteVecEngine(str(path), _profile())
        await engine.open()
        provider = NonRuntimeProvider()
        adapter = MemoryAdapter(
            engine,
            provider,
            trusted_write_context=trusted_write_context_for_profile(
                "delegated-candidate", contributor_ref="invalid-profile-test"
            ),
        )
        try:
            assert engine.embedding_compatibility_state == "UNPROVEN"
            assert engine.embedding_profile_metadata_invalid is True
            status = await adapter.status(scope="PROJECT", project_id="factlane")
            compatibility = status["backend_status"]["embedding_compatibility"]
            assert compatibility["state"] == "UNPROVEN"
            assert compatibility["binding"] is None
            assert "invalid" in str(compatibility["reason"])

            got = await adapter.get(
                memory_id=str(record["memory_id"]),
                scope="PROJECT",
                project_id="factlane",
                retrieval_mode="CURRENT",
            )
            assert got["results"][0]["fact"] == record["fact"]
            exact = await adapter.search(
                query=str(record["fact"]),
                intent_class="CURRENT_PROJECT_STATE",
                scope="PROJECT",
                project_id="factlane",
                retrieval_mode="CURRENT",
                retrieval_mode_kind="EXACT",
            )
            assert exact["results"][0]["fact"] == record["fact"]
            keyword = await adapter.search(
                query="durable",
                intent_class="CURRENT_PROJECT_STATE",
                scope="PROJECT",
                project_id="factlane",
                retrieval_mode="CURRENT",
                retrieval_mode_kind="KEYWORD",
            )
            assert keyword["results"]
            with pytest.raises(AdapterError) as semantic_error:
                await adapter.search(
                    query="legacy durable",
                    intent_class="CURRENT_PROJECT_STATE",
                    scope="PROJECT",
                    project_id="factlane",
                    retrieval_mode="CURRENT",
                    retrieval_mode_kind="SEMANTIC",
                )
            assert semantic_error.value.code == "PROFILE_MISMATCH"
            with pytest.raises(AdapterError) as write_error:
                await adapter.store(
                    fact="invalid metadata must not be silently relabeled",
                    scope="PROJECT",
                    project_id="factlane",
                    memory_type="PROJECT_FACT",
                    source_provenance={
                        "source_class": "TEST",
                        "source_ref": "invalid-profile-test",
                        "source_hash": "f" * 64,
                        "review_ref": "invalid-profile-test",
                        "extraction_method": "test",
                    },
                    freshness_policy={"kind": "manual"},
                    idempotency_key="invalid-profile-write",
                )
            assert write_error.value.code == "PROFILE_MISMATCH"
        finally:
            await engine.close()

        conn = _raw_connection(path)
        try:
            assert conn.execute(
                "SELECT value FROM adapter_meta WHERE key='embedding_profile'"
            ).fetchone()[0] == invalid_profile
            rowid = conn.execute(
                "SELECT id FROM memories WHERE content_hash=?",
                (record["native_content_hash"],),
            ).fetchone()[0]
            after_vector = bytes(
                conn.execute("SELECT content_embedding FROM memory_embeddings WHERE rowid=?", (rowid,)).fetchone()[0]
            )
            assert after_vector == before_vector
        finally:
            conn.close()

    asyncio.run(run())


def test_legacy_provenance_mismatch_fails_closed_without_relabeling(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "mixed-provenance.db"
        _, _ = await _seed_database(path, install_legacy_profile=True)
        conn = _raw_connection(path)
        try:
            conn.create_function("factlane_contract_v2_writer", 0, lambda: 1)
            conn.execute("UPDATE adapter_records SET embedding_model_digest=?", ("c" * 64,))
            conn.commit()
        finally:
            conn.close()
        provider = QualifiedProvider()
        engine = SQLiteVecEngine(str(path), provider.profile)
        await engine.open()
        try:
            await _establish_embedding_compatibility(engine, provider, _provider_status())
            assert engine.embedding_compatibility_state == "INCOMPATIBLE"
            assert provider.document_calls == 0
            assert provider.query_calls == 0
        finally:
            await engine.close()
            conn = _raw_connection(path)
        try:
            assert json.loads(conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone()[0]) == _legacy_profile_metadata()
            assert conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile_legacy_v1'").fetchone() is None
        finally:
            conn.close()

    asyncio.run(run())


def test_runtime_digest_drift_requalifies_known_space_and_preserves_per_record_provenance(tmp_path: Path) -> None:
    async def run() -> None:
        provider = DriftOllamaProvider(qualified=True)
        path = tmp_path / "runtime-drift-qualified.db"
        engine = SQLiteVecEngine(str(path), provider.profile)
        await engine.open()
        initial_status = provider.provider_status()
        fingerprint = await _establish_embedding_compatibility(engine, provider, initial_status)
        adapter = MemoryAdapter(
            engine,
            provider,
            trusted_write_context=trusted_write_context_for_profile(
                "delegated-candidate", contributor_ref="runtime-drift-test"
            ),
            embedding_runtime_fingerprint=fingerprint,
        )
        async def store(key: str, fact: str) -> dict[str, object]:
            result = await adapter.store(
                fact=fact,
                scope="PROJECT",
                project_id="factlane",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance={
                    "source_class": "TEST",
                    "source_ref": key,
                    "source_hash": (key[0] * 64),
                    "review_ref": "runtime-drift-test",
                    "extraction_method": "test",
                },
                freshness_policy={"kind": "manual"},
                idempotency_key=key,
            )
            return result["results"][0]
        try:
            first = await store("a-first-runtime", "fact created before compatible runtime digest drift")
            provider.current_digest = "b" * 64
            second = await store("b-second-runtime", "fact created after compatible runtime digest drift")
            assert engine.embedding_compatibility_state == "COMPATIBLE"
            assert adapter._embedding_runtime_fingerprint != fingerprint
            first_rows = await engine.get_record(str(first["memory_id"]), validate_scope("PROJECT", "factlane", None, None, None), history=True)
            second_rows = await engine.get_record(str(second["memory_id"]), validate_scope("PROJECT", "factlane", None, None, None), history=True)
            assert first_rows[0]["embedding_model_digest"] == "a" * 64
            assert second_rows[0]["embedding_model_digest"] == "b" * 64
            assert engine.embedding_profile_metadata is not None
            assert engine.embedding_profile_metadata["runtime_provenance"]["model_digest"] == "b" * 64
        finally:
            await adapter.close()

    asyncio.run(run())


def test_store_discards_embedding_when_runtime_drifts_in_flight(tmp_path: Path) -> None:
    async def run() -> None:
        provider = DriftOllamaProvider(qualified=True)
        path = tmp_path / "runtime-drift-inflight-store.db"
        engine = SQLiteVecEngine(str(path), provider.profile)
        await engine.open()
        initial_status = provider.provider_status()
        fingerprint = await _establish_embedding_compatibility(engine, provider, initial_status)
        adapter = MemoryAdapter(
            engine,
            provider,
            trusted_write_context=trusted_write_context_for_profile(
                "delegated-candidate", contributor_ref="runtime-drift-inflight-store"
            ),
            embedding_runtime_fingerprint=fingerprint,
        )
        provider.drift_after_document_embed = True
        try:
            with pytest.raises(AdapterError) as error:
                await adapter.store(
                    fact="in-flight runtime drift must discard this candidate embedding",
                    scope="PROJECT",
                    project_id="factlane",
                    memory_type="PROJECT_LEARNED_FACT",
                    source_provenance={
                        "source_class": "TEST",
                        "source_ref": "runtime-drift-inflight-store",
                        "source_hash": "d" * 64,
                        "review_ref": "runtime-drift-test",
                        "extraction_method": "test",
                    },
                    freshness_policy={"kind": "manual"},
                    idempotency_key="runtime-drift-inflight-store",
                )
            assert error.value.code == "PROFILE_MISMATCH"
            assert engine.embedding_compatibility_state == "UNPROVEN"
            assert provider.current_digest == "b" * 64
            snapshot = await engine.compatibility_snapshot()
            assert snapshot["adapter_count"] == 0
            assert snapshot["memory_count"] == 0
            assert snapshot["vector_count"] == 0
        finally:
            await adapter.close()

    asyncio.run(run())


def test_semantic_search_discards_query_vector_when_runtime_drifts_in_flight(tmp_path: Path) -> None:
    async def run() -> None:
        provider = DriftOllamaProvider(qualified=True)
        path = tmp_path / "runtime-drift-inflight-query.db"
        engine = SQLiteVecEngine(str(path), provider.profile)
        await engine.open()
        initial_status = provider.provider_status()
        fingerprint = await _establish_embedding_compatibility(engine, provider, initial_status)
        adapter = MemoryAdapter(
            engine,
            provider,
            trusted_write_context=trusted_write_context_for_profile(
                "delegated-candidate", contributor_ref="runtime-drift-inflight-query"
            ),
            embedding_runtime_fingerprint=fingerprint,
        )
        try:
            stored = await adapter.store(
                fact="stable fact retained while a later query runtime drifts",
                scope="PROJECT",
                project_id="factlane",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance={
                    "source_class": "TEST",
                    "source_ref": "runtime-drift-inflight-query",
                    "source_hash": "e" * 64,
                    "review_ref": "runtime-drift-test",
                    "extraction_method": "test",
                },
                freshness_policy={"kind": "manual"},
                idempotency_key="runtime-drift-inflight-query-seed",
            )
            assert stored["results"]
            provider.drift_after_query_embed = True
            with pytest.raises(AdapterError) as error:
                await adapter.search(
                    query="stable fact",
                    intent_class="CURRENT_PROJECT_STATE",
                    scope="PROJECT",
                    project_id="factlane",
                    retrieval_mode="CURRENT",
                    retrieval_mode_kind="SEMANTIC",
                )
            assert error.value.code == "PROFILE_MISMATCH"
            assert engine.embedding_compatibility_state == "UNPROVEN"
            assert provider.current_digest == "b" * 64
        finally:
            await adapter.close()

    asyncio.run(run())


def test_anchor_qualification_does_not_bind_when_runtime_drifts_in_flight(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "runtime-drift-inflight-anchor.db"
        await _seed_database(path, install_legacy_profile=True)
        provider = DriftOllamaProvider(qualified=True)
        provider.drift_after_document_embed = True
        engine = SQLiteVecEngine(str(path), provider.profile)
        await engine.open()
        initial_status = provider.provider_status()
        try:
            await _establish_embedding_compatibility(engine, provider, initial_status)
            assert engine.embedding_compatibility_state == "UNPROVEN"
            assert provider.current_digest == "b" * 64
        finally:
            await engine.close()
        conn = _raw_connection(path)
        try:
            assert json.loads(conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone()[0]) == _legacy_profile_metadata()
            assert conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile_legacy_v1'").fetchone() is None
        finally:
            conn.close()

    asyncio.run(run())


def test_runtime_drift_without_qualified_anchor_fails_closed_before_write(tmp_path: Path) -> None:
    async def run() -> None:
        provider = DriftOllamaProvider(qualified=False)
        path = tmp_path / "runtime-drift-unqualified.db"
        engine = SQLiteVecEngine(str(path), provider.profile)
        await engine.open()
        initial_status = provider.provider_status()
        fingerprint = await _establish_embedding_compatibility(engine, provider, initial_status)
        adapter = MemoryAdapter(
            engine,
            provider,
            trusted_write_context=trusted_write_context_for_profile(
                "delegated-candidate", contributor_ref="runtime-drift-test"
            ),
            embedding_runtime_fingerprint=fingerprint,
        )
        provider.current_digest = "b" * 64
        try:
            with pytest.raises(AdapterError) as error:
                await adapter.store(
                    fact="write must fail after unqualified runtime drift",
                    scope="PROJECT",
                    project_id="factlane",
                    memory_type="PROJECT_LEARNED_FACT",
                    source_provenance={
                        "source_class": "TEST",
                        "source_ref": "runtime-drift-unqualified",
                        "source_hash": "c" * 64,
                        "review_ref": "runtime-drift-test",
                        "extraction_method": "test",
                    },
                    freshness_policy={"kind": "manual"},
                    idempotency_key="runtime-drift-unqualified",
                )
            assert error.value.code == "PROFILE_MISMATCH"
            assert engine.embedding_compatibility_state == "UNPROVEN"
            assert provider.document_calls == 0
            assert engine.embedding_profile_metadata is not None
            assert engine.embedding_profile_metadata["runtime_provenance"]["model_digest"] == "a" * 64
        finally:
            await adapter.close()

    asyncio.run(run())


def test_promote_candidate_requires_embedding_compatibility_before_mutation(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "promotion-unproven.db"
        await _seed_database(path, install_legacy_profile=True)
        engine = SQLiteVecEngine(str(path), _profile())
        await engine.open()
        try:
            before = await engine.compatibility_snapshot()
            assert engine.embedding_compatibility_state == "UNPROVEN"
            with pytest.raises(AdapterError) as error:
                await engine.promote_candidate(
                    {},
                    [1.0] + [0.0] * 767,
                    validate_scope("PROJECT", "factlane", None, None, None),
                    expected_record_id=str(uuid.uuid4()),
                    expected_revision=1,
                )
            assert error.value.code == "PROFILE_MISMATCH"
            after = await engine.compatibility_snapshot()
            assert after == before
        finally:
            await engine.close()

    asyncio.run(run())


def test_embedding_profile_binding_failure_rolls_back_legacy_metadata_and_writer_fence(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "binding-rollback.db"
        record, before_vector = await _seed_database(path, install_legacy_profile=True)

        class FailingBindingEngine(SQLiteVecEngine):
            def _migration_checkpoint(self, name: str) -> None:
                if name == "after_embedding_writer_fence":
                    raise RuntimeError("injected-binding-failure")

        provider = QualifiedProvider()
        engine = FailingBindingEngine(str(path), provider.profile)
        await engine.open()
        try:
            with pytest.raises(RuntimeError, match="injected-binding-failure"):
                await _establish_embedding_compatibility(engine, provider, _provider_status())
        finally:
            await engine.close()

        conn = _raw_connection(path)
        try:
            profile_json = conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone()[0]
            assert json.loads(profile_json) == _legacy_profile_metadata()
            assert conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile_legacy_v1'").fetchone() is None
            trigger_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='factlane_v2_adapter_update_fence'"
            ).fetchone()[0]
            assert "factlane_contract_v2_writer" in trigger_sql
            assert "factlane_contract_v2_compat_writer" not in trigger_sql
            row = conn.execute(
                "SELECT native_content_hash, embedding_model_digest FROM adapter_records WHERE record_id=?",
                (record["record_id"],),
            ).fetchone()
            assert row[1] == LEGACY_DIGEST
            rowid = conn.execute("SELECT id FROM memories WHERE content_hash=?", (row[0],)).fetchone()[0]
            after_vector = bytes(
                conn.execute("SELECT content_embedding FROM memory_embeddings WHERE rowid=?", (rowid,)).fetchone()[0]
            )
            assert after_vector == before_vector
        finally:
            conn.close()

    asyncio.run(run())


def test_existing_legacy_database_opens_degraded_when_provider_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        path = tmp_path / "provider-unavailable-existing.db"
        record, before_vector = await _seed_database(path, install_legacy_profile=False)
        monkeypatch.setattr(adapter_module, "OllamaLocalProvider", UnavailableCreateProvider)
        adapter = await MemoryAdapter.create(
            str(path),
            "embeddinggemma-300m-768",
            trusted_write_context=trusted_write_context_for_profile("read-only"),
        )
        try:
            status = await adapter.status(scope="PROJECT", project_id="factlane")
            compatibility = status["backend_status"]["embedding_compatibility"]
            assert compatibility["state"] == "UNPROVEN"
            assert compatibility["binding"] is None
            assert compatibility["runtime_fingerprint_matches_bound"] is False
            assert status["provider_status"] == {
                "available": False,
                "error_code": "EMBEDDING_UNAVAILABLE",
                "message": "local embedding provider is unavailable",
            }
            got = await adapter.get(
                memory_id=str(record["memory_id"]),
                scope="PROJECT",
                project_id="factlane",
                retrieval_mode="CURRENT",
            )
            assert got["results"][0]["fact"] == record["fact"]
            exact = await adapter.search(
                query=str(record["fact"]),
                intent_class="CURRENT_PROJECT_STATE",
                scope="PROJECT",
                project_id="factlane",
                retrieval_mode="CURRENT",
                retrieval_mode_kind="EXACT",
            )
            assert exact["results"][0]["fact"] == record["fact"]
            with pytest.raises(AdapterError) as semantic_error:
                await adapter.search(
                    query="legacy durable",
                    intent_class="CURRENT_PROJECT_STATE",
                    scope="PROJECT",
                    project_id="factlane",
                    retrieval_mode="CURRENT",
                    retrieval_mode_kind="SEMANTIC",
                )
            assert semantic_error.value.code == "PROFILE_MISMATCH"
        finally:
            await adapter.close()

        conn = _raw_connection(path)
        try:
            assert conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone() is None
            rowid = conn.execute("SELECT id FROM memories WHERE content_hash=?", (record["native_content_hash"],)).fetchone()[0]
            after_vector = bytes(
                conn.execute("SELECT content_embedding FROM memory_embeddings WHERE rowid=?", (rowid,)).fetchone()[0]
            )
            assert after_vector == before_vector
        finally:
            conn.close()

    asyncio.run(run())


def test_fresh_database_provider_unavailable_fails_before_database_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        path = tmp_path / "provider-unavailable-fresh.db"
        monkeypatch.setattr(adapter_module, "OllamaLocalProvider", UnavailableCreateProvider)
        with pytest.raises(AdapterError) as error:
            await MemoryAdapter.create(
                str(path),
                "embeddinggemma-300m-768",
                trusted_write_context=trusted_write_context_for_profile("read-only"),
            )
        assert error.value.code == "EMBEDDING_UNAVAILABLE"
        assert path.exists() is False

    asyncio.run(run())
