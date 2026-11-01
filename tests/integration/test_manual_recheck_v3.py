from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest

import factlane.storage as storage_module
from factlane.adapter import MemoryAdapter, trusted_write_context_for_profile
from factlane.contract import AdapterError, validate_scope
from factlane.embeddings import EmbeddingProfile
from factlane.storage import SQLiteVecEngine


SOURCE_HASH = "a" * 64


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="manual-recheck-test-256",
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
        return {
            "available": True,
            "local_only": True,
            "profile_id": self.profile.profile_id,
            "provider_kind": self.profile.provider_kind,
            "model_digest": self.profile.model_digest,
            "output_dimension": self.profile.output_dimension,
        }


def _current(*, last_verified_at: str = "2026-10-01T00:00:00Z") -> dict[str, object]:
    record_id = str(uuid.uuid4())
    return {
        "record_id": record_id,
        "memory_id": str(uuid.uuid4()),
        "revision": 1,
        "parent_record_id": None,
        "scope": "PROJECT",
        "project_id": "manual-project",
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "memory_type": "PROJECT_LEARNED_FACT",
        "fact": "The governed source remains unchanged.",
        "source_provenance": {
            "source_class": "CURRENT_REPO",
            "source_ref": "repo:file.md",
            "source_hash": SOURCE_HASH,
            "review_ref": "initial",
            "extraction_method": "manual",
        },
        "source_timestamp": last_verified_at,
        "created_at": last_verified_at,
        "last_verified_at": last_verified_at,
        "verified_by": "OWNER",
        "authority_role": "PROJECT_CURRENT",
        "contribution_origin": {"contributor_class": "OWNER", "contributor_ref": "owner-test"},
        "freshness_policy": {
            "kind": "on_change",
            "ttl_seconds": None,
            "recheck_ref": "source:repo:file.md",
            "source_fingerprint": SOURCE_HASH,
        },
        "supersedes": [],
        "contradiction_key": "manual-recheck-key",
        "contradiction_state": "NONE",
        "confidence": 1.0,
        "tags": [],
        "lifecycle_state": "VALIDATED_CURRENT",
        "native_content_hash": "b" * 64,
        "payload_fingerprint": "c" * 64,
        "idempotency_key": f"seed-{record_id}",
        "embedding_profile_id": _profile().profile_id,
        "embedding_model_digest": _profile().model_digest,
        "embedding_output_dimension": 256,
    }


def _fingerprint(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


async def _seed(engine: SQLiteVecEngine, record: dict[str, object]) -> None:
    await engine.write_record(record, [1.0] + [0.0] * 255)
    # These tests construct historical timelines directly through the storage
    # fixture. Reset the global audit clock to the fixture's historical server
    # time so later monkeypatched samples exercise the intended boundary.
    assert engine.conn is not None
    engine.conn.execute(
        "UPDATE adapter_meta SET value=? WHERE key='trust_clock_high_water_at'",
        (str(record["created_at"]),),
    )
    engine.conn.commit()


def test_non_recheck_governed_write_advances_global_trust_clock(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "governed-write-clock.db"), _profile())
        await engine.open()
        try:
            record = _current(last_verified_at="2026-10-31T18:00:00Z")
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T21:00:00Z")
            await engine.write_record(record, [1.0] + [0.0] * 255)
            assert engine.conn is not None
            assert engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
            ).fetchone()[0] == "2026-10-31T21:00:00Z"

            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:00:00Z")
            blocked = await _recheck(
                engine,
                record,
                key="governed-write-clock-rollback",
                observed_at="2026-10-31T19:30:00Z",
            )
            assert blocked["status"] == "BLOCKED"
            assert blocked["error"] == "TRUST_CLOCK_UNRELIABLE"
            assert engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
            ).fetchone()[0] == "2026-10-31T21:00:00Z"
        finally:
            await engine.close()

    asyncio.run(run())


async def _token(engine: SQLiteVecEngine, record: dict[str, object]) -> str:
    scope = validate_scope("PROJECT", project_id="manual-project")
    return (await engine.group_observation(scope, str(record["contradiction_key"])))["public"]["observed_group_token"]


async def _recheck(
    engine: SQLiteVecEngine,
    record: dict[str, object],
    *,
    key: str,
    observed_at: str,
    claim: str = "OBSERVED_HASH",
    observed_hash: str | None = SOURCE_HASH,
    request_id: str | None = None,
) -> dict[str, object]:
    scope = validate_scope("PROJECT", project_id="manual-project")
    return await engine.manual_recheck(
        scope,
        memory_id=str(record["memory_id"]),
        expected_record_id=str(record["record_id"]),
        expected_revision=int(record["revision"]),
        observed_group_token=await _token(engine, record),
        idempotency_key=key,
        request_fingerprint=_fingerprint(key),
        request_id=request_id or f"request-{key}",
        claim=claim,
        recheck_ref="source:repo:file.md",
        observed_at=observed_at,
        observed_source_hash=observed_hash,
        evidence_ref_digest=_fingerprint(f"evidence-{key}"),
        method="OWNER_HASHED_SOURCE",
        actor_class="OWNER",
        actor_ref_digest=_fingerprint("owner-test"),
        grant_ref_digest=_fingerprint("grant-test"),
    )


def test_fresh_unchanged_creates_successor_with_evidence_time_and_exact_generations(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "fresh.db"), _profile())
        await engine.open()
        try:
            record = _current(last_verified_at="2026-10-31T18:00:00Z")
            await _seed(engine, record)
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:00:00Z")
            result = await _recheck(engine, record, key="fresh-match", observed_at="2026-10-31T19:30:00Z")
            assert result["status"] == "OK"
            assert result["outcome"] == "UNCHANGED"
            assert result["accepted_at"] == "2026-10-31T20:00:00Z"
            assert result["observed_at"] == "2026-10-31T19:30:00Z"
            assert result["accepted_event_freshness_generation"] == 1
            assert result["resulting_member_freshness_generation"] == 0
            assert result["resulting_record_id"] != record["record_id"]
            assert result["resulting_revision"] == 2
            scope = validate_scope("PROJECT", project_id="manual-project")
            history = await engine.get_record(str(record["memory_id"]), scope, history=True)
            assert history[0]["record_id"] == result["resulting_record_id"]
            assert history[0]["last_verified_at"] == "2026-10-31T19:30:00Z"
            assert history[0]["lifecycle_state"] == "VALIDATED_CURRENT"
            assert history[1]["lifecycle_state"] == "SUPERSEDED"
            event = engine.conn.execute(
                "SELECT freshness_outcome,observed_at,accepted_at,freshness_generation,evidence_ref_digest "
                "FROM lifecycle_events WHERE action='MANUAL_RECHECK'"
            ).fetchone()
            assert event[:4] == ("UNCHANGED", "2026-10-31T19:30:00Z", "2026-10-31T20:00:00Z", 1)
            assert event[4] == _fingerprint("evidence-fresh-match")
            assert SOURCE_HASH not in "|".join(str(value) for value in event if value is not None)
        finally:
            await engine.close()

    asyncio.run(run())


def test_old_but_newer_matching_evidence_after_hold_is_durably_blocked_and_hold_stays(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "old-match.db"), _profile())
        await engine.open()
        try:
            record = _current(last_verified_at="2026-10-01T00:00:00Z")
            await _seed(engine, record)
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-02T12:00:00Z")
            adverse = await _recheck(
                engine,
                record,
                key="adverse-oct2",
                observed_at="2026-10-02T10:00:00Z",
                observed_hash="d" * 64,
            )
            assert adverse["outcome"] == "CHANGED"
            assert adverse["freshness_gate_state"] == "SOURCE_CHANGE_REVIEW_REQUIRED"
            held_token = adverse["resulting_group_token"]

            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T12:00:00Z")
            blocked = await _recheck(
                engine,
                record,
                key="old-match-oct3",
                observed_at="2026-10-03T10:00:00Z",
            )
            assert blocked["status"] == "BLOCKED"
            assert blocked["error"] == "EVIDENCE_TOO_OLD_FOR_TRUST_RENEWAL"
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 1
            assert await _token(engine, record) == held_token
            scope = validate_scope("PROJECT", project_id="manual-project")
            history = await engine.get_record(str(record["memory_id"]), scope, history=True)
            assert len(history) == 1
            assert history[0]["record_id"] == record["record_id"]

            original_decision_at = blocked["decision_at"]
            await engine.close()
            reopened = SQLiteVecEngine(str(tmp_path / "old-match.db"), _profile())
            await reopened.open()
            try:
                monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-11-01T12:00:00Z")
                replay = await _recheck(
                    reopened,
                    record,
                    key="old-match-oct3",
                    observed_at="2026-10-03T10:00:00Z",
                    request_id="changed-request-id-does-not-replace-stored-one",
                )
                assert replay["status"] == "BLOCKED"
                assert replay["error"] == "EVIDENCE_TOO_OLD_FOR_TRUST_RENEWAL"
                assert replay["decision_at"] == original_decision_at
                assert replay["request_id"] == "request-old-match-oct3"
                assert replay["idempotent_replay"] is True
                assert reopened.conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 1
            finally:
                await reopened.close()
        finally:
            if engine.conn is not None:
                await engine.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("age_seconds", "expected_status", "expected_error"),
    [
        (86_400, "OK", None),
        (86_401, "BLOCKED", "EVIDENCE_TOO_OLD_FOR_TRUST_RENEWAL"),
    ],
)
def test_trust_renewal_age_boundary(tmp_path, monkeypatch, age_seconds, expected_status, expected_error) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / f"boundary-{age_seconds}.db"), _profile())
        await engine.open()
        try:
            record = _current(last_verified_at="2026-10-29T00:00:00Z")
            await _seed(engine, record)
            observed_at = "2026-10-30T12:00:00Z"
            accepted = "2026-10-31T12:00:00Z" if age_seconds == 86_400 else "2026-10-31T12:00:01Z"
            monkeypatch.setattr(storage_module, "iso_now", lambda: accepted)
            result = await _recheck(engine, record, key=f"boundary-{age_seconds}", observed_at=observed_at)
            assert result["status"] == expected_status
            if expected_error:
                assert result["error"] == expected_error
            else:
                assert result["outcome"] == "UNCHANGED"
        finally:
            await engine.close()

    asyncio.run(run())


def test_future_matching_within_skew_does_not_renew_trust(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "future.db"), _profile())
        await engine.open()
        try:
            record = _current(last_verified_at="2026-10-31T18:00:00Z")
            await _seed(engine, record)
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:00:00Z")
            result = await _recheck(engine, record, key="future-match", observed_at="2026-10-31T20:02:00Z")
            assert result["status"] == "BLOCKED"
            assert result["error"] == "EVIDENCE_NOT_YET_OBSERVED"
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_future_matching_beyond_skew_is_not_durable_or_idempotency_reserved(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "future-invalid.db"), _profile())
        await engine.open()
        try:
            record = _current(last_verified_at="2026-10-31T18:00:00Z")
            await _seed(engine, record)
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:00:00Z")
            with pytest.raises(AdapterError) as exc:
                await _recheck(engine, record, key="future-invalid", observed_at="2026-10-31T20:05:01Z")
            assert exc.value.code == "EVIDENCE_INVALID"
            assert engine.conn is not None
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_operations").fetchone()[0] == 0
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 0
            corrected = await _recheck(engine, record, key="future-invalid", observed_at="2026-10-31T20:02:00Z")
            assert corrected["status"] == "BLOCKED"
            assert corrected["error"] == "EVIDENCE_NOT_YET_OBSERVED"
        finally:
            await engine.close()

    asyncio.run(run())


def test_clock_rollback_blocks_unchanged_but_commits_causally_newer_adverse_with_generation(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "rollback.db"), _profile())
        await engine.open()
        try:
            record = _current(last_verified_at="2026-10-31T10:00:00Z")
            await _seed(engine, record)
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T12:00:00Z")
            first = await _recheck(
                engine,
                record,
                key="rollback-first",
                observed_at="2026-10-31T11:50:00Z",
                observed_hash="e" * 64,
            )
            assert first["accepted_event_freshness_generation"] == 1
            token1 = first["resulting_group_token"]

            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T11:00:00Z")
            second = await _recheck(
                engine,
                record,
                key="rollback-second",
                observed_at="2026-10-31T11:51:00Z",
                claim="UNAVAILABLE",
                observed_hash=None,
            )
            assert second["outcome"] == "UNAVAILABLE"
            assert second["accepted_event_freshness_generation"] == 2
            assert second["resulting_group_token"] != token1
            anomaly = engine.conn.execute(
                "SELECT clock_anomaly FROM lifecycle_events WHERE freshness_generation=2"
            ).fetchone()[0]
            assert anomaly == "CLOCK_UNTRUSTED_TIME"
            highwater = engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
            ).fetchone()[0]
            assert highwater == "2026-10-31T12:00:00Z"

            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T11:02:00Z")
            blocked = await _recheck(
                engine,
                record,
                key="rollback-match",
                observed_at="2026-10-31T11:52:00Z",
            )
            assert blocked["status"] == "BLOCKED"
            assert blocked["error"] == "TRUST_CLOCK_UNRELIABLE"
            assert engine.conn.execute("SELECT MAX(freshness_generation) FROM lifecycle_events").fetchone()[0] == 2
        finally:
            await engine.close()

    asyncio.run(run())


def test_healthy_clock_rejects_adverse_beyond_future_skew_without_event(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "future-adverse.db"), _profile())
        await engine.open()
        try:
            record = _current(last_verified_at="2026-10-31T18:00:00Z")
            await _seed(engine, record)
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:00:00Z")
            with pytest.raises(AdapterError) as exc_info:
                await _recheck(
                    engine,
                    record,
                    key="future-adverse",
                    observed_at="2026-10-31T20:06:00Z",
                    observed_hash="f" * 64,
                )
            assert exc_info.value.code == "EVIDENCE_INVALID"
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_binding_mismatch_fails_before_event(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "binding.db"), _profile())
        await engine.open()
        try:
            record = _current(last_verified_at="2026-10-31T18:00:00Z")
            record["freshness_policy"] = {
                "kind": "on_change",
                "ttl_seconds": None,
                "recheck_ref": "source:repo:file.md",
                "source_fingerprint": "9" * 64,
            }
            await _seed(engine, record)
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:00:00Z")
            with pytest.raises(AdapterError) as exc_info:
                await _recheck(engine, record, key="binding-mismatch", observed_at="2026-10-31T19:30:00Z")
            assert exc_info.value.code == "EVIDENCE_BINDING_MISMATCH"
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_public_reverify_cannot_bypass_active_manual_recheck_hold(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "reverify-hold.db"), _profile())
        await engine.open()
        try:
            record = _current(last_verified_at="2026-10-31T18:00:00Z")
            await _seed(engine, record)
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:00:00Z")
            adverse = await _recheck(
                engine,
                record,
                key="hold-before-public-reverify",
                observed_at="2026-10-31T19:30:00Z",
                observed_hash="7" * 64,
            )
            assert adverse["freshness_gate_state"] == "SOURCE_CHANGE_REVIEW_REQUIRED"
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile(
                    "owner-current",
                    contributor_ref="owner-test",
                ),
            )
            with pytest.raises(AdapterError) as exc_info:
                await adapter.update(
                    memory_id=str(record["memory_id"]),
                    scope="PROJECT",
                    project_id="manual-project",
                    expected_record_id=str(record["record_id"]),
                    expected_revision=1,
                    mode="REVERIFY",
                    idempotency_key="ordinary-reverify-bypass-attempt",
                    verification={
                        "source_provenance": record["source_provenance"],
                        "freshness_policy": record["freshness_policy"],
                        "source_timestamp": "2026-10-31T19:31:00Z",
                        "last_verified_at": "2026-10-31T19:31:00Z",
                        "verified_by": "OWNER",
                    },
                )
            assert exc_info.value.code == "RECHECK_EVIDENCE_REQUIRED"
            scope = validate_scope("PROJECT", project_id="manual-project")
            history = await engine.get_record(str(record["memory_id"]), scope, history=True)
            assert len(history) == 1
            assert history[0]["lifecycle_state"] == "VALIDATED_CURRENT"
        finally:
            await engine.close()

    asyncio.run(run())
