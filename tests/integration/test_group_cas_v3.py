from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from factlane.adapter import MemoryAdapter, trusted_write_context_for_profile
from factlane.contract import AdapterError, validate_scope
from factlane.embeddings import EmbeddingProfile
from factlane.storage import SQLiteVecEngine


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="group-cas-test-256",
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


def _record(*, marker: str, lifecycle: str, contradiction_key: str, fact: str) -> dict[str, object]:
    current = lifecycle == "VALIDATED_CURRENT"
    return {
        "record_id": str(uuid.uuid4()),
        "memory_id": str(uuid.uuid4()),
        "revision": 1,
        "parent_record_id": None,
        "scope": "PROJECT",
        "project_id": "group-project",
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "memory_type": "PROJECT_LEARNED_FACT",
        "fact": fact,
        "source_provenance": {
            "source_class": "TEST",
            "source_ref": marker,
            "source_hash": marker * 64,
            "review_ref": marker,
            "extraction_method": "test",
        },
        "source_timestamp": "2026-10-31T20:00:00Z" if current else None,
        "created_at": "2026-10-31T20:00:00Z",
        "last_verified_at": "2026-10-31T20:00:00Z" if current else None,
        "verified_by": "OWNER" if current else "UNVERIFIED",
        "authority_role": "PROJECT_CURRENT" if current else "UNRESOLVED",
        "contribution_origin": {"contributor_class": "OWNER", "contributor_ref": "test-owner"},
        "freshness_policy": {
            "kind": "on_change" if current else "manual",
            "ttl_seconds": None,
            "recheck_ref": "source:test" if current else None,
            "source_fingerprint": marker * 64 if current else None,
        },
        "supersedes": [],
        "contradiction_key": contradiction_key,
        "contradiction_state": "NONE" if current else "UNRESOLVED",
        "confidence": 1.0 if current else 0.5,
        "tags": [],
        "lifecycle_state": lifecycle,
        "native_content_hash": (marker * 64)[:64],
        "payload_fingerprint": ((marker.upper() if marker.isalpha() else marker) * 64)[:64],
        "idempotency_key": f"group-{marker}",
        "embedding_profile_id": _profile().profile_id,
        "embedding_model_digest": _profile().model_digest,
        "embedding_output_dimension": 256,
    }


def test_group_cas_schema_v2_rotates_on_freshness_generation(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "group.db"), _profile())
        await engine.open()
        try:
            scope = validate_scope("PROJECT", project_id="group-project")
            key = "group-key"
            current = _record(marker="a", lifecycle="VALIDATED_CURRENT", contradiction_key=key, fact="Current truth")
            candidate = _record(marker="b", lifecycle="CANDIDATE", contradiction_key=key, fact="Conflicting candidate")
            await engine.write_record(current, [1.0] + [0.0] * 255)
            await engine.write_record(candidate, [0.0, 1.0] + [0.0] * 254)
            before = await engine.group_observation(scope, key)
            assert before["public"]["schema_version"] == 2
            assert before["public"]["member_count"] == 2
            assert [m["record_id"] for m in before["public"]["members"]] == sorted(
                [current["record_id"], candidate["record_id"]]
            )
            current_member = next(m for m in before["public"]["members"] if m["record_id"] == current["record_id"])
            assert current_member["freshness_gate_state"] == "NONE"

            assert engine.conn is not None
            engine.conn.execute(
                "INSERT INTO lifecycle_events("
                "event_id,memory_id,record_id,target_record_id,target_revision,action,actor_class,grant_ref_digest,"
                "occurred_at,freshness_outcome,observation_origin,observed_at,accepted_at,freshness_generation,clock_anomaly"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "event-1", current["memory_id"], current["record_id"], current["record_id"], 1,
                    "MANUAL_RECHECK", "OWNER", "grant-digest", "2026-10-31T20:05:00Z", "CHANGED",
                    "OWNER_SUPPLIED", "2026-10-31T20:04:00Z", "2026-10-31T20:05:00Z", 1, "NONE",
                ),
            )
            engine.conn.commit()
            after = await engine.group_observation(scope, key)
            assert after["public"]["observed_group_token"] != before["public"]["observed_group_token"]
            current_member = next(m for m in after["public"]["members"] if m["record_id"] == current["record_id"])
            assert current_member["freshness_gate_state"] == "SOURCE_CHANGE_REVIEW_REQUIRED"
            assert after["server"]["members"][0 if after["server"]["members"][0]["record_id"] == current["record_id"] else 1]["freshness_generation"] == 1
        finally:
            await engine.close()

    asyncio.run(run())


def test_memory_get_held_current_returns_group_cas_and_history_exposes_gate(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "held.db"), _profile())
        await engine.open()
        try:
            key = "held-key"
            current = _record(marker="c", lifecycle="VALIDATED_CURRENT", contradiction_key=key, fact="Held current")
            await engine.write_record(current, [1.0] + [0.0] * 255)
            assert engine.conn is not None
            engine.conn.execute(
                "INSERT INTO lifecycle_events("
                "event_id,memory_id,record_id,target_record_id,target_revision,action,actor_class,grant_ref_digest,"
                "occurred_at,freshness_outcome,observation_origin,observed_at,accepted_at,freshness_generation,clock_anomaly"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "held-event", current["memory_id"], current["record_id"], current["record_id"], 1,
                    "MANUAL_RECHECK", "OWNER", "grant-digest", "2026-10-31T20:05:00Z", "UNAVAILABLE",
                    "OWNER_SUPPLIED", "2026-10-31T20:04:00Z", "2026-10-31T20:05:00Z", 1, "NONE",
                ),
            )
            engine.conn.commit()
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile("owner-current"),
            )
            current_view = await adapter.get(
                memory_id=current["memory_id"], scope="PROJECT", project_id="group-project", retrieval_mode="CURRENT"
            )
            assert current_view["status"] == "DEGRADED"
            assert current_view["degradation"] == "FRESHNESS_HOLD"
            assert current_view["results"] == []
            assert current_view["group_cas"]["schema_version"] == 2
            assert current_view["group_cas"]["members"][0]["freshness_gate_state"] == "RECHECK_UNAVAILABLE"

            history = await adapter.get(
                memory_id=current["memory_id"], scope="PROJECT", project_id="group-project", retrieval_mode="REVIEW_HISTORY"
            )
            assert history["status"] == "OK"
            assert history["results"][0]["freshness_gate_state"] == "RECHECK_UNAVAILABLE"
            assert history["group_cas"]["observed_group_token"] == current_view["group_cas"]["observed_group_token"]
        finally:
            await engine.close()

    asyncio.run(run())


def test_group_overflow_returns_no_token(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "overflow.db"), _profile())
        await engine.open()
        try:
            scope = validate_scope("PROJECT", project_id="group-project")
            key = "overflow-key"
            for index in range(33):
                marker = f"{index:064x}"[-64:]
                record = _record(
                    marker=chr(ord("d") + (index % 20)),
                    lifecycle="CANDIDATE",
                    contradiction_key=key,
                    fact=f"candidate {index}",
                )
                record["record_id"] = str(uuid.uuid4())
                record["memory_id"] = str(uuid.uuid4())
                record["native_content_hash"] = marker
                record["payload_fingerprint"] = f"{index + 1000:064x}"
                record["idempotency_key"] = f"overflow-{index}"
                await engine.write_record(record, [1.0] + [0.0] * 255)
            with pytest.raises(AdapterError) as exc_info:
                await engine.group_observation(scope, key)
            assert exc_info.value.code == "GROUP_LIMIT_EXCEEDED"
            assert exc_info.value.details["member_count"] == 33
        finally:
            await engine.close()

    asyncio.run(run())


def test_owner_revoke_is_atomic_recomputes_group_and_replays(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "revoke.db"), _profile())
        await engine.open()
        try:
            key = "revoke-key"
            current = _record(marker="f", lifecycle="VALIDATED_CURRENT", contradiction_key=key, fact="Old truth")
            candidate = _record(marker="g", lifecycle="CANDIDATE", contradiction_key=key, fact="New candidate")
            await engine.write_record(current, [1.0] + [0.0] * 255)
            await engine.write_record(candidate, [0.0, 1.0] + [0.0] * 254)
            owner = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile("owner-current", contributor_ref="owner-1"),
            )
            observed = await owner.get(
                memory_id=current["memory_id"], scope="PROJECT", project_id="group-project", retrieval_mode="CURRENT"
            )
            token = observed["group_cas"]["observed_group_token"]
            result = await owner.update(
                memory_id=current["memory_id"],
                scope="PROJECT",
                project_id="group-project",
                expected_record_id=current["record_id"],
                expected_revision=1,
                mode="REVOKE",
                idempotency_key="revoke-operation-1",
                observed_group_token=token,
                reason_code="OUTDATED",
            )
            assert result["action"] == "REVOKE"
            assert result["resulting_lifecycle_state"] == "REVOKED"
            assert result["idempotent_replay"] is False
            assert result["affected_count"] == 2
            assert result["results"] == []
            assert result["resulting_group"]["group_token"] != token
            rows = await engine.get_record(current["memory_id"], validate_scope("PROJECT", project_id="group-project"), history=True)
            assert rows[0]["lifecycle_state"] == "REVOKED"
            candidate_rows = await engine.get_record(candidate["memory_id"], validate_scope("PROJECT", project_id="group-project"), history=True)
            assert candidate_rows[0]["contradiction_state"] == "NONE"
            assert engine.conn is not None
            actions = [row[0] for row in engine.conn.execute("SELECT action FROM lifecycle_events ORDER BY rowid").fetchall()]
            assert actions == ["REVOKE", "CONTRADICTION_RECOMPUTED"]

            replay = await owner.update(
                memory_id=current["memory_id"],
                scope="PROJECT",
                project_id="group-project",
                expected_record_id=current["record_id"],
                expected_revision=1,
                mode="REVOKE",
                idempotency_key="revoke-operation-1",
                observed_group_token=token,
                reason_code="OUTDATED",
            )
            assert replay["operation_id"] == result["operation_id"]
            assert replay["idempotent_replay"] is True
            assert engine.conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 2
        finally:
            await engine.close()

    asyncio.run(run())


def test_owner_reject_candidate_and_non_owner_disposition_denied(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "reject.db"), _profile())
        await engine.open()
        try:
            key = "reject-key"
            candidate = _record(marker="h", lifecycle="CANDIDATE", contradiction_key=key, fact="Candidate to reject")
            await engine.write_record(candidate, [1.0] + [0.0] * 255)
            owner = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile("owner-current", contributor_ref="owner-1"),
            )
            observed = await owner.get(
                memory_id=candidate["memory_id"], scope="PROJECT", project_id="group-project", retrieval_mode="REVIEW_HISTORY"
            )
            token = observed["group_cas"]["observed_group_token"]
            verifier = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile("repo-verifier", contributor_ref="ci-1"),
            )
            with pytest.raises(AdapterError) as denied:
                await verifier.update(
                    memory_id=candidate["memory_id"],
                    scope="PROJECT",
                    project_id="group-project",
                    expected_record_id=candidate["record_id"],
                    expected_revision=1,
                    mode="REJECT",
                    idempotency_key="reject-denied",
                    observed_group_token=token,
                    reason_code="INSUFFICIENT_EVIDENCE",
                )
            assert denied.value.code == "WRITE_AUTHORIZATION_DENIED"
            result = await owner.update(
                memory_id=candidate["memory_id"],
                scope="PROJECT",
                project_id="group-project",
                expected_record_id=candidate["record_id"],
                expected_revision=1,
                mode="REJECT",
                idempotency_key="reject-operation-1",
                observed_group_token=token,
                reason_code="INSUFFICIENT_EVIDENCE",
            )
            assert result["resulting_lifecycle_state"] == "REJECTED"
        finally:
            await engine.close()

    asyncio.run(run())


def test_terminal_disposition_stale_group_token_and_global_key_collision_fail_closed(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "terminal-conflict.db"), _profile())
        await engine.open()
        try:
            key = "terminal-conflict-key"
            current = _record(marker="i", lifecycle="VALIDATED_CURRENT", contradiction_key=key, fact="Current")
            await engine.write_record(current, [1.0] + [0.0] * 255)
            owner = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile("owner-current", contributor_ref="owner-1"),
            )
            observed = await owner.get(
                memory_id=current["memory_id"], scope="PROJECT", project_id="group-project", retrieval_mode="CURRENT"
            )
            stale_token = observed["group_cas"]["observed_group_token"]
            contender = _record(marker="j", lifecycle="CANDIDATE", contradiction_key=key, fact="Contender")
            await engine.write_record(contender, [0.0, 1.0] + [0.0] * 254)
            with pytest.raises(AdapterError) as changed:
                await owner.update(
                    memory_id=current["memory_id"],
                    scope="PROJECT",
                    project_id="group-project",
                    expected_record_id=current["record_id"],
                    expected_revision=1,
                    mode="REVOKE",
                    idempotency_key="stale-token-op",
                    observed_group_token=stale_token,
                    reason_code="OUTDATED",
                )
            assert changed.value.code == "GROUP_VERSION_CONFLICT"
            history = await engine.get_record(current["memory_id"], validate_scope("PROJECT", project_id="group-project"), history=True)
            assert history[0]["lifecycle_state"] == "VALIDATED_CURRENT"

            current_view = await owner.get(
                memory_id=current["memory_id"], scope="PROJECT", project_id="group-project", retrieval_mode="CURRENT"
            )
            with pytest.raises(AdapterError) as collision:
                await owner.update(
                    memory_id=current["memory_id"],
                    scope="PROJECT",
                    project_id="group-project",
                    expected_record_id=current["record_id"],
                    expected_revision=1,
                    mode="REVOKE",
                    idempotency_key=current["idempotency_key"],
                    observed_group_token=current_view["group_cas"]["observed_group_token"],
                    reason_code="OUTDATED",
                )
            assert collision.value.code == "IDEMPOTENCY_CONFLICT"
        finally:
            await engine.close()

    asyncio.run(run())


def _candidate_successor(candidate: dict[str, object], *, native_marker: str, operation_marker: str) -> dict[str, object]:
    successor = dict(candidate)
    successor.update(
        {
            "record_id": str(uuid.uuid4()),
            "revision": int(candidate["revision"]) + 1,
            "parent_record_id": candidate["record_id"],
            "source_timestamp": "2026-10-31T20:10:00Z",
            "created_at": "2026-10-31T20:10:00Z",
            "last_verified_at": "2026-10-31T20:10:00Z",
            "verified_by": "OWNER",
            "authority_role": "PROJECT_CURRENT",
            "contradiction_state": "NONE",
            "lifecycle_state": "VALIDATED_CURRENT",
            "native_content_hash": native_marker * 64,
            "payload_fingerprint": (native_marker.upper() * 64),
            "idempotency_key": f"internal-{operation_marker}",
        }
    )
    return successor


def test_resolve_existing_current_rejects_all_candidates_atomically_and_replays(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "resolve-current.db"), _profile())
        await engine.open()
        try:
            scope = validate_scope("PROJECT", project_id="group-project")
            key = "resolve-current-key"
            current = _record(marker="k", lifecycle="VALIDATED_CURRENT", contradiction_key=key, fact="Current truth")
            candidate_a = _record(marker="l", lifecycle="CANDIDATE", contradiction_key=key, fact="Candidate A")
            candidate_b = _record(marker="m", lifecycle="CANDIDATE", contradiction_key=key, fact="Candidate B")
            for row, vector in (
                (current, [1.0] + [0.0] * 255),
                (candidate_a, [0.0, 1.0] + [0.0] * 254),
                (candidate_b, [0.0, 0.0, 1.0] + [0.0] * 253),
            ):
                await engine.write_record(row, vector)
            observed = await engine.group_observation(scope, key)
            result = await engine.resolve_group(
                scope,
                contradiction_key=key,
                observed_group_token=observed["public"]["observed_group_token"],
                winner_memory_id=current["memory_id"],
                winner_record_id=current["record_id"],
                winner_expected_revision=1,
                idempotency_key="resolve-current-op",
                request_fingerprint="resolve-current-fingerprint",
                request_id="resolve-current-request",
                reason_code="OWNER_ADJUDICATION",
                actor_class="OWNER",
                actor_ref_digest="actor-digest",
                grant_ref_digest="grant-digest",
                committed_at="2026-10-31T20:20:00Z",
                candidate_successor=None,
            )
            assert result["winner"]["result_record_id"] == current["record_id"]
            assert result["winner"]["resulting_revision"] == 1
            assert result["resulting_group"]["conflict_state"] == "CLOSED"
            assert result["resulting_group"]["member_count"] == 1
            assert result["affected_count"] == 2
            for candidate in (candidate_a, candidate_b):
                row = (await engine.get_record(candidate["memory_id"], scope, history=True))[0]
                assert row["lifecycle_state"] == "REJECTED"
            replay = await engine.resolve_group(
                scope,
                contradiction_key=key,
                observed_group_token=observed["public"]["observed_group_token"],
                winner_memory_id=current["memory_id"],
                winner_record_id=current["record_id"],
                winner_expected_revision=1,
                idempotency_key="resolve-current-op",
                request_fingerprint="resolve-current-fingerprint",
                request_id="different-request-is-not-used-on-replay",
                reason_code="OWNER_ADJUDICATION",
                actor_class="OWNER",
                actor_ref_digest="actor-digest",
                grant_ref_digest="grant-digest",
                committed_at="2026-10-31T20:21:00Z",
                candidate_successor=None,
            )
            assert replay["operation_id"] == result["operation_id"]
            assert replay["request_id"] == "resolve-current-request"
            assert replay["idempotent_replay"] is True
        finally:
            await engine.close()

    asyncio.run(run())


def test_resolve_candidate_creates_verified_successor_and_supersedes_current(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "resolve-candidate.db"), _profile())
        await engine.open()
        try:
            scope = validate_scope("PROJECT", project_id="group-project")
            key = "resolve-candidate-key"
            current = _record(marker="n", lifecycle="VALIDATED_CURRENT", contradiction_key=key, fact="Old current")
            winner = _record(marker="o", lifecycle="CANDIDATE", contradiction_key=key, fact="Winner candidate")
            loser = _record(marker="p", lifecycle="CANDIDATE", contradiction_key=key, fact="Losing candidate")
            for row, vector in (
                (current, [1.0] + [0.0] * 255),
                (winner, [0.0, 1.0] + [0.0] * 254),
                (loser, [0.0, 0.0, 1.0] + [0.0] * 253),
            ):
                await engine.write_record(row, vector)
            successor = _candidate_successor(winner, native_marker="q", operation_marker="resolve-candidate")
            observed = await engine.group_observation(scope, key)
            result = await engine.resolve_group(
                scope,
                contradiction_key=key,
                observed_group_token=observed["public"]["observed_group_token"],
                winner_memory_id=winner["memory_id"],
                winner_record_id=winner["record_id"],
                winner_expected_revision=1,
                idempotency_key="resolve-candidate-op",
                request_fingerprint="resolve-candidate-fingerprint",
                request_id="resolve-candidate-request",
                reason_code="STRONGER_EVIDENCE",
                actor_class="OWNER",
                actor_ref_digest="actor-digest",
                grant_ref_digest="grant-digest",
                committed_at="2026-10-31T20:20:00Z",
                candidate_successor=successor,
            )
            assert result["winner"]["memory_id"] == winner["memory_id"]
            assert result["winner"]["parent_record_id"] == winner["record_id"]
            assert result["winner"]["result_record_id"] == successor["record_id"]
            assert result["winner"]["resulting_revision"] == 2
            assert result["resulting_group"]["member_count"] == 1
            current_history = (await engine.get_record(current["memory_id"], scope, history=True))[0]
            assert current_history["lifecycle_state"] == "SUPERSEDED"
            winner_history = await engine.get_record(winner["memory_id"], scope, history=True)
            assert [row["lifecycle_state"] for row in winner_history[:2]] == ["VALIDATED_CURRENT", "SUPERSEDED"]
            assert winner_history[0]["record_id"] == successor["record_id"]
            loser_history = (await engine.get_record(loser["memory_id"], scope, history=True))[0]
            assert loser_history["lifecycle_state"] == "REJECTED"
            assert engine.conn is not None
            native = engine.conn.execute(
                "SELECT m.id,e.rowid FROM memories m JOIN memory_embeddings e ON e.rowid=m.id "
                "WHERE m.content_hash=? AND e.store=?",
                (successor["native_content_hash"], engine.embedding_store_key),
            ).fetchone()
            assert native is not None
        finally:
            await engine.close()

    asyncio.run(run())


def test_candidate_promotion_rereads_complete_group_and_rejects_incompatible_peer(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "promotion-group.db"), _profile())
        await engine.open()
        try:
            scope = validate_scope("PROJECT", project_id="group-project")
            key = "promotion-group-key"
            target = _record(marker="r", lifecycle="CANDIDATE", contradiction_key=key, fact="Candidate target")
            target["contradiction_state"] = "NONE"
            contender = _record(marker="s", lifecycle="CANDIDATE", contradiction_key=key, fact="Incompatible contender")
            contender["contradiction_state"] = "UNRESOLVED"
            await engine.write_record(target, [1.0] + [0.0] * 255)
            await engine.write_record(contender, [0.0, 1.0] + [0.0] * 254)
            successor = _candidate_successor(target, native_marker="t", operation_marker="promotion-group")
            with pytest.raises(AdapterError) as exc_info:
                await engine.promote_candidate(
                    successor,
                    [1.0] + [0.0] * 255,
                    scope,
                    expected_record_id=str(target["record_id"]),
                    expected_revision=1,
                )
            assert exc_info.value.code == "CONTRADICTION"
            history = await engine.get_record(str(target["memory_id"]), scope, history=True)
            assert len(history) == 1
            assert history[0]["lifecycle_state"] == "CANDIDATE"
        finally:
            await engine.close()

    asyncio.run(run())
