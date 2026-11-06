from __future__ import annotations

import asyncio
import sqlite3
import threading
import uuid
from typing import Any

import pytest

import factlane.owner_control as owner_control_module
import factlane.storage as storage_module
from factlane.adapter import MemoryAdapter, trusted_write_context_for_profile
from factlane.contract import AdapterError, canonical_json, validate_scope
from factlane.embeddings import EmbeddingProfile
from factlane.owner_control import OwnerControlService
from factlane.storage import SQLiteVecEngine


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="owner-control-test-256",
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


class _BeginImmediateProbe:
    def __init__(self, connection: sqlite3.Connection, attempted: threading.Event) -> None:
        self._connection = connection
        self._attempted = attempted

    def execute(self, sql: str, *args, **kwargs):
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            self._attempted.set()
        return self._connection.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def _record(
    *,
    marker: str,
    project_id: str,
    lifecycle: str = "CANDIDATE",
    contradiction_key: str | None = None,
    fact: str | None = None,
    created_at: str = "2026-10-31T20:00:00Z",
) -> dict[str, object]:
    current = lifecycle == "VALIDATED_CURRENT"
    key = contradiction_key or f"key-{marker}"
    value = fact or f"Fact {marker}"
    return {
        "record_id": str(uuid.uuid4()),
        "memory_id": str(uuid.uuid4()),
        "revision": 1,
        "parent_record_id": None,
        "scope": "PROJECT",
        "project_id": project_id,
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "memory_type": "PROJECT_LEARNED_FACT",
        "fact": value,
        "source_provenance": {
            "source_class": "TEST",
            "source_ref": f"source:{marker}",
            "source_hash": (marker * 64)[:64],
            "review_ref": f"review:{marker}",
            "extraction_method": "test",
        },
        "source_timestamp": created_at if current else None,
        "created_at": created_at,
        "last_verified_at": created_at if current else None,
        "verified_by": "OWNER" if current else "UNVERIFIED",
        "authority_role": "PROJECT_CURRENT" if current else "UNRESOLVED",
        "contribution_origin": {"contributor_class": "OWNER", "contributor_ref": "owner-test"},
        "freshness_policy": {"kind": "manual"},
        "supersedes": [],
        "contradiction_key": key,
        "contradiction_state": "NONE" if current else "UNRESOLVED",
        "confidence": 1.0 if current else 0.5,
        "tags": [f"subject:{key}"],
        "lifecycle_state": lifecycle,
        "native_content_hash": (marker * 64)[:64],
        "payload_fingerprint": ((marker.upper() if marker.isalpha() else marker) * 64)[:64],
        "idempotency_key": f"owner-{marker}-{project_id}",
        "embedding_profile_id": _profile().profile_id,
        "embedding_model_digest": _profile().model_digest,
        "embedding_output_dimension": 256,
    }


async def _service(tmp_path, *, owner: bool = True) -> tuple[SQLiteVecEngine, OwnerControlService]:
    engine = SQLiteVecEngine(str(tmp_path / f"owner-{uuid.uuid4()}.db"), _profile())
    await engine.open()
    profile = "owner-current" if owner else "repo-verifier"
    adapter = MemoryAdapter(
        engine,
        _Provider(),
        trusted_write_context=trusted_write_context_for_profile(profile, contributor_ref="owner-test"),
    )
    return engine, OwnerControlService(adapter)


def test_service_requires_direct_owner_authority(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "non-owner.db"), _profile())
        await engine.open()
        try:
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile("repo-verifier", contributor_ref="ci-test"),
            )
            with pytest.raises(AdapterError) as exc_info:
                OwnerControlService(adapter)
            assert exc_info.value.code == "WRITE_AUTHORIZATION_DENIED"
        finally:
            await engine.close()

    asyncio.run(run())


def test_inventory_is_scope_bound_and_keyset_paginated(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            rows = [
                _record(marker="a", project_id="project-a", created_at="2026-10-31T20:00:01Z"),
                _record(marker="b", project_id="project-a", created_at="2026-10-31T20:00:02Z"),
                _record(marker="c", project_id="project-a", created_at="2026-10-31T20:00:03Z"),
                _record(marker="d", project_id="project-b", created_at="2026-10-31T20:00:04Z"),
            ]
            for index, row in enumerate(rows):
                await engine.write_record(row, [1.0 if index == 0 else 0.0] + [0.0] * 255)

            first = await service.inventory(scope="PROJECT", project_id="project-a", limit=2)
            assert first["status"] == "OK"
            assert len(first["items"]) == 2
            assert all(item["scope"]["project_id"] == "project-a" for item in first["items"])
            assert all(item["group_cas"]["observed_group_token"] for item in first["items"])
            assert first["next_cursor"] is not None

            second = await service.inventory(
                scope="PROJECT",
                project_id="project-a",
                limit=2,
                cursor=first["next_cursor"],
            )
            assert len(second["items"]) == 1
            assert second["next_cursor"] is None
            assert {item["record_id"] for item in first["items"]}.isdisjoint(
                {item["record_id"] for item in second["items"]}
            )

            with pytest.raises(AdapterError) as exc_info:
                await service.inventory(
                    scope="PROJECT",
                    project_id="project-b",
                    limit=2,
                    cursor=first["next_cursor"],
                )
            assert exc_info.value.code == "INVALID_CURSOR"
        finally:
            await engine.close()

    asyncio.run(run())


def test_detail_exposes_exact_latest_and_independent_record_event_cursors(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            row = _record(
                marker="e",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="detail-pagination-key",
                created_at="2026-11-01T20:00:00Z",
            )
            await engine.write_record(row, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")

            parent = row
            for index in (2, 3):
                token = (await engine.group_observation(scope, "detail-pagination-key"))["public"][
                    "observed_group_token"
                ]
                result = await service.reverify(
                    memory_id=row["memory_id"],
                    expected_record_id=parent["record_id"],
                    expected_revision=index - 1,
                    observed_group_token=token,
                    idempotency_key=f"detail-pagination-reverify-{index}",
                    verification={"source_timestamp": "2026-11-01T20:00:00Z", "verified_by": "OWNER"},
                    scope="PROJECT",
                    project_id="project-a",
                )
                parent = result["results"][0]

            token = (await engine.group_observation(scope, "detail-pagination-key"))["public"][
                "observed_group_token"
            ]
            await service.revoke(
                memory_id=row["memory_id"],
                expected_record_id=parent["record_id"],
                expected_revision=3,
                observed_group_token=token,
                idempotency_key="detail-pagination-revoke-1",
                reason_code="OUTDATED",
                scope="PROJECT",
                project_id="project-a",
            )

            revoked_token = (await engine.group_observation(scope, "detail-pagination-key"))["public"][
                "observed_group_token"
            ]
            await service.readmit(
                memory_id=row["memory_id"],
                revoked_record_id=parent["record_id"],
                expected_revoked_revision=3,
                observed_group_token=revoked_token,
                idempotency_key="detail-pagination-readmit",
                verification={
                    "source_provenance": {
                        "source_class": "TEST",
                        "source_ref": "detail-pagination-source",
                        "source_hash": "7" * 64,
                        "review_ref": "detail-pagination-review",
                        "extraction_method": "owner-check",
                    },
                    "freshness_policy": {"kind": "manual"},
                    "source_timestamp": "2026-11-01T21:00:00Z",
                    "verified_by": "OWNER",
                    "tags": row["tags"],
                },
                scope="PROJECT",
                project_id="project-a",
            )
            current = (await engine.get_record(row["memory_id"], scope, history=False))[0]
            token = (await engine.group_observation(scope, "detail-pagination-key"))["public"][
                "observed_group_token"
            ]
            await service.revoke(
                memory_id=row["memory_id"],
                expected_record_id=current["record_id"],
                expected_revision=4,
                observed_group_token=token,
                idempotency_key="detail-pagination-revoke-2",
                reason_code="OUTDATED",
                scope="PROJECT",
                project_id="project-a",
            )

            first = await service.detail(
                memory_id=row["memory_id"],
                scope="PROJECT",
                project_id="project-a",
                limit=1,
            )
            assert first["records"][0]["revision"] == 1
            assert first["latest"]["revision"] == 4
            assert first["latest"]["record_id"] == current["record_id"]
            assert first["latest"]["lifecycle_state"] == "REVOKED"
            assert first["next_cursor"] is not None
            assert len(first["events"]) == 1
            assert first["events_next_cursor"] is not None

            second = await service.detail(
                memory_id=row["memory_id"],
                scope="PROJECT",
                project_id="project-a",
                limit=1,
                cursor=first["next_cursor"],
                event_cursor=first["events_next_cursor"],
            )
            assert second["records"][0]["revision"] == 2
            assert second["latest"]["revision"] == 4
            assert len(second["events"]) == 1
            assert second["events"][0]["event_id"] != first["events"][0]["event_id"]
        finally:
            await engine.close()

    asyncio.run(run())


def test_detail_conflicts_and_health_are_exact_scope_reads(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            key = "shared-conflict"
            current = _record(
                marker="e",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key=key,
                fact="Current truth",
            )
            candidate = _record(
                marker="f",
                project_id="project-a",
                contradiction_key=key,
                fact="Candidate truth",
            )
            other = _record(marker="g", project_id="project-b", lifecycle="VALIDATED_CURRENT")
            for row in (current, candidate, other):
                await engine.write_record(row, [1.0] + [0.0] * 255)

            detail = await service.detail(
                memory_id=current["memory_id"],
                scope="PROJECT",
                project_id="project-a",
            )
            assert detail["status"] == "OK"
            assert detail["memory_id"] == current["memory_id"]
            assert detail["records"][0]["record_id"] == current["record_id"]
            assert detail["group_cas"]["member_count"] == 2
            assert isinstance(detail["events"], list)

            with pytest.raises(AdapterError) as cross_scope:
                await service.detail(
                    memory_id=current["memory_id"],
                    scope="PROJECT",
                    project_id="project-b",
                )
            assert cross_scope.value.code == "CROSS_SCOPE_DENIED"

            conflicts = await service.conflicts(scope="PROJECT", project_id="project-a")
            assert conflicts["status"] == "OK"
            assert len(conflicts["items"]) == 1
            assert conflicts["items"][0]["contradiction_key"] == key
            assert conflicts["items"][0]["group_cas"]["member_count"] == 2
            seed = conflicts["items"][0]["candidate_verification_seeds"][candidate["record_id"]]
            assert seed["source_provenance"]["source_ref"] == "source:f"
            assert seed["freshness_policy"] == {"kind": "manual"}
            assert seed["source_timestamp"] == candidate["source_timestamp"]
            assert seed["verified_by"] == "OWNER"

            health = await service.health(scope="PROJECT", project_id="project-a")
            assert health["status"] == "OK"
            assert health["scope"]["project_id"] == "project-a"
            assert health["backend_status"]["counts"]["VALIDATED_CURRENT"] == 1
        finally:
            await engine.close()

    asyncio.run(run())


def test_revoke_and_reject_wrap_governed_adapter_paths(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            revoke_key = "revoke-key"
            current = _record(
                marker="h",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key=revoke_key,
                fact="Old truth",
            )
            survivor = _record(
                marker="i",
                project_id="project-a",
                contradiction_key=revoke_key,
                fact="Candidate truth",
            )
            reject_candidate = _record(marker="j", project_id="project-a", contradiction_key="reject-key")
            for row in (current, survivor, reject_candidate):
                await engine.write_record(row, [1.0] + [0.0] * 255)

            revoke_group = await engine.group_observation(validate_scope("PROJECT", project_id="project-a"), revoke_key)
            revoked = await service.revoke(
                memory_id=current["memory_id"],
                expected_record_id=current["record_id"],
                expected_revision=1,
                observed_group_token=revoke_group["public"]["observed_group_token"],
                idempotency_key="owner-service-revoke",
                reason_code="OUTDATED",
                scope="PROJECT",
                project_id="project-a",
            )
            assert revoked["action"] == "REVOKE"
            assert revoked["resulting_lifecycle_state"] == "REVOKED"

            reject_group = await engine.group_observation(
                validate_scope("PROJECT", project_id="project-a"), "reject-key"
            )
            rejected = await service.reject(
                memory_id=reject_candidate["memory_id"],
                expected_record_id=reject_candidate["record_id"],
                expected_revision=1,
                observed_group_token=reject_group["public"]["observed_group_token"],
                idempotency_key="owner-service-reject",
                reason_code="INSUFFICIENT_EVIDENCE",
                scope="PROJECT",
                project_id="project-a",
            )
            assert rejected["action"] == "REJECT"
            assert rejected["resulting_lifecycle_state"] == "REJECTED"
        finally:
            await engine.close()

    asyncio.run(run())


def test_resolve_candidate_requires_owner_proof_and_builds_verified_successor(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            key = "resolve-key"
            current = _record(
                marker="k",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key=key,
                fact="Old current",
            )
            winner = _record(
                marker="l",
                project_id="project-a",
                contradiction_key=key,
                fact="Candidate winner",
            )
            loser = _record(
                marker="m",
                project_id="project-a",
                contradiction_key=key,
                fact="Candidate loser",
            )
            for row in (current, winner, loser):
                await engine.write_record(row, [1.0] + [0.0] * 255)
            observed = await engine.group_observation(validate_scope("PROJECT", project_id="project-a"), key)

            with pytest.raises(AdapterError) as proof_required:
                await service.resolve(
                    contradiction_key=key,
                    observed_group_token=observed["public"]["observed_group_token"],
                    winner_memory_id=winner["memory_id"],
                    winner_record_id=winner["record_id"],
                    winner_expected_revision=1,
                    idempotency_key="resolve-missing-proof",
                    reason_code="STRONGER_EVIDENCE",
                    scope="PROJECT",
                    project_id="project-a",
                )
            assert proof_required.value.code == "WINNER_PROOF_REQUIRED"

            verification = {
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "verified-source",
                    "source_hash": "1" * 64,
                    "review_ref": "owner-review",
                    "extraction_method": "owner-check",
                },
                "freshness_policy": {"kind": "manual"},
                "source_timestamp": "2026-10-31T20:10:00Z",
                "last_verified_at": "2026-10-31T20:11:00Z",
                "verified_by": "OWNER",
                "tags": winner["tags"],
            }
            resolved = await service.resolve(
                contradiction_key=key,
                observed_group_token=observed["public"]["observed_group_token"],
                winner_memory_id=winner["memory_id"],
                winner_record_id=winner["record_id"],
                winner_expected_revision=1,
                idempotency_key="resolve-with-proof",
                reason_code="STRONGER_EVIDENCE",
                verification=verification,
                scope="PROJECT",
                project_id="project-a",
            )
            assert resolved["action"] == "RESOLVE"
            assert resolved["winner"]["parent_record_id"] == winner["record_id"]
            assert resolved["winner"]["resulting_revision"] == 2
            replayed = await service.resolve(
                contradiction_key=key,
                observed_group_token=observed["public"]["observed_group_token"],
                winner_memory_id=winner["memory_id"],
                winner_record_id=winner["record_id"],
                winner_expected_revision=1,
                idempotency_key="resolve-with-proof",
                reason_code="STRONGER_EVIDENCE",
                verification=verification,
                scope="PROJECT",
                project_id="project-a",
            )
            assert replayed["idempotent_replay"] is True
            assert replayed["operation_id"] == resolved["operation_id"]
            assert replayed["winner"] == resolved["winner"]

            changed_verification = dict(verification)
            changed_verification["source_timestamp"] = "2026-10-31T20:12:00Z"
            with pytest.raises(AdapterError) as changed:
                await service.resolve(
                    contradiction_key=key,
                    observed_group_token=observed["public"]["observed_group_token"],
                    winner_memory_id=winner["memory_id"],
                    winner_record_id=winner["record_id"],
                    winner_expected_revision=1,
                    idempotency_key="resolve-with-proof",
                    reason_code="STRONGER_EVIDENCE",
                    verification=changed_verification,
                    scope="PROJECT",
                    project_id="project-a",
                )
            assert changed.value.code == "IDEMPOTENCY_CONFLICT"
            history = await engine.get_record(
                winner["memory_id"], validate_scope("PROJECT", project_id="project-a"), history=True
            )
            assert history[0]["lifecycle_state"] == "VALIDATED_CURRENT"
            assert history[0]["fact"] == winner["fact"]
            assert history[0]["verified_by"] == "OWNER"
            assert engine.conn is not None
            operation_row = engine.conn.execute(
                "SELECT * FROM lifecycle_operations WHERE idempotency_key=?",
                ("resolve-with-proof",),
            ).fetchone()
            assert operation_row is not None
            durable_operation = repr(tuple(operation_row))
            assert "verified-source" not in durable_operation
            assert "owner-review" not in durable_operation
            assert "owner-check" not in durable_operation
        finally:
            await engine.close()

    asyncio.run(run())


def test_resolve_candidate_rejects_stale_ttl_verification_before_current_admission(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            key = "resolve-stale-proof-key"
            current = _record(
                marker="1",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key=key,
                fact="Old current",
            )
            winner = _record(
                marker="2",
                project_id="project-a",
                contradiction_key=key,
                fact="Candidate stale proof winner",
            )
            for row in (current, winner):
                await engine.write_record(row, [1.0] + [0.0] * 255)
            observed = await engine.group_observation(validate_scope("PROJECT", project_id="project-a"), key)
            monkeypatch.setattr(owner_control_module, "iso_now", lambda: "2026-10-31T22:00:00Z")
            verification = {
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "stale-resolve-source",
                    "source_hash": "7" * 64,
                    "review_ref": "stale-resolve-review",
                    "extraction_method": "owner-check",
                },
                "freshness_policy": {"kind": "ttl", "ttl_seconds": 60},
                "source_timestamp": "2026-10-31T20:00:00Z",
                "last_verified_at": "2026-10-31T20:00:00Z",
                "verified_by": "OWNER",
                "tags": winner["tags"],
            }
            with pytest.raises(AdapterError) as stale:
                await service.resolve(
                    contradiction_key=key,
                    observed_group_token=observed["public"]["observed_group_token"],
                    winner_memory_id=winner["memory_id"],
                    winner_record_id=winner["record_id"],
                    winner_expected_revision=1,
                    idempotency_key="resolve-stale-proof",
                    reason_code="STRONGER_EVIDENCE",
                    verification=verification,
                    scope="PROJECT",
                    project_id="project-a",
                )
            assert stale.value.code == "WINNER_PROOF_INVALID"
            history = await engine.get_record(winner["memory_id"], validate_scope("PROJECT", project_id="project-a"), history=True)
            assert len(history) == 1
            assert history[0]["lifecycle_state"] == "CANDIDATE"
        finally:
            await engine.close()

    asyncio.run(run())


def test_resolve_candidate_rechecks_ttl_after_write_lock_wait_without_partial_adjudication(
    tmp_path,
    monkeypatch,
) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        blocker: sqlite3.Connection | None = None
        begin_attempted = threading.Event()
        try:
            key = "resolve-lock-wait-expiry"
            current = _record(
                marker="a1",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key=key,
                fact="Current before lock wait",
            )
            winner = _record(
                marker="b1",
                project_id="project-a",
                contradiction_key=key,
                fact="Candidate whose proof expires",
            )
            loser = _record(
                marker="c1",
                project_id="project-a",
                contradiction_key=key,
                fact="Losing candidate",
            )
            for row in (current, winner, loser):
                await engine.write_record(row, [1.0] + [0.0] * 255)
            assert engine.conn is not None
            engine.conn.execute(
                "UPDATE adapter_meta SET value=? WHERE key='trust_clock_high_water_at'",
                ("2026-10-31T20:00:00Z",),
            )
            engine.conn.commit()
            scope = validate_scope("PROJECT", project_id="project-a")
            observed = await engine.group_observation(scope, key)
            token_before = observed["public"]["observed_group_token"]
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
            verification = {
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "lock-wait-resolve-source",
                    "source_hash": "7" * 64,
                    "review_ref": "lock-wait-resolve-review",
                    "extraction_method": "owner-check",
                },
                "freshness_policy": {"kind": "ttl", "ttl_seconds": 60},
                "source_timestamp": "2026-10-31T20:00:00Z",
                "last_verified_at": "2026-10-31T20:00:00Z",
                "verified_by": "OWNER",
                "tags": winner["tags"],
            }
            monkeypatch.setattr(owner_control_module, "iso_now", lambda: "2026-10-31T20:00:30Z")
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:01:01Z")
            blocker = sqlite3.connect(engine.db_path, timeout=5.0, check_same_thread=False)
            blocker.execute("BEGIN IMMEDIATE")
            engine.conn = _BeginImmediateProbe(engine.conn, begin_attempted)  # type: ignore[assignment]
            contender = asyncio.create_task(
                service.resolve(
                    contradiction_key=key,
                    observed_group_token=token_before,
                    winner_memory_id=winner["memory_id"],
                    winner_record_id=winner["record_id"],
                    winner_expected_revision=1,
                    idempotency_key="resolve-lock-wait-expired",
                    reason_code="STRONGER_EVIDENCE",
                    verification=verification,
                    scope="PROJECT",
                    project_id="project-a",
                )
            )
            assert await asyncio.to_thread(begin_attempted.wait, 2)
            await asyncio.sleep(0)
            assert not contender.done()
            blocker.commit()
            with pytest.raises(AdapterError) as expired:
                await contender
            assert expired.value.code == "WINNER_PROOF_INVALID"
            assert begin_attempted.is_set()
            assert engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
            ).fetchone()[0] == "2026-10-31T20:01:01Z"
            assert {
                table: int(engine.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in counts_before
            } == counts_before
            assert (await engine.group_observation(scope, key))["public"]["observed_group_token"] == token_before
            assert (await engine.get_record(current["memory_id"], scope, history=True))[0]["lifecycle_state"] == "VALIDATED_CURRENT"
            assert (await engine.get_record(winner["memory_id"], scope, history=True))[0]["lifecycle_state"] == "CANDIDATE"
            assert (await engine.get_record(loser["memory_id"], scope, history=True))[0]["lifecycle_state"] == "CANDIDATE"
        finally:
            if blocker is not None:
                blocker.close()
            await engine.close()

    asyncio.run(run())


def test_resolve_group_drift_after_lock_wait_precedes_transactional_expiry(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        blocker: sqlite3.Connection | None = None
        begin_attempted = threading.Event()
        try:
            key = "resolve-lock-wait-group-drift"
            current = _record(
                marker="a2",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key=key,
                fact="Current before group drift",
            )
            winner = _record(
                marker="b2",
                project_id="project-a",
                contradiction_key=key,
                fact="Candidate whose proof also expires",
            )
            loser = _record(
                marker="c2",
                project_id="project-a",
                contradiction_key=key,
                fact="Candidate changed by concurrent writer",
            )
            for row in (current, winner, loser):
                await engine.write_record(row, [1.0] + [0.0] * 255)
            assert engine.conn is not None
            engine.conn.execute(
                "UPDATE adapter_meta SET value=? WHERE key='trust_clock_high_water_at'",
                ("2026-10-31T20:00:00Z",),
            )
            engine.conn.commit()
            scope = validate_scope("PROJECT", project_id="project-a")
            token_before = (await engine.group_observation(scope, key))["public"]["observed_group_token"]
            event_count_before = int(engine.conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0])
            operation_count_before = int(engine.conn.execute("SELECT COUNT(*) FROM lifecycle_operations").fetchone()[0])
            verification = {
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "lock-wait-group-drift-source",
                    "source_hash": "9" * 64,
                    "review_ref": "lock-wait-group-drift-review",
                    "extraction_method": "owner-check",
                },
                "freshness_policy": {"kind": "ttl", "ttl_seconds": 60},
                "source_timestamp": "2026-10-31T20:00:00Z",
                "last_verified_at": "2026-10-31T20:00:00Z",
                "verified_by": "OWNER",
                "tags": winner["tags"],
            }
            monkeypatch.setattr(owner_control_module, "iso_now", lambda: "2026-10-31T20:00:30Z")
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:01:01Z")
            blocker = sqlite3.connect(engine.db_path, timeout=5.0, check_same_thread=False)
            blocker.create_function("factlane_contract_v3_writer", 0, lambda: 1)
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "UPDATE adapter_records SET contradiction_state='NONE' WHERE record_id=?",
                (loser["record_id"],),
            )
            engine.conn = _BeginImmediateProbe(engine.conn, begin_attempted)  # type: ignore[assignment]
            contender = asyncio.create_task(
                service.resolve(
                    contradiction_key=key,
                    observed_group_token=token_before,
                    winner_memory_id=winner["memory_id"],
                    winner_record_id=winner["record_id"],
                    winner_expected_revision=1,
                    idempotency_key="resolve-lock-wait-group-drift",
                    reason_code="STRONGER_EVIDENCE",
                    verification=verification,
                    scope="PROJECT",
                    project_id="project-a",
                )
            )
            assert await asyncio.to_thread(begin_attempted.wait, 2)
            await asyncio.sleep(0)
            assert not contender.done()
            blocker.commit()
            with pytest.raises(AdapterError) as drift:
                await contender
            assert drift.value.code == "GROUP_VERSION_CONFLICT"
            assert begin_attempted.is_set()
            assert engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
            ).fetchone()[0] == "2026-10-31T20:00:00Z"
            assert int(engine.conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0]) == event_count_before
            assert int(engine.conn.execute("SELECT COUNT(*) FROM lifecycle_operations").fetchone()[0]) == operation_count_before
            assert (await engine.get_record(current["memory_id"], scope, history=True))[0]["lifecycle_state"] == "VALIDATED_CURRENT"
            assert (await engine.get_record(winner["memory_id"], scope, history=True))[0]["lifecycle_state"] == "CANDIDATE"
            assert (await engine.get_record(loser["memory_id"], scope, history=True))[0]["lifecycle_state"] == "CANDIDATE"
            assert (await engine.group_observation(scope, key))["public"]["observed_group_token"] != token_before
        finally:
            if blocker is not None:
                blocker.close()
            await engine.close()

    asyncio.run(run())


def test_readmit_builds_same_memory_successor_replays_and_does_not_persist_proof_in_operation(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            target = _record(
                marker="o",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="readmit-key",
                fact="Revoked immutable truth",
            )
            await engine.write_record(target, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            before_revoke = await engine.group_observation(scope, "readmit-key")
            await service.revoke(
                memory_id=target["memory_id"],
                expected_record_id=target["record_id"],
                expected_revision=1,
                observed_group_token=before_revoke["public"]["observed_group_token"],
                idempotency_key="owner-service-readmit-revoke",
                reason_code="OUTDATED",
                scope="PROJECT",
                project_id="project-a",
            )
            after_revoke = await engine.group_observation(scope, "readmit-key")
            verification = {
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "readmit-secret-source",
                    "source_hash": "2" * 64,
                    "review_ref": "readmit-secret-review",
                    "extraction_method": "readmit-secret-method",
                },
                "freshness_policy": {"kind": "manual"},
                "source_timestamp": "2026-10-31T20:30:00Z",
                "last_verified_at": "2026-10-31T20:31:00Z",
                "verified_by": "OWNER",
                "tags": target["tags"],
            }
            result = await service.readmit(
                memory_id=target["memory_id"],
                revoked_record_id=target["record_id"],
                expected_revoked_revision=1,
                observed_group_token=after_revoke["public"]["observed_group_token"],
                idempotency_key="owner-service-readmit",
                verification=verification,
                scope="PROJECT",
                project_id="project-a",
                request_id="owner-readmit-request",
            )
            assert result["status"] == "OK"
            assert result["memory_id"] == target["memory_id"]
            assert result["revoked_record_id"] == target["record_id"]
            assert result["resulting_revision"] == 2
            assert result["resulting_lifecycle"] == "VALIDATED_CURRENT"
            history = await engine.get_record(target["memory_id"], scope, history=True)
            assert history[0]["memory_id"] == target["memory_id"]
            assert history[0]["parent_record_id"] == target["record_id"]
            assert history[0]["fact"] == target["fact"]
            assert history[0]["verified_by"] == "OWNER"

            assert engine.conn is not None
            operation = engine.conn.execute(
                "SELECT * FROM lifecycle_operations WHERE idempotency_key=?",
                ("owner-service-readmit",),
            ).fetchone()
            assert operation is not None
            durable = repr(tuple(operation))
            assert "readmit-secret-source" not in durable
            assert "readmit-secret-review" not in durable
            assert "readmit-secret-method" not in durable

            replay = await service.readmit(
                memory_id=target["memory_id"],
                revoked_record_id=target["record_id"],
                expected_revoked_revision=1,
                observed_group_token=after_revoke["public"]["observed_group_token"],
                idempotency_key="owner-service-readmit",
                verification=verification,
                scope="PROJECT",
                project_id="project-a",
                request_id="ignored-on-replay",
            )
            assert replay["idempotent_replay"] is True
            assert replay["operation_id"] == result["operation_id"]
            assert replay["request_id"] == "owner-readmit-request"
        finally:
            await engine.close()

    asyncio.run(run())


def test_readmit_rechecks_ttl_after_write_lock_wait_without_partial_successor(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        blocker: sqlite3.Connection | None = None
        begin_attempted = threading.Event()
        try:
            target = _record(
                marker="d1",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="readmit-lock-wait-expiry",
                fact="Revoked truth with expiring proof",
            )
            await engine.write_record(target, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            token = (await engine.group_observation(scope, "readmit-lock-wait-expiry"))["public"][
                "observed_group_token"
            ]
            await service.revoke(
                memory_id=target["memory_id"],
                expected_record_id=target["record_id"],
                expected_revision=1,
                observed_group_token=token,
                idempotency_key="readmit-lock-wait-revoke",
                reason_code="OUTDATED",
                scope="PROJECT",
                project_id="project-a",
            )
            assert engine.conn is not None
            engine.conn.execute(
                "UPDATE adapter_meta SET value=? WHERE key='trust_clock_high_water_at'",
                ("2026-10-31T20:00:00Z",),
            )
            engine.conn.commit()
            observed = await engine.group_observation(scope, "readmit-lock-wait-expiry")
            token_before = observed["public"]["observed_group_token"]
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
            verification = {
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "lock-wait-readmit-source",
                    "source_hash": "8" * 64,
                    "review_ref": "lock-wait-readmit-review",
                    "extraction_method": "owner-check",
                },
                "freshness_policy": {"kind": "ttl", "ttl_seconds": 60},
                "source_timestamp": "2026-10-31T20:00:00Z",
                "last_verified_at": "2026-10-31T20:00:00Z",
                "verified_by": "OWNER",
                "tags": target["tags"],
            }
            monkeypatch.setattr(owner_control_module, "iso_now", lambda: "2026-10-31T20:00:30Z")
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:01:01Z")
            blocker = sqlite3.connect(engine.db_path, timeout=5.0, check_same_thread=False)
            blocker.execute("BEGIN IMMEDIATE")
            engine.conn = _BeginImmediateProbe(engine.conn, begin_attempted)  # type: ignore[assignment]
            contender = asyncio.create_task(
                service.readmit(
                    memory_id=target["memory_id"],
                    revoked_record_id=target["record_id"],
                    expected_revoked_revision=1,
                    observed_group_token=token_before,
                    idempotency_key="readmit-lock-wait-expired",
                    verification=verification,
                    scope="PROJECT",
                    project_id="project-a",
                )
            )
            assert await asyncio.to_thread(begin_attempted.wait, 2)
            await asyncio.sleep(0)
            assert not contender.done()
            blocker.commit()
            with pytest.raises(AdapterError) as expired:
                await contender
            assert expired.value.code == "VERIFICATION_REQUIRED"
            assert begin_attempted.is_set()
            assert engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
            ).fetchone()[0] == "2026-10-31T20:01:01Z"
            assert {
                table: int(engine.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in counts_before
            } == counts_before
            assert (await engine.group_observation(scope, "readmit-lock-wait-expiry"))["public"][
                "observed_group_token"
            ] == token_before
            history = await engine.get_record(target["memory_id"], scope, history=True)
            assert len(history) == 1
            assert history[0]["lifecycle_state"] == "REVOKED"
        finally:
            if blocker is not None:
                blocker.close()
            await engine.close()

    asyncio.run(run())


def test_readmit_lineage_drift_after_lock_wait_precedes_transactional_expiry(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        blocker: sqlite3.Connection | None = None
        begin_attempted = threading.Event()
        try:
            target = _record(
                marker="d2",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="readmit-lock-wait-lineage-drift",
                fact="Revoked truth with concurrent descendant",
            )
            await engine.write_record(target, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            token = (await engine.group_observation(scope, "readmit-lock-wait-lineage-drift"))["public"][
                "observed_group_token"
            ]
            await service.revoke(
                memory_id=target["memory_id"],
                expected_record_id=target["record_id"],
                expected_revision=1,
                observed_group_token=token,
                idempotency_key="readmit-lineage-drift-revoke",
                reason_code="OUTDATED",
                scope="PROJECT",
                project_id="project-a",
            )
            assert engine.conn is not None
            engine.conn.execute(
                "UPDATE adapter_meta SET value=? WHERE key='trust_clock_high_water_at'",
                ("2026-10-31T20:00:00Z",),
            )
            engine.conn.commit()
            token_before = (await engine.group_observation(scope, "readmit-lock-wait-lineage-drift"))["public"][
                "observed_group_token"
            ]
            event_count_before = int(engine.conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0])
            operation_count_before = int(engine.conn.execute("SELECT COUNT(*) FROM lifecycle_operations").fetchone()[0])
            target_cursor = engine.conn.execute("SELECT * FROM adapter_records WHERE record_id=?", (target["record_id"],))
            target_row = target_cursor.fetchone()
            assert target_row is not None
            columns = [description[0] for description in target_cursor.description]
            child = dict(zip(columns, tuple(target_row), strict=True))
            child.update(
                {
                    "record_id": str(uuid.uuid4()),
                    "revision": 2,
                    "parent_record_id": target["record_id"],
                    "source_timestamp": None,
                    "last_verified_at": None,
                    "verified_by": "UNVERIFIED",
                    "authority_role": "UNRESOLVED",
                    "contradiction_state": "UNRESOLVED",
                    "confidence": 0.5,
                    "lifecycle_state": "CANDIDATE",
                    "native_content_hash": "e" * 64,
                    "payload_fingerprint": "f" * 64,
                    "idempotency_key": "readmit-lineage-drift-child",
                }
            )
            verification = {
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "lock-wait-lineage-drift-source",
                    "source_hash": "a" * 64,
                    "review_ref": "lock-wait-lineage-drift-review",
                    "extraction_method": "owner-check",
                },
                "freshness_policy": {"kind": "ttl", "ttl_seconds": 60},
                "source_timestamp": "2026-10-31T20:00:00Z",
                "last_verified_at": "2026-10-31T20:00:00Z",
                "verified_by": "OWNER",
                "tags": target["tags"],
            }
            monkeypatch.setattr(owner_control_module, "iso_now", lambda: "2026-10-31T20:00:30Z")
            monkeypatch.setattr(storage_module, "iso_now", lambda: "2026-10-31T20:01:01Z")
            blocker = sqlite3.connect(engine.db_path, timeout=5.0, check_same_thread=False)
            blocker.create_function("factlane_contract_v3_writer", 0, lambda: 1)
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                f"INSERT INTO adapter_records ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [child[column] for column in columns],
            )
            engine.conn = _BeginImmediateProbe(engine.conn, begin_attempted)  # type: ignore[assignment]
            contender = asyncio.create_task(
                service.readmit(
                    memory_id=target["memory_id"],
                    revoked_record_id=target["record_id"],
                    expected_revoked_revision=1,
                    observed_group_token=token_before,
                    idempotency_key="readmit-lock-wait-lineage-drift",
                    verification=verification,
                    scope="PROJECT",
                    project_id="project-a",
                )
            )
            assert await asyncio.to_thread(begin_attempted.wait, 2)
            await asyncio.sleep(0)
            assert not contender.done()
            blocker.commit()
            with pytest.raises(AdapterError) as drift:
                await contender
            assert drift.value.code == "LINEAGE_CHANGED"
            assert begin_attempted.is_set()
            assert engine.conn.execute(
                "SELECT value FROM adapter_meta WHERE key='trust_clock_high_water_at'"
            ).fetchone()[0] == "2026-10-31T20:00:00Z"
            assert int(engine.conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0]) == event_count_before
            assert int(engine.conn.execute("SELECT COUNT(*) FROM lifecycle_operations").fetchone()[0]) == operation_count_before
            history = await engine.get_record(target["memory_id"], scope, history=True)
            assert [row["lifecycle_state"] for row in history[:2]] == ["CANDIDATE", "REVOKED"]
            assert history[0]["record_id"] == child["record_id"]
        finally:
            if blocker is not None:
                blocker.close()
            await engine.close()

    asyncio.run(run())


def test_readmit_rejects_requested_lifecycle_and_subject_identity_change(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            target = _record(
                marker="p",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="readmit-identity-key",
                fact="Identity truth",
            )
            await engine.write_record(target, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            token = (await engine.group_observation(scope, "readmit-identity-key"))["public"]["observed_group_token"]
            await service.revoke(
                memory_id=target["memory_id"],
                expected_record_id=target["record_id"],
                expected_revision=1,
                observed_group_token=token,
                idempotency_key="readmit-identity-revoke",
                reason_code="OUTDATED",
                scope="PROJECT",
                project_id="project-a",
            )
            readmit_token = (await engine.group_observation(scope, "readmit-identity-key"))["public"]["observed_group_token"]
            base_verification = {
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "identity-source",
                    "source_hash": "3" * 64,
                    "review_ref": "identity-review",
                    "extraction_method": "owner-review",
                },
                "freshness_policy": {"kind": "manual"},
                "source_timestamp": "2026-10-31T20:40:00Z",
                "verified_by": "OWNER",
            }
            with pytest.raises(AdapterError) as lifecycle_error:
                await service.readmit(
                    memory_id=target["memory_id"],
                    revoked_record_id=target["record_id"],
                    expected_revoked_revision=1,
                    observed_group_token=readmit_token,
                    idempotency_key="readmit-invalid-lifecycle",
                    verification={**base_verification, "lifecycle_state": "VALIDATED_CURRENT"},
                    scope="PROJECT",
                    project_id="project-a",
                )
            assert lifecycle_error.value.code == "INVALID_ENVELOPE"

            with pytest.raises(AdapterError) as identity_error:
                await service.readmit(
                    memory_id=target["memory_id"],
                    revoked_record_id=target["record_id"],
                    expected_revoked_revision=1,
                    observed_group_token=readmit_token,
                    idempotency_key="readmit-invalid-subject",
                    verification={**base_verification, "subject": "different-subject"},
                    scope="PROJECT",
                    project_id="project-a",
                )
            assert identity_error.value.code == "SEMANTIC_IDENTITY_MISMATCH"
        finally:
            await engine.close()

    asyncio.run(run())


def test_readmit_rejects_stale_ttl_verification_before_current_admission(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            target = _record(
                marker="3",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="readmit-stale-proof-key",
                fact="Revoked truth requiring fresh re-admission proof",
            )
            await engine.write_record(target, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            token = (await engine.group_observation(scope, "readmit-stale-proof-key"))["public"]["observed_group_token"]
            await service.revoke(
                memory_id=target["memory_id"],
                expected_record_id=target["record_id"],
                expected_revision=1,
                observed_group_token=token,
                idempotency_key="readmit-stale-proof-revoke",
                reason_code="OUTDATED",
                scope="PROJECT",
                project_id="project-a",
            )
            readmit_token = (await engine.group_observation(scope, "readmit-stale-proof-key"))["public"]["observed_group_token"]
            monkeypatch.setattr(owner_control_module, "iso_now", lambda: "2026-10-31T22:00:00Z")
            verification = {
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "stale-readmit-source",
                    "source_hash": "8" * 64,
                    "review_ref": "stale-readmit-review",
                    "extraction_method": "owner-check",
                },
                "freshness_policy": {"kind": "ttl", "ttl_seconds": 60},
                "source_timestamp": "2026-10-31T20:00:00Z",
                "last_verified_at": "2026-10-31T20:00:00Z",
                "verified_by": "OWNER",
                "tags": target["tags"],
            }
            with pytest.raises(AdapterError) as stale:
                await service.readmit(
                    memory_id=target["memory_id"],
                    revoked_record_id=target["record_id"],
                    expected_revoked_revision=1,
                    observed_group_token=readmit_token,
                    idempotency_key="readmit-stale-proof",
                    verification=verification,
                    scope="PROJECT",
                    project_id="project-a",
                )
            assert stale.value.code == "VERIFICATION_REQUIRED"
            history = await engine.get_record(target["memory_id"], scope, history=True)
            assert len(history) == 1
            assert history[0]["lifecycle_state"] == "REVOKED"
        finally:
            await engine.close()

    asyncio.run(run())


def test_retention_status_fixed_metrics_are_exact_bounded_and_read_only(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            manual = _record(marker="q", project_id="project-a", lifecycle="VALIDATED_CURRENT")
            ttl = _record(marker="r", project_id="project-a", lifecycle="VALIDATED_CURRENT")
            ttl["last_verified_at"] = "2026-10-30T20:00:00Z"
            ttl["freshness_policy"] = {
                "kind": "ttl",
                "ttl_seconds": 60,
                "recheck_ref": None,
                "source_fingerprint": None,
            }
            superseded = _record(marker="s", project_id="project-a", lifecycle="VALIDATED_CURRENT")
            revoked = _record(marker="t", project_id="project-a", lifecycle="VALIDATED_CURRENT")
            rejected = _record(marker="u", project_id="project-a", lifecycle="CANDIDATE")
            outside = _record(marker="v", project_id="project-b", lifecycle="VALIDATED_CURRENT")
            for row in (manual, ttl, superseded, revoked, rejected, outside):
                await engine.write_record(row, [1.0] + [0.0] * 255)
            assert engine.conn is not None
            engine.conn.execute(
                "UPDATE adapter_records SET lifecycle_state='SUPERSEDED' WHERE record_id=?",
                (superseded["record_id"],),
            )
            engine.conn.commit()
            project_scope = validate_scope("PROJECT", project_id="project-a")
            revoke_token = (await engine.group_observation(project_scope, revoked["contradiction_key"]))["public"]["observed_group_token"]
            await service.revoke(
                memory_id=revoked["memory_id"],
                expected_record_id=revoked["record_id"],
                expected_revision=1,
                observed_group_token=revoke_token,
                idempotency_key="retention-revoke",
                reason_code="OUTDATED",
                scope="PROJECT",
                project_id="project-a",
            )
            reject_token = (await engine.group_observation(project_scope, rejected["contradiction_key"]))["public"]["observed_group_token"]
            await service.reject(
                memory_id=rejected["memory_id"],
                expected_record_id=rejected["record_id"],
                expected_revision=1,
                observed_group_token=reject_token,
                idempotency_key="retention-reject",
                reason_code="INSUFFICIENT_EVIDENCE",
                scope="PROJECT",
                project_id="project-a",
            )

            before_changes = engine.conn.total_changes
            result = await service.retention_status(scope="PROJECT", project_id="project-a")
            assert engine.conn.total_changes == before_changes
            assert result["status"] == "OK"
            assert result["scope"]["project_id"] == "project-a"
            counts = result["logical_counts"]
            assert counts["observation"] == "EXACT"
            assert tuple(counts["value"]) == (
                "CANDIDATE",
                "VALIDATED_CURRENT",
                "SUPERSEDED",
                "STALE",
                "QUARANTINED",
                "HISTORICAL",
                "REVOKED",
                "REJECTED",
            )
            assert counts["value"]["VALIDATED_CURRENT"] == 2
            assert counts["value"]["SUPERSEDED"] == 1
            assert counts["value"]["REVOKED"] == 1
            assert counts["value"]["REJECTED"] == 1
            assert counts["value"]["CANDIDATE"] == 0

            freshness = result["freshness_review_counts"]
            assert freshness["observation"] == "EXACT"
            assert freshness["value"]["manual_review"] == 1
            assert freshness["value"]["ttl_stale"] == 1
            assert freshness["value"]["on_change_source_change_required"] == 0
            assert freshness["value"]["on_change_unavailable"] == 0
            assert freshness["value"]["on_change_indeterminate"] == 0

            materialized = result["native_vector_materialized_by_state"]
            assert materialized["observation"] == "EXACT"
            assert materialized["value"]["VALIDATED_CURRENT"] == 2
            assert materialized["value"]["SUPERSEDED"] == 1
            assert materialized["value"]["REVOKED"] == 1
            assert materialized["value"]["REJECTED"] == 1
            assert result["history_only_total"]["value"] == 0
            assert result["compaction_ready"]["value"] == 1
            assert result["compaction_blocked_partial"]["value"] == 0
            assert result["terminal_native_vector_materialized"]["value"] == 2
            assert result["terminal_graph_linked"]["value"] == 0
            assert result["terminal_partial_materialization"]["value"] == 0

            capacity = result["capacity"]
            assert capacity["consistency"] == "OUTSIDE_DB_SNAPSHOT"
            assert capacity["observation"] == "KNOWN"
            assert isinstance(capacity["database_allocated_bytes"], int)
            assert isinstance(capacity["freelist_bytes"], int)
            assert len(canonical_json(result).encode("utf-8")) <= 16_384
        finally:
            await engine.close()

    asyncio.run(run())


def test_retention_status_scope_preflight_uses_plus_one_budget(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            await engine.write_record(_record(marker="w", project_id="project-a"), [1.0] + [0.0] * 255)
            await engine.write_record(_record(marker="x", project_id="project-a"), [1.0] + [0.0] * 255)
            monkeypatch.setattr(owner_control_module, "MAX_STATUS_SCOPE_ROWS", 1)
            result = await service.retention_status(scope="PROJECT", project_id="project-a")
            assert result["logical_counts"]["observation"] == "UNKNOWN"
            assert result["logical_counts"]["reason"] == "STATUS_SCAN_LIMIT_EXCEEDED"
            assert all(value is None for value in result["logical_counts"]["value"].values())
        finally:
            await engine.close()

    asyncio.run(run())


def test_retention_status_event_preflight_uses_plus_one_budget(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            row = _record(
                marker="y",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="retention-event-key",
            )
            source_hash = row["source_provenance"]["source_hash"]  # type: ignore[index]
            row["freshness_policy"] = {
                "kind": "on_change",
                "ttl_seconds": None,
                "recheck_ref": "source:retention-event",
                "source_fingerprint": source_hash,
            }
            await engine.write_record(row, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            token = (await engine.group_observation(scope, "retention-event-key"))["public"]["observed_group_token"]
            await service.manual_recheck(
                memory_id=row["memory_id"],
                expected_record_id=row["record_id"],
                expected_revision=1,
                observed_group_token=token,
                idempotency_key="retention-event-recheck",
                evidence={
                    "origin": "OWNER_SUPPLIED",
                    "claim": "UNAVAILABLE",
                    "recheck_ref": "source:retention-event",
                    "observed_at": "2026-10-31T20:01:00Z",
                    "method": "OWNER_REVIEWED_SOURCE",
                },
                scope="PROJECT",
                project_id="project-a",
            )
            monkeypatch.setattr(owner_control_module, "MAX_STATUS_EVENT_ROWS", 0)
            result = await service.retention_status(scope="PROJECT", project_id="project-a")
            assert result["logical_counts"]["observation"] == "EXACT"
            assert result["freshness_review_counts"]["observation"] == "UNKNOWN"
            assert result["freshness_review_counts"]["reason"] == "STATUS_SCAN_LIMIT_EXCEEDED"
        finally:
            await engine.close()

    asyncio.run(run())


def test_retention_status_history_only_and_graph_only_terminal_predicates(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            historical = _record(marker="z", project_id="project-a", lifecycle="VALIDATED_CURRENT")
            terminal = _record(marker="1", project_id="project-a", lifecycle="VALIDATED_CURRENT")
            peer = _record(marker="2", project_id="project-a", lifecycle="VALIDATED_CURRENT")
            for row in (historical, terminal, peer):
                await engine.write_record(row, [1.0] + [0.0] * 255)
            assert engine.conn is not None
            historical_native = engine.conn.execute(
                "SELECT id FROM memories WHERE content_hash=?",
                (historical["native_content_hash"],),
            ).fetchone()[0]
            terminal_native = engine.conn.execute(
                "SELECT id FROM memories WHERE content_hash=?",
                (terminal["native_content_hash"],),
            ).fetchone()[0]
            engine.conn.execute(
                "UPDATE adapter_records SET lifecycle_state='HISTORICAL' WHERE record_id=?",
                (historical["record_id"],),
            )
            engine.conn.execute(
                "UPDATE adapter_records SET lifecycle_state='REVOKED',contradiction_state='RESOLVED' WHERE record_id=?",
                (terminal["record_id"],),
            )
            engine.conn.execute("DELETE FROM memory_embeddings WHERE rowid=?", (historical_native,))
            engine.conn.execute("DELETE FROM memories WHERE id=?", (historical_native,))
            engine.conn.execute("DELETE FROM memory_embeddings WHERE rowid=?", (terminal_native,))
            engine.conn.execute("DELETE FROM memories WHERE id=?", (terminal_native,))
            engine.conn.execute(
                "INSERT INTO memory_graph(source_hash,target_hash,similarity,connection_types,metadata,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    terminal["native_content_hash"],
                    peer["native_content_hash"],
                    0.9,
                    '["related"]',
                    "{}",
                    1.0,
                ),
            )
            engine.conn.commit()

            result = await service.retention_status(scope="PROJECT", project_id="project-a")
            assert result["history_only_total"]["observation"] == "EXACT"
            assert result["history_only_total"]["value"] == 1
            assert result["terminal_native_vector_materialized"]["value"] == 0
            assert result["terminal_graph_linked"]["value"] == 1
            assert result["terminal_partial_materialization"]["value"] == 1
        finally:
            await engine.close()

    asyncio.run(run())


def test_retention_status_missing_required_scope_index_fails_closed(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            await engine.write_record(_record(marker="3", project_id="project-a"), [1.0] + [0.0] * 255)
            assert engine.conn is not None
            engine.conn.execute("DROP INDEX idx_adapter_scope_current")
            engine.conn.commit()
            result = await service.retention_status(scope="PROJECT", project_id="project-a")
            assert result["logical_counts"]["observation"] == "UNKNOWN"
            assert result["logical_counts"]["reason"] == "STATUS_INDEX_UNAVAILABLE"
            assert result["freshness_review_counts"]["reason"] == "STATUS_INDEX_UNAVAILABLE"
            assert result["native_vector_materialized_by_state"]["reason"] == "STATUS_INDEX_UNAVAILABLE"
        finally:
            await engine.close()

    asyncio.run(run())


def test_manual_recheck_uses_available_governed_core_primitive(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            row = _record(
                marker="n",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="recheck-key",
                fact="Observed source-backed truth",
            )
            source_hash = row["source_provenance"]["source_hash"]  # type: ignore[index]
            row["freshness_policy"] = {
                "kind": "on_change",
                "ttl_seconds": None,
                "recheck_ref": "source:owner-recheck",
                "source_fingerprint": source_hash,
            }
            await engine.write_record(row, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            observed = await engine.group_observation(scope, "recheck-key")
            result = await service.manual_recheck(
                memory_id=row["memory_id"],
                expected_record_id=row["record_id"],
                expected_revision=1,
                observed_group_token=observed["public"]["observed_group_token"],
                idempotency_key="owner-service-recheck",
                evidence={
                    "origin": "OWNER_SUPPLIED",
                    "claim": "UNAVAILABLE",
                    "recheck_ref": "source:owner-recheck",
                    "observed_at": "2026-10-31T20:01:00Z",
                    "method": "OWNER_REVIEWED_SOURCE",
                },
                scope="PROJECT",
                project_id="project-a",
            )
            assert result["status"] == "OK"
            assert result["outcome"] == "UNAVAILABLE"
            assert result["freshness_gate_state"] == "RECHECK_UNAVAILABLE"
            assert result["resulting_record_id"] == row["record_id"]
        finally:
            await engine.close()

    asyncio.run(run())


def test_reverify_requires_inspected_group_token_and_delegates_to_governed_update(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            row = _record(
                marker="4",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="owner-reverify-key",
            )
            await engine.write_record(row, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            token = (await engine.group_observation(scope, "owner-reverify-key"))["public"]["observed_group_token"]

            with pytest.raises(AdapterError) as stale:
                await service.reverify(
                    memory_id=row["memory_id"],
                    expected_record_id=row["record_id"],
                    expected_revision=1,
                    observed_group_token="0" * 64,
                    idempotency_key="owner-reverify-stale",
                    verification={"source_timestamp": "2026-10-31T20:00:00Z", "verified_by": "OWNER"},
                    scope="PROJECT",
                    project_id="project-a",
                )
            assert stale.value.code == "GROUP_VERSION_CONFLICT"
            assert (await engine.get_record(row["memory_id"], scope, history=True))[0]["record_id"] == row["record_id"]

            result = await service.reverify(
                memory_id=row["memory_id"],
                expected_record_id=row["record_id"],
                expected_revision=1,
                observed_group_token=token,
                idempotency_key="owner-reverify-ok",
                verification={"source_timestamp": "2026-10-31T20:00:00Z", "verified_by": "OWNER"},
                scope="PROJECT",
                project_id="project-a",
            )
            assert result["status"] == "OK"
            assert result["results"][0]["revision"] == 2
            assert result["results"][0]["parent_record_id"] == row["record_id"]
        finally:
            await engine.close()

    asyncio.run(run())


def test_replace_requires_inspected_group_token_and_delegates_to_governed_update(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            row = _record(
                marker="5",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="owner-replace-key",
            )
            await engine.write_record(row, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            token = (await engine.group_observation(scope, "owner-replace-key"))["public"]["observed_group_token"]
            replacement = {
                "fact": "Replacement owner-controlled fact",
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "owner-replace-source",
                    "source_hash": "9" * 64,
                    "review_ref": "owner-replace-review",
                    "extraction_method": "owner-check",
                },
                "freshness_policy": {"kind": "manual"},
                "source_timestamp": "2026-10-31T20:05:00Z",
                "verified_by": "OWNER",
                "subject": "replacement-owner-subject",
            }

            with pytest.raises(AdapterError) as stale:
                await service.replace(
                    memory_id=row["memory_id"],
                    expected_record_id=row["record_id"],
                    expected_revision=1,
                    observed_group_token="f" * 64,
                    idempotency_key="owner-replace-stale",
                    replacement=replacement,
                    scope="PROJECT",
                    project_id="project-a",
                )
            assert stale.value.code == "GROUP_VERSION_CONFLICT"

            result = await service.replace(
                memory_id=row["memory_id"],
                expected_record_id=row["record_id"],
                expected_revision=1,
                observed_group_token=token,
                idempotency_key="owner-replace-ok",
                replacement=replacement,
                scope="PROJECT",
                project_id="project-a",
            )
            assert result["status"] == "OK"
            assert result["results"][0]["fact"] == "Replacement owner-controlled fact"
            assert result["results"][0]["parent_record_id"] == row["record_id"]
            assert result["results"][0]["memory_id"] != row["memory_id"]
        finally:
            await engine.close()

    asyncio.run(run())


def test_reverify_replay_precedes_stale_group_cas_and_changed_payload_conflicts(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            row = _record(
                marker="6",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="owner-reverify-replay-key",
            )
            await engine.write_record(row, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            token = (await engine.group_observation(scope, "owner-reverify-replay-key"))["public"]["observed_group_token"]
            verification = {"source_timestamp": "2026-10-31T20:00:00Z", "verified_by": "OWNER"}

            first = await service.reverify(
                memory_id=row["memory_id"],
                expected_record_id=row["record_id"],
                expected_revision=1,
                observed_group_token=token,
                idempotency_key="owner-reverify-replay",
                verification=verification,
                scope="PROJECT",
                project_id="project-a",
            )
            assert first["results"][0]["revision"] == 2
            assert (await engine.group_observation(scope, "owner-reverify-replay-key"))["public"]["observed_group_token"] != token

            replay = await service.reverify(
                memory_id=row["memory_id"],
                expected_record_id=row["record_id"],
                expected_revision=1,
                observed_group_token=token,
                idempotency_key="owner-reverify-replay",
                verification=verification,
                scope="PROJECT",
                project_id="project-a",
            )
            assert replay["idempotent_replay"] is True
            assert replay["results"][0]["record_id"] == first["results"][0]["record_id"]

            with pytest.raises(AdapterError) as conflict:
                await service.reverify(
                    memory_id=row["memory_id"],
                    expected_record_id=row["record_id"],
                    expected_revision=1,
                    observed_group_token=token,
                    idempotency_key="owner-reverify-replay",
                    verification={**verification, "confidence": 0.9},
                    scope="PROJECT",
                    project_id="project-a",
                )
            assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
        finally:
            await engine.close()

    asyncio.run(run())


def test_replace_replay_precedes_stale_group_cas_and_changed_payload_conflicts(tmp_path) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            row = _record(
                marker="7",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="owner-replace-replay-key",
            )
            await engine.write_record(row, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            token = (await engine.group_observation(scope, "owner-replace-replay-key"))["public"]["observed_group_token"]
            replacement = {
                "fact": "Replacement replay fact",
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "owner-replace-replay-source",
                    "source_hash": "8" * 64,
                    "review_ref": "owner-replace-replay-review",
                    "extraction_method": "owner-check",
                },
                "freshness_policy": {"kind": "manual"},
                "source_timestamp": "2026-10-31T20:05:00Z",
                "verified_by": "OWNER",
                "subject": "replacement-replay-subject",
            }

            first = await service.replace(
                memory_id=row["memory_id"],
                expected_record_id=row["record_id"],
                expected_revision=1,
                observed_group_token=token,
                idempotency_key="owner-replace-replay",
                replacement=replacement,
                scope="PROJECT",
                project_id="project-a",
            )
            replay = await service.replace(
                memory_id=row["memory_id"],
                expected_record_id=row["record_id"],
                expected_revision=1,
                observed_group_token=token,
                idempotency_key="owner-replace-replay",
                replacement=replacement,
                scope="PROJECT",
                project_id="project-a",
            )
            assert replay["idempotent_replay"] is True
            assert replay["results"][0]["record_id"] == first["results"][0]["record_id"]

            with pytest.raises(AdapterError) as conflict:
                await service.replace(
                    memory_id=row["memory_id"],
                    expected_record_id=row["record_id"],
                    expected_revision=1,
                    observed_group_token=token,
                    idempotency_key="owner-replace-replay",
                    replacement={**replacement, "confidence": 0.9},
                    scope="PROJECT",
                    project_id="project-a",
                )
            assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
        finally:
            await engine.close()

    asyncio.run(run())


def test_reverify_rechecks_freshness_group_change_inside_storage_transaction(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            row = _record(
                marker="8",
                project_id="project-a",
                lifecycle="VALIDATED_CURRENT",
                contradiction_key="owner-reverify-freshness-race",
            )
            source_hash = row["source_provenance"]["source_hash"]  # type: ignore[index]
            row["freshness_policy"] = {
                "kind": "on_change",
                "ttl_seconds": None,
                "recheck_ref": "source:owner-race",
                "source_fingerprint": source_hash,
            }
            await engine.write_record(row, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            token = (await engine.group_observation(scope, "owner-reverify-freshness-race"))["public"]["observed_group_token"]
            original_write = engine.write_record
            injected = False

            async def racing_write(record, embedding, supersede_record_id=None, **kwargs):
                nonlocal injected
                if supersede_record_id is not None and not injected:
                    injected = True
                    outcome = await service.manual_recheck(
                        memory_id=row["memory_id"],
                        expected_record_id=row["record_id"],
                        expected_revision=1,
                        observed_group_token=token,
                        idempotency_key="owner-race-adverse-recheck",
                        evidence={
                            "origin": "OWNER_SUPPLIED",
                            "claim": "UNAVAILABLE",
                            "recheck_ref": "source:owner-race",
                            "observed_at": "2026-10-31T20:01:00Z",
                            "method": "OWNER_REVIEWED_SOURCE",
                        },
                        scope="PROJECT",
                        project_id="project-a",
                    )
                    assert outcome["freshness_gate_state"] == "RECHECK_UNAVAILABLE"
                return await original_write(record, embedding, supersede_record_id, **kwargs)

            monkeypatch.setattr(engine, "write_record", racing_write)
            with pytest.raises(AdapterError) as changed:
                await service.reverify(
                    memory_id=row["memory_id"],
                    expected_record_id=row["record_id"],
                    expected_revision=1,
                    observed_group_token=token,
                    idempotency_key="owner-reverify-freshness-race",
                    verification={"source_timestamp": "2026-10-31T20:00:00Z", "verified_by": "OWNER"},
                    scope="PROJECT",
                    project_id="project-a",
                )
            assert changed.value.code == "GROUP_VERSION_CONFLICT"
            history = await engine.get_record(row["memory_id"], scope, history=True)
            assert len(history) == 1
            assert history[0]["record_id"] == row["record_id"]
            assert history[0]["lifecycle_state"] == "VALIDATED_CURRENT"
        finally:
            await engine.close()

    asyncio.run(run())


def test_candidate_reverify_rechecks_group_arrival_inside_storage_transaction(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine, service = await _service(tmp_path)
        try:
            row = _record(
                marker="9",
                project_id="project-a",
                contradiction_key="owner-candidate-race",
                fact="Candidate target",
            )
            row["contradiction_state"] = "NONE"
            await engine.write_record(row, [1.0] + [0.0] * 255)
            scope = validate_scope("PROJECT", project_id="project-a")
            token = (await engine.group_observation(scope, "owner-candidate-race"))["public"]["observed_group_token"]
            original_write = engine.write_record
            original_promote = engine.promote_candidate
            injected = False

            async def racing_promote(record, embedding, promotion_scope, **kwargs):
                nonlocal injected
                if not injected:
                    injected = True
                    contender = _record(
                        marker="a",
                        project_id="project-a",
                        contradiction_key="owner-candidate-race",
                        fact="Incompatible contender",
                    )
                    await original_write(contender, [0.0, 1.0] + [0.0] * 254)
                return await original_promote(record, embedding, promotion_scope, **kwargs)

            monkeypatch.setattr(engine, "promote_candidate", racing_promote)
            with pytest.raises(AdapterError) as changed:
                await service.reverify(
                    memory_id=row["memory_id"],
                    expected_record_id=row["record_id"],
                    expected_revision=1,
                    observed_group_token=token,
                    idempotency_key="owner-candidate-race-reverify",
                    verification={"source_provenance": row["source_provenance"], "freshness_policy": row["freshness_policy"], "source_timestamp": "2026-10-31T20:02:00Z", "verified_by": "OWNER"},
                    scope="PROJECT",
                    project_id="project-a",
                )
            assert changed.value.code == "GROUP_VERSION_CONFLICT"
            history = await engine.get_record(row["memory_id"], scope, history=True)
            assert len(history) == 1
            assert history[0]["record_id"] == row["record_id"]
            assert history[0]["lifecycle_state"] == "CANDIDATE"
        finally:
            await engine.close()

    asyncio.run(run())
