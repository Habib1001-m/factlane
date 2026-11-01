from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest

import factlane.storage as storage_module
from factlane.contract import AdapterError, validate_scope
from factlane.embeddings import EmbeddingProfile
from factlane.storage import SQLiteVecEngine


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="readmit-test-256",
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


def _record(*, fact: str, key: str, lifecycle: str = "VALIDATED_CURRENT", marker: str = "a") -> dict[str, object]:
    record_id = str(uuid.uuid4())
    return {
        "record_id": record_id,
        "memory_id": str(uuid.uuid4()),
        "revision": 1,
        "parent_record_id": None,
        "scope": "PROJECT",
        "project_id": "readmit-project",
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "memory_type": "PROJECT_LEARNED_FACT",
        "fact": fact,
        "source_provenance": {
            "source_class": "CURRENT_REPO",
            "source_ref": f"repo:{marker}.md",
            "source_hash": marker * 64,
            "review_ref": f"review-{marker}",
            "extraction_method": "manual",
        },
        "source_timestamp": "2026-10-31T18:00:00Z" if lifecycle == "VALIDATED_CURRENT" else None,
        "created_at": "2026-10-31T18:00:00Z",
        "last_verified_at": "2026-10-31T18:00:00Z" if lifecycle == "VALIDATED_CURRENT" else None,
        "verified_by": "OWNER" if lifecycle == "VALIDATED_CURRENT" else "UNVERIFIED",
        "authority_role": "PROJECT_CURRENT" if lifecycle == "VALIDATED_CURRENT" else "UNRESOLVED",
        "contribution_origin": {"contributor_class": "OWNER", "contributor_ref": "owner-readmit"},
        "freshness_policy": {"kind": "manual", "ttl_seconds": None, "recheck_ref": None, "source_fingerprint": None},
        "supersedes": [],
        "contradiction_key": key,
        "contradiction_state": "NONE" if lifecycle == "VALIDATED_CURRENT" else "UNRESOLVED",
        "confidence": 1.0 if lifecycle == "VALIDATED_CURRENT" else 0.5,
        "tags": [],
        "lifecycle_state": lifecycle,
        "native_content_hash": marker * 64,
        "payload_fingerprint": marker.upper() * 64,
        "idempotency_key": f"seed-{marker}-{record_id}",
        "embedding_profile_id": _profile().profile_id,
        "embedding_model_digest": _profile().model_digest,
        "embedding_output_dimension": 256,
    }


def _successor(target: dict[str, object], *, marker: str = "z") -> dict[str, object]:
    successor = dict(target)
    successor.update(
        {
            "record_id": str(uuid.uuid4()),
            "revision": int(target["revision"]) + 1,
            "parent_record_id": target["record_id"],
            "source_provenance": {
                "source_class": "CURRENT_REPO",
                "source_ref": "repo:readmit-proof.md",
                "source_hash": marker * 64,
                "review_ref": "readmit-proof",
                "extraction_method": "owner-review",
            },
            "source_timestamp": "2026-10-31T20:00:00Z",
            "created_at": "2026-10-31T20:00:00Z",
            "last_verified_at": "2026-10-31T20:00:00Z",
            "verified_by": "OWNER",
            "authority_role": "PROJECT_CURRENT",
            "freshness_policy": {"kind": "manual", "ttl_seconds": None, "recheck_ref": None, "source_fingerprint": None},
            "contradiction_state": "NONE",
            "lifecycle_state": "VALIDATED_CURRENT",
            "native_content_hash": marker * 64,
            "payload_fingerprint": hashlib.sha256(f"successor-{marker}".encode()).hexdigest(),
            "idempotency_key": f"internal-readmit-{uuid.uuid4()}",
        }
    )
    return successor


async def _revoke(engine: SQLiteVecEngine, target: dict[str, object]) -> None:
    scope = validate_scope("PROJECT", project_id="readmit-project")
    token = (await engine.group_observation(scope, str(target["contradiction_key"])))["public"]["observed_group_token"]
    await engine.terminal_disposition(
        scope,
        action="REVOKE",
        memory_id=str(target["memory_id"]),
        expected_record_id=str(target["record_id"]),
        expected_revision=int(target["revision"]),
        observed_group_token=token,
        idempotency_key=f"revoke-{target['record_id']}",
        request_fingerprint=hashlib.sha256(f"revoke-{target['record_id']}".encode()).hexdigest(),
        request_id=f"request-revoke-{target['record_id']}",
        reason_code="OUTDATED",
        actor_class="OWNER",
        actor_ref_digest="actor-digest",
        grant_ref_digest="grant-digest",
        committed_at="2026-10-31T19:00:00Z",
    )


async def _readmit(
    engine: SQLiteVecEngine,
    target: dict[str, object],
    successor: dict[str, object],
    *,
    key: str,
    request_id: str | None = None,
) -> dict[str, object]:
    scope = validate_scope("PROJECT", project_id="readmit-project")
    token = (await engine.group_observation(scope, str(target["contradiction_key"])))["public"]["observed_group_token"]
    return await engine.readmit(
        scope,
        memory_id=str(target["memory_id"]),
        revoked_record_id=str(target["record_id"]),
        expected_revoked_revision=int(target["revision"]),
        observed_group_token=token,
        idempotency_key=key,
        request_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
        request_id=request_id or f"request-{key}",
        verified_successor=successor,
        actor_class="OWNER",
        actor_ref_digest="actor-digest",
        grant_ref_digest="grant-digest",
        committed_at="2026-10-31T20:00:00Z",
    )


def test_readmit_unique_revoked_tip_creates_current_successor_and_replays(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "readmit-current.db"), _profile())
        await engine.open()
        try:
            target = _record(fact="Revoked truth", key="readmit-current", marker="a")
            await engine.write_record(target, [1.0] + [0.0] * 255)
            await _revoke(engine, target)
            successor = _successor(target, marker="b")
            result = await _readmit(engine, target, successor, key="readmit-current-op")
            assert result["status"] == "OK"
            assert result["memory_id"] == target["memory_id"]
            assert result["revoked_record_id"] == target["record_id"]
            assert result["result_record_id"] == successor["record_id"]
            assert result["resulting_revision"] == 2
            assert result["resulting_lifecycle"] == "VALIDATED_CURRENT"
            assert result["resulting_contradiction_state"] == "NONE"
            scope = validate_scope("PROJECT", project_id="readmit-project")
            history = await engine.get_record(str(target["memory_id"]), scope, history=True)
            assert [row["lifecycle_state"] for row in history[:2]] == ["VALIDATED_CURRENT", "REVOKED"]
            assert history[0]["parent_record_id"] == target["record_id"]
            revoke_events = engine.conn.execute(
                "SELECT action FROM lifecycle_events WHERE memory_id=? ORDER BY rowid",
                (target["memory_id"],),
            ).fetchall()
            assert revoke_events == [("REVOKE",), ("READMIT",)]

            replay = await _readmit(
                engine,
                target,
                successor,
                key="readmit-current-op",
                request_id="new-request-id-must-not-replace-stored",
            )
            assert replay["operation_id"] == result["operation_id"]
            assert replay["request_id"] == "request-readmit-current-op"
            assert replay["idempotent_replay"] is True
            assert engine.conn.execute(
                "SELECT COUNT(*) FROM adapter_records WHERE memory_id=? AND parent_record_id=?",
                (target["memory_id"], target["record_id"]),
            ).fetchone()[0] == 1
        finally:
            await engine.close()

    asyncio.run(run())


def test_readmit_accepts_exact_ttl_boundary_and_replays_after_expiry(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "readmit-ttl-boundary.db"), _profile())
        await engine.open()
        try:
            target = _record(fact="TTL boundary truth", key="readmit-ttl-boundary", marker="a")
            await engine.write_record(target, [1.0] + [0.0] * 255)
            await _revoke(engine, target)
            assert engine.conn is not None
            engine.conn.execute(
                "UPDATE adapter_meta SET value=? WHERE key='trust_clock_high_water_at'",
                ("2026-10-31T19:59:00Z",),
            )
            engine.conn.commit()
            successor = _successor(target, marker="b")
            successor["source_timestamp"] = "2026-10-31T19:59:00Z"
            successor["last_verified_at"] = "2026-10-31T19:59:00Z"
            successor["freshness_policy"] = {
                "kind": "ttl",
                "ttl_seconds": 60,
                "recheck_ref": None,
                "source_fingerprint": None,
            }
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:00:00Z")
            result = await _readmit(engine, target, successor, key="readmit-ttl-boundary-op")
            assert result["status"] == "OK"
            assert result["result_record_id"] == successor["record_id"]

            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:30:00Z")
            replay = await _readmit(engine, target, successor, key="readmit-ttl-boundary-op")
            assert replay["operation_id"] == result["operation_id"]
            assert replay["idempotent_replay"] is True
            assert engine.conn.execute(
                "SELECT COUNT(*) FROM lifecycle_operations WHERE idempotency_key='readmit-ttl-boundary-op'"
            ).fetchone()[0] == 1

            scope = validate_scope("PROJECT", project_id="readmit-project")
            with pytest.raises(AdapterError) as changed_replay:
                await engine.readmit(
                    scope,
                    memory_id=str(target["memory_id"]),
                    revoked_record_id=str(target["record_id"]),
                    expected_revoked_revision=int(target["revision"]),
                    observed_group_token="stale-token-is-ignored-on-replay",
                    idempotency_key="readmit-ttl-boundary-op",
                    request_fingerprint="different-readmit-fingerprint",
                    request_id="changed-replay-request",
                    verified_successor=successor,
                    actor_class="OWNER",
                    actor_ref_digest="actor-digest",
                    grant_ref_digest="grant-digest",
                    committed_at="2026-10-31T20:30:00Z",
                )
            assert changed_replay.value.code == "IDEMPOTENCY_CONFLICT"
            assert engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
            ).fetchone()[0] == "2026-10-31T20:00:00Z"
        finally:
            await engine.close()

    asyncio.run(run())


def test_readmit_expiry_persists_highwater_without_partial_adjudication(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "readmit-ttl-expired.db"), _profile())
        await engine.open()
        try:
            target = _record(fact="TTL expiring truth", key="readmit-ttl-expired", marker="c")
            await engine.write_record(target, [1.0] + [0.0] * 255)
            await _revoke(engine, target)
            assert engine.conn is not None
            engine.conn.execute(
                "UPDATE adapter_meta SET value=? WHERE key='trust_clock_high_water_at'",
                ("2026-10-31T20:00:00Z",),
            )
            engine.conn.commit()
            successor = _successor(target, marker="d")
            successor["source_timestamp"] = "2026-10-31T20:00:00Z"
            successor["last_verified_at"] = "2026-10-31T20:00:00Z"
            successor["freshness_policy"] = {
                "kind": "ttl",
                "ttl_seconds": 60,
                "recheck_ref": None,
                "source_fingerprint": None,
            }
            scope = validate_scope("PROJECT", project_id="readmit-project")
            token_before = (await engine.group_observation(scope, str(target["contradiction_key"])))[
                "public"
            ]["observed_group_token"]
            counts_before = {
                table: int(engine.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "adapter_records",
                    "memories",
                    "memory_embeddings",
                    "lifecycle_events",
                    "lifecycle_operations",
                )
            }

            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:01:01Z")
            with pytest.raises(AdapterError) as expired:
                await _readmit(engine, target, successor, key="readmit-ttl-expired-op")
            assert expired.value.code == "VERIFICATION_REQUIRED"
            assert engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
            ).fetchone()[0] == "2026-10-31T20:01:01Z"
            assert {
                table: int(engine.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in counts_before
            } == counts_before
            assert (await engine.group_observation(scope, str(target["contradiction_key"])))[
                "public"
            ]["observed_group_token"] == token_before
            history = await engine.get_record(str(target["memory_id"]), scope, history=True)
            assert len(history) == 1
            assert history[0]["lifecycle_state"] == "REVOKED"

            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T19:00:00Z")
            with pytest.raises(AdapterError) as rollback_retry:
                await _readmit(engine, target, successor, key="readmit-ttl-expired-retry")
            assert rollback_retry.value.code == "VERIFICATION_REQUIRED"
            assert engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
            ).fetchone()[0] == "2026-10-31T20:01:01Z"
        finally:
            await engine.close()

    asyncio.run(run())


def test_readmit_with_incompatible_candidate_derives_candidate_unresolved(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "readmit-candidate.db"), _profile())
        await engine.open()
        try:
            target = _record(fact="Revoked truth", key="readmit-conflict", marker="c")
            contender = _record(fact="Different active truth", key="readmit-conflict", lifecycle="CANDIDATE", marker="d")
            await engine.write_record(target, [1.0] + [0.0] * 255)
            await engine.write_record(contender, [0.0, 1.0] + [0.0] * 254)
            await _revoke(engine, target)
            successor = _successor(target, marker="e")
            result = await _readmit(engine, target, successor, key="readmit-conflict-op")
            assert result["status"] == "OK"
            assert result["resulting_lifecycle"] == "CANDIDATE"
            assert result["resulting_contradiction_state"] == "UNRESOLVED"
            scope = validate_scope("PROJECT", project_id="readmit-project")
            history = await engine.get_record(str(target["memory_id"]), scope, history=True)
            assert history[0]["lifecycle_state"] == "CANDIDATE"
            assert history[0]["contradiction_state"] == "UNRESOLVED"
            contender_history = await engine.get_record(str(contender["memory_id"]), scope, history=True)
            assert contender_history[0]["contradiction_state"] == "UNRESOLVED"
        finally:
            await engine.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("peer_state", "expected_status"),
    [
        ("VALIDATED_CURRENT", "ALREADY_CURRENT_EQUIVALENT"),
        ("CANDIDATE", "ALREADY_CANDIDATE_EQUIVALENT"),
    ],
)
def test_readmit_equivalent_peer_claims_noop_idempotency_result(tmp_path, peer_state, expected_status) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / f"equivalent-{peer_state}.db"), _profile())
        await engine.open()
        try:
            target = _record(fact="Equivalent truth", key="equivalent-key", marker="f")
            await engine.write_record(target, [1.0] + [0.0] * 255)
            await _revoke(engine, target)
            peer = _record(fact="  EQUIVALENT   TRUTH ", key="equivalent-key", lifecycle=peer_state, marker="g")
            await engine.write_record(peer, [0.0, 1.0] + [0.0] * 254)
            successor = _successor(target, marker="h")
            result = await _readmit(engine, target, successor, key=f"equivalent-{peer_state}-op")
            assert result["status"] == expected_status
            assert result["result_record_id"] is None
            assert result["resulting_revision"] is None
            assert result["resulting_lifecycle"] is None
            assert result["event_ids"] == []
            replay = await _readmit(engine, target, successor, key=f"equivalent-{peer_state}-op")
            assert replay["status"] == expected_status
            assert replay["idempotent_replay"] is True
        finally:
            await engine.close()

    asyncio.run(run())


def test_different_key_after_successor_returns_lineage_changed_but_original_key_replays(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "lineage-change.db"), _profile())
        await engine.open()
        try:
            target = _record(fact="Lineage truth", key="lineage-key", marker="i")
            await engine.write_record(target, [1.0] + [0.0] * 255)
            await _revoke(engine, target)
            successor = _successor(target, marker="j")
            first = await _readmit(engine, target, successor, key="readmit-first-key")
            assert first["status"] == "OK"
            with pytest.raises(AdapterError) as changed:
                await _readmit(engine, target, _successor(target, marker="k"), key="readmit-second-key")
            assert changed.value.code == "LINEAGE_CHANGED"
            replay = await _readmit(engine, target, successor, key="readmit-first-key")
            assert replay["idempotent_replay"] is True
            assert replay["result_record_id"] == first["result_record_id"]
        finally:
            await engine.close()

    asyncio.run(run())


def test_recovery_quarantined_descendant_and_blocking_peer_fail_closed(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "readmit-blocked.db"), _profile())
        await engine.open()
        try:
            target = _record(fact="Blocked truth", key="blocked-key", marker="l")
            await engine.write_record(target, [1.0] + [0.0] * 255)
            await _revoke(engine, target)
            successor = _successor(target, marker="m")
            first = await _readmit(engine, target, successor, key="blocked-first")
            assert first["status"] == "OK"
            engine.conn.execute(
                "UPDATE adapter_records SET lifecycle_state='QUARANTINED' WHERE record_id=?",
                (successor["record_id"],),
            )
            engine.conn.commit()
            with pytest.raises(AdapterError) as purged:
                await _readmit(engine, target, _successor(target, marker="n"), key="blocked-second")
            assert purged.value.code == "READMIT_RECOVERY_PURGED_DENIED"
        finally:
            await engine.close()

        second = SQLiteVecEngine(str(tmp_path / "readmit-stale-peer.db"), _profile())
        await second.open()
        try:
            target = _record(fact="Stale peer target", key="stale-peer-key", marker="o")
            await second.write_record(target, [1.0] + [0.0] * 255)
            await _revoke(second, target)
            peer = _record(fact="Blocking stale peer", key="stale-peer-key", lifecycle="CANDIDATE", marker="p")
            await second.write_record(peer, [0.0, 1.0] + [0.0] * 254)
            second.conn.execute(
                "UPDATE adapter_records SET lifecycle_state='STALE' WHERE record_id=?",
                (peer["record_id"],),
            )
            second.conn.commit()
            with pytest.raises(AdapterError) as blocked:
                await _readmit(second, target, _successor(target, marker="q"), key="stale-peer-readmit")
            assert blocked.value.code == "GROUP_MEMBER_NOT_ADMISSIBLE"
        finally:
            await second.close()

    asyncio.run(run())


def test_readmit_rejects_freshness_held_current_peer(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "readmit-held-current.db"), _profile())
        await engine.open()
        try:
            target = _record(fact="Revoked truth", key="held-current-group", marker="a")
            await engine.write_record(target, [1.0] + [0.0] * 255)
            await _revoke(engine, target)
            peer = _record(fact="Revoked truth", key="held-current-group", marker="b")
            await engine.write_record(peer, [0.0, 1.0] + [0.0] * 254)
            assert engine.conn is not None
            engine.conn.execute(
                "INSERT INTO lifecycle_events(event_id,memory_id,record_id,target_record_id,target_revision,"
                "action,actor_class,grant_ref_digest,occurred_at,freshness_outcome,freshness_generation) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), peer["memory_id"], peer["record_id"], peer["record_id"], 1,
                 "MANUAL_RECHECK", "OWNER", "test-grant", "2026-10-31T19:00:00Z", "CHANGED", 1),
            )
            engine.conn.commit()
            with pytest.raises(AdapterError) as blocked:
                await _readmit(engine, target, _successor(target), key="held-current-readmit")
            assert blocked.value.code == "GROUP_MEMBER_NOT_ADMISSIBLE"
            assert engine.conn.execute(
                "SELECT COUNT(*) FROM lifecycle_operations WHERE idempotency_key='held-current-readmit'"
            ).fetchone()[0] == 0
        finally:
            await engine.close()

    asyncio.run(run())


def test_readmit_rejects_cross_memory_child_of_revoked_record(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "readmit-cross-memory-child.db"), _profile())
        await engine.open()
        try:
            target = _record(fact="Original truth", key="cross-memory-child", marker="a")
            await engine.write_record(target, [1.0] + [0.0] * 255)
            await _revoke(engine, target)
            child = _record(fact="Replacement truth", key="cross-memory-child", lifecycle="CANDIDATE", marker="b")
            await engine.write_record(child, [0.0, 1.0] + [0.0] * 254)
            assert engine.conn is not None
            engine.conn.execute(
                "UPDATE adapter_records SET parent_record_id=? WHERE record_id=?",
                (target["record_id"], child["record_id"]),
            )
            engine.conn.commit()
            with pytest.raises(AdapterError) as blocked:
                await _readmit(engine, target, _successor(target), key="cross-memory-child-readmit")
            assert blocked.value.code == "LINEAGE_CHANGED"
        finally:
            await engine.close()

    asyncio.run(run())
