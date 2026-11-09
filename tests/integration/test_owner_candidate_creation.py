from __future__ import annotations

import asyncio

import pytest

from factlane.adapter import MemoryAdapter, trusted_write_context_for_profile
from factlane.embeddings import EmbeddingProfile
from factlane.owner_control import OwnerControlService
from factlane.storage import SQLiteVecEngine


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="owner-candidate-test-256",
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


def test_owner_create_candidate_forces_unverified_candidate_and_is_idempotent(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "candidate.db"), _profile())
        await engine.open()
        try:
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile(
                    "owner-current", contributor_ref="owner-webui-test"
                ),
            )
            service = OwnerControlService(adapter)
            provenance = {
                "source_class": "TEST",
                "source_ref": "fixture:owner-create",
                "source_hash": "a" * 64,
                "review_ref": "review:owner-create",
                "extraction_method": "explicit-owner-form",
            }

            first = await service.create_candidate(
                fact="The owner explicitly requested this candidate.",
                scope="PROJECT",
                project_id="factlane",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance=provenance,
                freshness_policy={"kind": "manual"},
                source_timestamp="2026-11-05T20:00:00Z",
                idempotency_key="owner-create-candidate-1",
                tags=["subject:owner-create"],
            )
            assert first["status"] == "OK"
            created = first["results"][0]
            assert created["scope"]["scope"] == "PROJECT"
            assert created["scope"]["project_id"] == "factlane"
            assert created["lifecycle_state"] == "CANDIDATE"
            assert created["verified_by"] == "UNVERIFIED"
            assert created["authority_role"] == "UNRESOLVED"
            assert created["fact"] == "The owner explicitly requested this candidate."
            assert created["source_provenance"] == provenance

            replay = await service.create_candidate(
                fact="The owner explicitly requested this candidate.",
                scope="PROJECT",
                project_id="factlane",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance=provenance,
                freshness_policy={"kind": "manual"},
                source_timestamp="2026-11-05T20:00:00Z",
                idempotency_key="owner-create-candidate-1",
                tags=["subject:owner-create"],
            )
            assert replay["idempotent_replay"] is True
            assert replay["results"][0]["record_id"] == created["record_id"]

            inventory = await service.inventory(scope="PROJECT", project_id="factlane")
            matching = [item for item in inventory["items"] if item["memory_id"] == created["memory_id"]]
            assert len(matching) == 1
            assert matching[0]["lifecycle_state"] == "CANDIDATE"
        finally:
            await engine.close()

    asyncio.run(run())


def test_candidate_approval_requires_explicit_complete_owner_verification(tmp_path) -> None:
    from factlane.contract import AdapterError, validate_scope

    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "candidate-approval.db"), _profile())
        await engine.open()
        try:
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile(
                    "owner-current", contributor_ref="owner-webui-test"
                ),
            )
            service = OwnerControlService(adapter)
            provenance = {
                "source_class": "TEST",
                "source_ref": "fixture:candidate-approval",
                "source_hash": "b" * 64,
                "review_ref": "review:candidate-approval",
                "extraction_method": "explicit-owner-form",
            }
            created = await service.create_candidate(
                fact="Candidate approval needs fresh explicit evidence.",
                scope="PROJECT",
                project_id="factlane",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance=provenance,
                freshness_policy={"kind": "manual"},
                source_timestamp="2026-11-05T20:00:00Z",
                idempotency_key="owner-candidate-approval-create",
                tags=["subject:candidate-approval"],
            )
            record = created["results"][0]
            inventory = await service.inventory(scope="PROJECT", project_id="factlane")
            item = next(row for row in inventory["items"] if row["memory_id"] == record["memory_id"])
            token = item["group_cas"]["observed_group_token"]
            complete = {
                "source_provenance": provenance,
                "freshness_policy": {"kind": "manual"},
                "source_timestamp": "2026-11-05T20:00:00Z",
                "verified_by": "OWNER",
            }

            for missing in ("source_provenance", "freshness_policy", "source_timestamp"):
                verification = {key: value for key, value in complete.items() if key != missing}
                try:
                    await service.reverify(
                        memory_id=record["memory_id"],
                        expected_record_id=record["record_id"],
                        expected_revision=1,
                        observed_group_token=token,
                        idempotency_key=f"owner-candidate-missing-{missing}",
                        verification=verification,
                        scope="PROJECT",
                        project_id="factlane",
                    )
                except AdapterError as exc:
                    assert exc.code == "VERIFICATION_REQUIRED"
                    assert missing in exc.safe_message
                else:
                    raise AssertionError(f"candidate approval unexpectedly accepted without {missing}")

            scope = validate_scope("PROJECT", project_id="factlane")
            history = await engine.get_record(record["memory_id"], scope, history=True)
            assert len(history) == 1
            assert history[0]["record_id"] == record["record_id"]
            assert history[0]["lifecycle_state"] == "CANDIDATE"
        finally:
            await engine.close()

    asyncio.run(run())


def test_batch_review_is_exact_scope_bounded_and_reports_concurrent_conflicts(tmp_path) -> None:
    from factlane.contract import AdapterError, validate_scope

    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "batch-review.db"), _profile())
        await engine.open()
        try:
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile(
                    "owner-current", contributor_ref="owner-webui-test"
                ),
            )
            service = OwnerControlService(adapter)

            async def create(label: str, fact: str):
                provenance = {
                    "source_class": "TEST",
                    "source_ref": f"fixture:{label}",
                    "source_hash": (label[0] * 64),
                    "review_ref": f"review:{label}",
                    "extraction_method": "explicit-owner-form",
                }
                result = await service.create_candidate(
                    fact=fact,
                    scope="PROJECT",
                    project_id="factlane",
                    memory_type="PROJECT_LEARNED_FACT",
                    source_provenance=provenance,
                    freshness_policy={"kind": "manual"},
                    source_timestamp="2026-11-05T20:00:00Z",
                    idempotency_key=f"create-{label}",
                    tags=[f"subject:{label}"],
                )
                return result["results"][0], provenance

            first, first_provenance = await create("batch-a", "Batch candidate A")
            second, second_provenance = await create("batch-b", "Batch candidate B")
            inventory = await service.inventory(scope="PROJECT", project_id="factlane")
            by_id = {item["memory_id"]: item for item in inventory["items"]}
            first_token = by_id[first["memory_id"]]["group_cas"]["observed_group_token"]
            second_token = by_id[second["memory_id"]]["group_cas"]["observed_group_token"]

            # Change only the second contradiction group after it was previewed.
            await service.create_candidate(
                fact="Incompatible concurrent contender",
                scope="PROJECT",
                project_id="factlane",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance={
                    "source_class": "TEST",
                    "source_ref": "fixture:batch-b-concurrent",
                    "source_hash": "c" * 64,
                    "review_ref": "review:batch-b-concurrent",
                    "extraction_method": "explicit-owner-form",
                },
                freshness_policy={"kind": "manual"},
                source_timestamp="2026-11-05T20:00:30Z",
                idempotency_key="create-batch-b-concurrent",
                tags=["subject:batch-b"],
            )

            result = await service.batch_review(
                scope="PROJECT",
                project_id="factlane",
                items=[
                    {
                        "action": "APPROVE",
                        "memory_id": first["memory_id"],
                        "expected_record_id": first["record_id"],
                        "expected_revision": 1,
                        "observed_group_token": first_token,
                        "idempotency_key": "batch-approve-a",
                        "verification": {
                            "source_provenance": first_provenance,
                            "freshness_policy": {"kind": "manual"},
                            "source_timestamp": "2026-11-05T20:01:00Z",
                            "verified_by": "OWNER",
                        },
                    },
                    {
                        "action": "APPROVE",
                        "memory_id": second["memory_id"],
                        "expected_record_id": second["record_id"],
                        "expected_revision": 1,
                        "observed_group_token": second_token,
                        "idempotency_key": "batch-approve-b",
                        "verification": {
                            "source_provenance": second_provenance,
                            "freshness_policy": {"kind": "manual"},
                            "source_timestamp": "2026-11-05T20:01:00Z",
                            "verified_by": "OWNER",
                        },
                    },
                ],
            )
            assert result["status"] == "PARTIAL"
            assert result["counts"] == {"applied": 1, "conflicted": 1, "failed": 0, "unattempted": 0}
            assert result["outcomes"][0]["status"] == "APPLIED"
            assert result["outcomes"][1]["status"] == "CONFLICTED"
            assert result["outcomes"][1]["error_code"] == "GROUP_VERSION_CONFLICT"

            scope = validate_scope("PROJECT", project_id="factlane")
            first_history = await engine.get_record(first["memory_id"], scope, history=True)
            assert any(row["lifecycle_state"] == "VALIDATED_CURRENT" for row in first_history)
            second_history = await engine.get_record(second["memory_id"], scope, history=True)
            assert all(row["lifecycle_state"] == "CANDIDATE" for row in second_history)

            # Scope override and oversize selection fail before any write attempt.
            with pytest.raises(AdapterError) as mixed:
                await service.batch_review(
                    scope="PROJECT",
                    project_id="factlane",
                    items=[{
                        "action": "APPROVE",
                        "memory_id": second["memory_id"],
                        "expected_record_id": second["record_id"],
                        "expected_revision": 1,
                        "observed_group_token": second_token,
                        "idempotency_key": "batch-mixed-scope",
                        "verification": {},
                        "project_id": "other-project",
                    }],
                )
            assert mixed.value.code == "CROSS_SCOPE_DENIED"

            with pytest.raises(AdapterError) as oversized:
                await service.batch_review(
                    scope="PROJECT",
                    project_id="factlane",
                    items=[{"action": "REJECT"}] * 51,
                )
            assert oversized.value.code == "LIMIT_EXCEEDED"
        finally:
            await engine.close()

    asyncio.run(run())


def test_batch_review_systemic_failure_marks_later_items_unattempted(tmp_path) -> None:
    from factlane.contract import AdapterError, validate_scope

    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "batch-systemic-stop.db"), _profile())
        await engine.open()
        try:
            provider = _Provider()
            adapter = MemoryAdapter(
                engine,
                provider,
                trusted_write_context=trusted_write_context_for_profile(
                    "owner-current", contributor_ref="owner-webui-test"
                ),
            )
            service = OwnerControlService(adapter)
            provenance = {
                "source_class": "TEST",
                "source_ref": "fixture:batch-systemic-stop",
                "source_hash": "7" * 64,
                "review_ref": "review:batch-systemic-stop",
                "extraction_method": "explicit-owner-form",
            }

            records = []
            for index in (1, 2, 3):
                created = await service.create_candidate(
                    fact=f"Systemic-stop candidate {index}",
                    scope="PROJECT",
                    project_id="factlane",
                    memory_type="PROJECT_LEARNED_FACT",
                    source_provenance=provenance,
                    freshness_policy={"kind": "manual"},
                    source_timestamp="2026-11-05T20:00:00Z",
                    idempotency_key=f"batch-systemic-create-{index}",
                    tags=[f"subject:batch-systemic-{index}"],
                )
                records.append(created["results"][0])

            inventory = await service.inventory(scope="PROJECT", project_id="factlane")
            by_id = {item["memory_id"]: item for item in inventory["items"]}
            review_embedding_calls = 0
            embed_documents = provider.embed_documents

            def fail_second_review_embedding(texts: list[str]) -> list[list[float]]:
                nonlocal review_embedding_calls
                review_embedding_calls += 1
                if review_embedding_calls == 2:
                    raise AdapterError("EMBEDDING_UNAVAILABLE", "test provider unavailable")
                return embed_documents(texts)

            provider.embed_documents = fail_second_review_embedding  # type: ignore[method-assign]
            verification = {
                "source_provenance": provenance,
                "freshness_policy": {"kind": "manual"},
                "source_timestamp": "2026-11-05T20:01:00Z",
                "verified_by": "OWNER",
            }
            items = [
                {
                    "action": "APPROVE",
                    "memory_id": record["memory_id"],
                    "expected_record_id": record["record_id"],
                    "expected_revision": 1,
                    "observed_group_token": by_id[record["memory_id"]]["group_cas"]["observed_group_token"],
                    "idempotency_key": f"batch-systemic-approve-{index}",
                    "verification": dict(verification),
                }
                for index, record in enumerate(records, start=1)
            ]

            result = await service.batch_review(scope="PROJECT", project_id="factlane", items=items)

            assert result["status"] == "PARTIAL"
            assert result["counts"] == {"applied": 1, "conflicted": 0, "failed": 1, "unattempted": 1}
            assert [outcome["status"] for outcome in result["outcomes"]] == [
                "APPLIED",
                "FAILED",
                "UNATTEMPTED",
            ]
            assert result["outcomes"][1]["error_code"] == "EMBEDDING_UNAVAILABLE"
            assert result["outcomes"][2]["blocked_by_error_code"] == "EMBEDDING_UNAVAILABLE"
            assert review_embedding_calls == 2

            scope = validate_scope("PROJECT", project_id="factlane")
            first_history = await engine.get_record(records[0]["memory_id"], scope, history=True)
            second_history = await engine.get_record(records[1]["memory_id"], scope, history=True)
            third_history = await engine.get_record(records[2]["memory_id"], scope, history=True)
            assert any(row["lifecycle_state"] == "VALIDATED_CURRENT" for row in first_history)
            assert [(row["record_id"], row["lifecycle_state"]) for row in second_history] == [
                (records[1]["record_id"], "CANDIDATE")
            ]
            assert [(row["record_id"], row["lifecycle_state"]) for row in third_history] == [
                (records[2]["record_id"], "CANDIDATE")
            ]
        finally:
            await engine.close()

    asyncio.run(run())


def test_batch_review_malformed_later_item_is_rejected_before_any_mutation(tmp_path) -> None:
    from factlane.contract import AdapterError, validate_scope

    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "batch-preflight.db"), _profile())
        await engine.open()
        try:
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile(
                    "owner-current", contributor_ref="owner-webui-test"
                ),
            )
            service = OwnerControlService(adapter)
            provenance = {
                "source_class": "TEST",
                "source_ref": "fixture:batch-preflight",
                "source_hash": "d" * 64,
                "review_ref": "review:batch-preflight",
                "extraction_method": "explicit-owner-form",
            }

            records = []
            for index in (1, 2):
                created = await service.create_candidate(
                    fact=f"Batch preflight candidate {index}",
                    scope="PROJECT",
                    project_id="factlane",
                    memory_type="PROJECT_LEARNED_FACT",
                    source_provenance=provenance,
                    freshness_policy={"kind": "manual"},
                    source_timestamp="2026-11-05T20:00:00Z",
                    idempotency_key=f"batch-preflight-create-{index}",
                    tags=[f"subject:batch-preflight-{index}"],
                )
                records.append(created["results"][0])

            inventory = await service.inventory(scope="PROJECT", project_id="factlane")
            by_id = {item["memory_id"]: item for item in inventory["items"]}
            complete = {
                "source_provenance": provenance,
                "freshness_policy": {"kind": "manual"},
                "source_timestamp": "2026-11-05T20:01:00Z",
                "verified_by": "OWNER",
            }
            items = []
            for index, record in enumerate(records, start=1):
                items.append(
                    {
                        "action": "APPROVE",
                        "memory_id": record["memory_id"],
                        "expected_record_id": record["record_id"],
                        "expected_revision": 1,
                        "observed_group_token": by_id[record["memory_id"]]["group_cas"]["observed_group_token"],
                        "idempotency_key": f"batch-preflight-approve-{index}",
                        "verification": dict(complete),
                    }
                )
            del items[1]["verification"]["source_provenance"]

            with pytest.raises(AdapterError) as invalid:
                await service.batch_review(scope="PROJECT", project_id="factlane", items=items)
            assert invalid.value.code == "VERIFICATION_REQUIRED"

            scope = validate_scope("PROJECT", project_id="factlane")
            for record in records:
                history = await engine.get_record(record["memory_id"], scope, history=True)
                assert len(history) == 1
                assert history[0]["record_id"] == record["record_id"]
                assert history[0]["lifecycle_state"] == "CANDIDATE"
        finally:
            await engine.close()

    asyncio.run(run())


def test_batch_review_semantic_cross_scope_target_blocks_before_first_mutation(tmp_path) -> None:
    from factlane.contract import AdapterError, validate_scope

    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "batch-cross-scope.db"), _profile())
        await engine.open()
        try:
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile(
                    "owner-current", contributor_ref="owner-webui-test"
                ),
            )
            service = OwnerControlService(adapter)
            provenance = {
                "source_class": "TEST",
                "source_ref": "fixture:batch-cross-scope",
                "source_hash": "e" * 64,
                "review_ref": "review:batch-cross-scope",
                "extraction_method": "explicit-owner-form",
            }

            async def create(project_id: str, suffix: str):
                created = await service.create_candidate(
                    fact=f"Cross-scope candidate {suffix}",
                    scope="PROJECT",
                    project_id=project_id,
                    memory_type="PROJECT_LEARNED_FACT",
                    source_provenance=provenance,
                    freshness_policy={"kind": "manual"},
                    source_timestamp="2026-11-05T20:00:00Z",
                    idempotency_key=f"cross-scope-create-{suffix}",
                    tags=[f"subject:cross-scope-{suffix}"],
                )
                record = created["results"][0]
                inv = await service.inventory(scope="PROJECT", project_id=project_id)
                item = next(row for row in inv["items"] if row["memory_id"] == record["memory_id"])
                return record, item["group_cas"]["observed_group_token"]

            local, local_token = await create("factlane", "local")
            foreign, foreign_token = await create("other", "foreign")
            verification = {
                "source_provenance": provenance,
                "freshness_policy": {"kind": "manual"},
                "source_timestamp": "2026-11-05T20:01:00Z",
                "verified_by": "OWNER",
            }
            items = [
                {
                    "action": "APPROVE",
                    "memory_id": local["memory_id"],
                    "expected_record_id": local["record_id"],
                    "expected_revision": 1,
                    "observed_group_token": local_token,
                    "idempotency_key": "cross-scope-approve-local",
                    "verification": dict(verification),
                },
                {
                    "action": "APPROVE",
                    "memory_id": foreign["memory_id"],
                    "expected_record_id": foreign["record_id"],
                    "expected_revision": 1,
                    "observed_group_token": foreign_token,
                    "idempotency_key": "cross-scope-approve-foreign",
                    "verification": dict(verification),
                },
            ]

            with pytest.raises(AdapterError) as denied:
                await service.batch_review(scope="PROJECT", project_id="factlane", items=items)
            assert denied.value.code == "CROSS_SCOPE_DENIED"

            local_history = await engine.get_record(local["memory_id"], validate_scope("PROJECT", project_id="factlane"), history=True)
            assert len(local_history) == 1
            assert local_history[0]["lifecycle_state"] == "CANDIDATE"
        finally:
            await engine.close()

    asyncio.run(run())


def test_batch_review_classifies_idempotency_collision_as_conflict(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "batch-idempotency.db"), _profile())
        await engine.open()
        try:
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile(
                    "owner-current", contributor_ref="owner-webui-test"
                ),
            )
            service = OwnerControlService(adapter)
            provenance = {
                "source_class": "TEST",
                "source_ref": "fixture:batch-idempotency",
                "source_hash": "f" * 64,
                "review_ref": "review:batch-idempotency",
                "extraction_method": "explicit-owner-form",
            }

            async def create(suffix: str):
                result = await service.create_candidate(
                    fact=f"Idempotency candidate {suffix}",
                    scope="PROJECT",
                    project_id="factlane",
                    memory_type="PROJECT_LEARNED_FACT",
                    source_provenance=provenance,
                    freshness_policy={"kind": "manual"},
                    source_timestamp="2026-11-05T20:00:00Z",
                    idempotency_key=f"batch-idem-create-{suffix}",
                    tags=[f"subject:batch-idem-{suffix}"],
                )
                record = result["results"][0]
                inventory = await service.inventory(scope="PROJECT", project_id="factlane")
                item = next(row for row in inventory["items"] if row["memory_id"] == record["memory_id"])
                return record, item["group_cas"]["observed_group_token"]

            first, first_token = await create("first")
            second, second_token = await create("second")
            verification = {
                "source_provenance": provenance,
                "freshness_policy": {"kind": "manual"},
                "source_timestamp": "2026-11-05T20:01:00Z",
                "verified_by": "OWNER",
            }
            await service.reverify(
                memory_id=first["memory_id"],
                expected_record_id=first["record_id"],
                expected_revision=1,
                observed_group_token=first_token,
                idempotency_key="shared-review-key",
                verification=dict(verification),
                scope="PROJECT",
                project_id="factlane",
            )

            result = await service.batch_review(
                scope="PROJECT",
                project_id="factlane",
                items=[{
                    "action": "APPROVE",
                    "memory_id": second["memory_id"],
                    "expected_record_id": second["record_id"],
                    "expected_revision": 1,
                    "observed_group_token": second_token,
                    "idempotency_key": "shared-review-key",
                    "verification": dict(verification),
                }],
            )
            assert result["status"] == "PARTIAL"
            assert result["counts"]["conflicted"] == 1
            assert result["counts"]["failed"] == 0
            assert result["outcomes"][0]["status"] == "CONFLICTED"
            assert result["outcomes"][0]["error_code"] == "IDEMPOTENCY_CONFLICT"
        finally:
            await engine.close()

    asyncio.run(run())


def test_candidate_approval_rejects_future_last_verified_at(tmp_path) -> None:
    from factlane.contract import AdapterError, validate_scope

    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "candidate-future-verification.db"), _profile())
        await engine.open()
        try:
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile(
                    "owner-current", contributor_ref="owner-webui-test"
                ),
            )
            service = OwnerControlService(adapter)
            provenance = {
                "source_class": "TEST",
                "source_ref": "fixture:future-verification",
                "source_hash": "1" * 64,
                "review_ref": "review:future-verification",
                "extraction_method": "explicit-owner-form",
            }
            created = await service.create_candidate(
                fact="Future verification timestamps are invalid.",
                scope="PROJECT",
                project_id="factlane",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance=provenance,
                freshness_policy={"kind": "ttl", "ttl_seconds": 60},
                source_timestamp="2026-11-05T20:00:00Z",
                idempotency_key="future-verification-create",
                tags=["subject:future-verification"],
            )
            record = created["results"][0]
            inventory = await service.inventory(scope="PROJECT", project_id="factlane")
            token = next(row for row in inventory["items"] if row["memory_id"] == record["memory_id"])["group_cas"]["observed_group_token"]

            with pytest.raises(AdapterError) as invalid:
                await service.reverify(
                    memory_id=record["memory_id"],
                    expected_record_id=record["record_id"],
                    expected_revision=1,
                    observed_group_token=token,
                    idempotency_key="future-verification-approve",
                    verification={
                        "source_provenance": provenance,
                        "freshness_policy": {"kind": "ttl", "ttl_seconds": 60},
                        "source_timestamp": "2026-11-05T20:00:00Z",
                        "last_verified_at": "2099-01-01T00:00:00Z",
                        "verified_by": "OWNER",
                    },
                    scope="PROJECT",
                    project_id="factlane",
                )
            assert invalid.value.code == "VERIFICATION_REQUIRED"

            history = await engine.get_record(record["memory_id"], validate_scope("PROJECT", project_id="factlane"), history=True)
            assert len(history) == 1
            assert history[0]["lifecycle_state"] == "CANDIDATE"
        finally:
            await engine.close()

    asyncio.run(run())


def test_batch_review_exact_success_retry_returns_idempotent_applied_outcome(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "batch-replay.db"), _profile())
        await engine.open()
        try:
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile(
                    "owner-current", contributor_ref="owner-webui-test"
                ),
            )
            service = OwnerControlService(adapter)
            provenance = {
                "source_class": "TEST",
                "source_ref": "fixture:batch-replay",
                "source_hash": "2" * 64,
                "review_ref": "review:batch-replay",
                "extraction_method": "explicit-owner-form",
            }
            created = await service.create_candidate(
                fact="Batch retry must return the original success.",
                scope="PROJECT",
                project_id="factlane",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance=provenance,
                freshness_policy={"kind": "manual"},
                source_timestamp="2026-11-05T20:00:00Z",
                idempotency_key="batch-replay-create",
                tags=["subject:batch-replay"],
            )
            record = created["results"][0]
            inventory = await service.inventory(scope="PROJECT", project_id="factlane")
            token = next(row for row in inventory["items"] if row["memory_id"] == record["memory_id"])["group_cas"]["observed_group_token"]
            item = {
                "action": "APPROVE",
                "memory_id": record["memory_id"],
                "expected_record_id": record["record_id"],
                "expected_revision": 1,
                "observed_group_token": token,
                "idempotency_key": "batch-replay-approve",
                "verification": {
                    "source_provenance": provenance,
                    "freshness_policy": {"kind": "manual"},
                    "source_timestamp": "2026-11-05T20:01:00Z",
                    "verified_by": "OWNER",
                },
            }

            first = await service.batch_review(scope="PROJECT", project_id="factlane", items=[item])
            replay = await service.batch_review(scope="PROJECT", project_id="factlane", items=[item])
            assert first["status"] == "OK"
            assert replay["status"] == "OK"
            assert replay["counts"] == {"applied": 1, "conflicted": 0, "failed": 0, "unattempted": 0}
            assert replay["outcomes"][0]["status"] == "APPLIED"
            assert replay["outcomes"][0]["result"]["idempotent_replay"] is True
            assert replay["outcomes"][0]["result"]["results"][0]["record_id"] == first["outcomes"][0]["result"]["results"][0]["record_id"]
        finally:
            await engine.close()

    asyncio.run(run())


def test_batch_reject_exact_success_retry_returns_idempotent_applied_outcome(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "batch-reject-replay.db"), _profile())
        await engine.open()
        try:
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile(
                    "owner-current", contributor_ref="owner-webui-test"
                ),
            )
            service = OwnerControlService(adapter)
            provenance = {
                "source_class": "TEST",
                "source_ref": "fixture:batch-reject-replay",
                "source_hash": "3" * 64,
                "review_ref": "review:batch-reject-replay",
                "extraction_method": "explicit-owner-form",
            }
            created = await service.create_candidate(
                fact="Batch reject retry must return the original result.",
                scope="PROJECT",
                project_id="factlane",
                memory_type="PROJECT_LEARNED_FACT",
                source_provenance=provenance,
                freshness_policy={"kind": "manual"},
                source_timestamp="2026-11-05T20:00:00Z",
                idempotency_key="batch-reject-replay-create",
                tags=["subject:batch-reject-replay"],
            )
            record = created["results"][0]
            inventory = await service.inventory(scope="PROJECT", project_id="factlane")
            token = next(row for row in inventory["items"] if row["memory_id"] == record["memory_id"])["group_cas"]["observed_group_token"]
            item = {
                "action": "REJECT",
                "memory_id": record["memory_id"],
                "expected_record_id": record["record_id"],
                "expected_revision": 1,
                "observed_group_token": token,
                "idempotency_key": "batch-reject-replay-action",
                "reason_code": "INSUFFICIENT_EVIDENCE",
            }
            first = await service.batch_review(scope="PROJECT", project_id="factlane", items=[item])
            replay = await service.batch_review(scope="PROJECT", project_id="factlane", items=[item])
            assert first["status"] == "OK"
            assert replay["status"] == "OK"
            assert replay["counts"] == {"applied": 1, "conflicted": 0, "failed": 0, "unattempted": 0}
            assert replay["outcomes"][0]["result"]["idempotent_replay"] is True
            assert replay["outcomes"][0]["result"]["result_record_id"] == first["outcomes"][0]["result"]["result_record_id"]
        finally:
            await engine.close()

    asyncio.run(run())


def test_batch_reserved_idempotency_foreign_scope_still_aborts_before_first_mutation(tmp_path) -> None:
    from factlane.contract import AdapterError, validate_scope

    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "batch-reserved-cross-scope.db"), _profile())
        await engine.open()
        try:
            adapter = MemoryAdapter(
                engine,
                _Provider(),
                trusted_write_context=trusted_write_context_for_profile(
                    "owner-current", contributor_ref="owner-webui-test"
                ),
            )
            service = OwnerControlService(adapter)
            provenance = {
                "source_class": "TEST",
                "source_ref": "fixture:batch-reserved-cross-scope",
                "source_hash": "4" * 64,
                "review_ref": "review:batch-reserved-cross-scope",
                "extraction_method": "explicit-owner-form",
            }

            async def create(project_id: str, suffix: str):
                created = await service.create_candidate(
                    fact=f"Reserved cross-scope candidate {suffix}",
                    scope="PROJECT",
                    project_id=project_id,
                    memory_type="PROJECT_LEARNED_FACT",
                    source_provenance=provenance,
                    freshness_policy={"kind": "manual"},
                    source_timestamp="2026-11-05T20:00:00Z",
                    idempotency_key=f"reserved-cross-create-{suffix}",
                    tags=[f"subject:reserved-cross-{suffix}"],
                )
                record = created["results"][0]
                inv = await service.inventory(scope="PROJECT", project_id=project_id)
                token = next(row for row in inv["items"] if row["memory_id"] == record["memory_id"])["group_cas"]["observed_group_token"]
                return record, token

            local, local_token = await create("factlane", "local")
            foreign, foreign_token = await create("other", "foreign")
            verification = {
                "source_provenance": provenance,
                "freshness_policy": {"kind": "manual"},
                "source_timestamp": "2026-11-05T20:01:00Z",
                "verified_by": "OWNER",
            }
            await service.reverify(
                memory_id=foreign["memory_id"],
                expected_record_id=foreign["record_id"],
                expected_revision=1,
                observed_group_token=foreign_token,
                idempotency_key="foreign-reserved-approval",
                verification=dict(verification),
                scope="PROJECT",
                project_id="other",
            )

            items = [
                {
                    "action": "APPROVE",
                    "memory_id": local["memory_id"],
                    "expected_record_id": local["record_id"],
                    "expected_revision": 1,
                    "observed_group_token": local_token,
                    "idempotency_key": "local-batch-approval",
                    "verification": dict(verification),
                },
                {
                    "action": "APPROVE",
                    "memory_id": foreign["memory_id"],
                    "expected_record_id": foreign["record_id"],
                    "expected_revision": 1,
                    "observed_group_token": foreign_token,
                    "idempotency_key": "foreign-reserved-approval",
                    "verification": dict(verification),
                },
            ]
            with pytest.raises(AdapterError) as denied:
                await service.batch_review(scope="PROJECT", project_id="factlane", items=items)
            assert denied.value.code == "CROSS_SCOPE_DENIED"

            local_history = await engine.get_record(local["memory_id"], validate_scope("PROJECT", project_id="factlane"), history=True)
            assert len(local_history) == 1
            assert local_history[0]["lifecycle_state"] == "CANDIDATE"
        finally:
            await engine.close()

    asyncio.run(run())
