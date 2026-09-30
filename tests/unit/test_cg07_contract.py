from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

import factlane.adapter as adapter_module
from factlane.adapter import MemoryAdapter, TrustedWriteContext, trusted_write_context_for_profile
from factlane.contract import AdapterError
from factlane.contract import validate_scope
from factlane.public_contract import PUBLIC_CONTRACT_REVISION, request_schema


def test_cross_project_workflow_is_an_exact_no_identity_scope() -> None:
    scope = validate_scope("CROSS_PROJECT_WORKFLOW")

    assert scope.scope == "CROSS_PROJECT_WORKFLOW"
    assert scope.project_id is None
    assert scope.worktree_id is None
    assert scope.workflow_id is None
    assert scope.agent_id is None


class _Provider:
    document_calls = 0
    query_calls = 0

    def provider_status(self) -> dict[str, object]:
        return {"local_only": True}


class _Engine:
    def __init__(self) -> None:
        self.idempotency_calls = 0

    async def find_idempotency(self, key: str):
        self.idempotency_calls += 1
        return None

    async def status(self, scope):
        return {"scope": scope.to_dict()}


def _adapter(profile: str = "delegated-candidate") -> MemoryAdapter:
    return MemoryAdapter(object(), _Provider(), trusted_write_context=trusted_write_context_for_profile(profile))  # type: ignore[arg-type]


def test_trusted_write_context_is_mandatory_and_deeply_immutable() -> None:
    with pytest.raises(TypeError):
        MemoryAdapter(object(), _Provider())  # type: ignore[call-arg,arg-type]

    context = trusted_write_context_for_profile("delegated-candidate")
    assert isinstance(context, TrustedWriteContext)
    assert isinstance(context.permitted_operations, frozenset)
    assert isinstance(context.permitted_scopes, frozenset)
    assert isinstance(context.permitted_verifications, frozenset)
    with pytest.raises(FrozenInstanceError):
        context.authorization_class = "DIRECT_OWNER_OPERATOR"  # type: ignore[misc]


def test_trusted_write_context_slot_is_assign_once_for_adapter_lifetime() -> None:
    adapter = MemoryAdapter(
        _Engine(),
        _Provider(),
        trusted_write_context=trusted_write_context_for_profile("read-only"),
    )  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        adapter._trusted_write_context = trusted_write_context_for_profile("owner-current")  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del adapter._trusted_write_context  # type: ignore[misc]
    assert adapter._trusted_write_context.authorization_class == "READ_ONLY"


def test_trusted_write_context_enforces_authorization_class_ceilings() -> None:
    invalid_contexts = [
        dict(
            authorization_class="READ_ONLY",
            permitted_operations=frozenset({"memory_store"}),
            permitted_scopes=frozenset({"CROSS_PROJECT_WORKFLOW"}),
            max_lifecycle="VALIDATED_CURRENT",
            permitted_verifications=frozenset({"OWNER"}),
            contributor_class="LEGACY_UNKNOWN",
            contributor_ref=None,
            grant_ref="invalid-read-only",
        ),
        dict(
            authorization_class="DELEGATED_AGENT_CONTRIBUTION",
            permitted_operations=frozenset({"memory_store", "memory_update"}),
            permitted_scopes=frozenset({"CROSS_PROJECT_WORKFLOW"}),
            max_lifecycle="VALIDATED_CURRENT",
            permitted_verifications=frozenset({"OWNER"}),
            contributor_class="DELEGATED_AGENT",
            contributor_ref=None,
            grant_ref="invalid-delegated-current",
        ),
    ]
    for values in invalid_contexts:
        with pytest.raises(AdapterError) as error:
            TrustedWriteContext(**values)
        assert error.value.code == "INVALID_WRITE_CONTEXT"


def test_compatibility_helper_preserves_explicit_null_identity_key() -> None:
    adapter = MemoryAdapter(
        _Engine(),
        _Provider(),
        trusted_write_context=trusted_write_context_for_profile("read-only"),
    )  # type: ignore[arg-type]
    with pytest.raises(AdapterError) as error:
        asyncio.run(adapter.status(scope="CROSS_PROJECT_WORKFLOW", project_id=None))
    assert error.value.code == "CROSS_SCOPE_DENIED"
    response = asyncio.run(adapter.status(scope="CROSS_PROJECT_WORKFLOW"))
    assert response["status"] == "OK"


def test_private_handler_rejects_reusable_module_policy_token() -> None:
    adapter = MemoryAdapter(
        _Engine(),
        _Provider(),
        trusted_write_context=trusted_write_context_for_profile("read-only"),
    )  # type: ignore[arg-type]
    leaked = getattr(adapter_module, "_POLICY_TOKEN", object())
    with pytest.raises(AdapterError) as error:
        asyncio.run(
            adapter._status(  # type: ignore[attr-defined]
                _policy_token=leaked,
                scope="CROSS_PROJECT_WORKFLOW",
            )
        )
    assert error.value.code == "POLICY_BYPASS_DENIED"


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("query", "", "INVALID_ENVELOPE"),
        ("top_k", 0, "INVALID_ENVELOPE"),
        ("retrieval_mode_kind", "INVALID_KIND", "INVALID_ENUM"),
    ],
)
def test_no_memory_needed_validates_complete_request_before_router_short_circuit(
    field: str, value: object, expected_code: str
) -> None:
    adapter = _adapter("read-only")
    request = {
        "query": "self-contained task",
        "intent_class": "GENERAL_TASK_NO_MEMORY_REQUIRED",
        "scope": "CROSS_PROJECT_WORKFLOW",
        "retrieval_mode": "CURRENT",
        "retrieval_mode_kind": "SEMANTIC",
        "top_k": 5,
        "max_memories": 5,
        "max_bytes": 6000,
        "max_tokens": 1200,
    }
    request[field] = value
    with pytest.raises(AdapterError) as error:
        asyncio.run(adapter.dispatch("memory_search", request))
    assert error.value.code == expected_code


@pytest.mark.parametrize("field", ["project_id", "worktree_id", "workflow_id", "agent_id"])
@pytest.mark.parametrize("value", [None, "", "value"])
def test_cross_project_workflow_raw_identity_key_presence_fails_closed(field: str, value: object) -> None:
    adapter = _adapter("read-only")
    with pytest.raises(AdapterError) as error:
        asyncio.run(adapter.dispatch("memory_status", {"scope": "CROSS_PROJECT_WORKFLOW", field: value}))
    assert error.value.code == "CROSS_SCOPE_DENIED"


def test_delegated_current_store_is_denied_before_idempotency_lookup() -> None:
    engine = _Engine()
    adapter = MemoryAdapter(
        engine,
        _Provider(),
        trusted_write_context=trusted_write_context_for_profile("delegated-candidate"),
    )  # type: ignore[arg-type]
    request = {
        "fact": "Cross-project rules are proposed as candidates by delegated agents.",
        "scope": "CROSS_PROJECT_WORKFLOW",
        "memory_type": "WORKFLOW_RULE",
        "source_provenance": {
            "source_class": "TEST",
            "source_ref": "cg07-policy",
            "source_hash": "a" * 64,
            "review_ref": "cg07-policy",
            "extraction_method": "test",
        },
        "freshness_policy": {"kind": "manual"},
        "idempotency_key": "cg07-delegated-current-denied",
        "requested_lifecycle_state": "VALIDATED_CURRENT",
        "source_timestamp": "2026-09-26T00:00:00Z",
        "last_verified_at": "2026-09-26T00:00:00Z",
        "verified_by": "OWNER",
    }
    with pytest.raises(AdapterError) as error:
        asyncio.run(adapter.dispatch("memory_store", request))
    assert error.value.code == "WRITE_AUTHORIZATION_DENIED"
    assert engine.idempotency_calls == 0


def test_public_contract_revision_two_exposes_candidate_parent_record_guard() -> None:
    assert PUBLIC_CONTRACT_REVISION == 2
    assert "expected_record_id" in request_schema("memory_update")["properties"]


def test_cross_project_freshness_uses_stable_scope_specific_error_codes() -> None:
    adapter = _adapter("delegated-candidate")
    base = {
        "fact": "Shared workflow doctrine is reviewed when its source changes.",
        "scope": "CROSS_PROJECT_WORKFLOW",
        "memory_type": "WORKFLOW_RULE",
        "idempotency_key": "cg07-freshness-errors",
        "source_provenance": {
            "source_class": "TEST",
            "source_ref": "cg07-freshness",
            "source_hash": "a" * 64,
            "review_ref": "cg07-freshness",
            "extraction_method": "test",
        },
    }
    with pytest.raises(AdapterError) as mismatch:
        asyncio.run(adapter.dispatch("memory_store", {**base, "source_provenance": {**base["source_provenance"], "source_fingerprint": "a" * 64}, "freshness_policy": {"kind": "manual"}}))
    assert mismatch.value.code == "SCOPE_FRESHNESS_MISMATCH"

    with pytest.raises(AdapterError) as invalid:
        asyncio.run(adapter.dispatch("memory_store", {**base, "idempotency_key": "cg07-freshness-errors-2", "freshness_policy": {"kind": "on_change", "recheck_ref": "", "source_fingerprint": "a" * 64}}))
    assert invalid.value.code == "INVALID_FRESHNESS"


@pytest.mark.parametrize("profile", ["repo-verifier", "automated-verifier"])
def test_non_owner_verifier_cannot_mint_owner_before_idempotency_lookup(profile: str) -> None:
    engine = _Engine()
    adapter = MemoryAdapter(
        engine,
        _Provider(),
        trusted_write_context=trusted_write_context_for_profile(profile),
    )  # type: ignore[arg-type]
    request = {
        "fact": "Verification classes may not impersonate Owner authority.",
        "scope": "CROSS_PROJECT_WORKFLOW",
        "memory_type": "WORKFLOW_RULE",
        "source_provenance": {
            "source_class": "TEST",
            "source_ref": "cg07-owner-impersonation",
            "source_hash": "c" * 64,
            "review_ref": "cg07-owner-impersonation",
            "extraction_method": "test",
        },
        "freshness_policy": {"kind": "manual"},
        "idempotency_key": f"cg07-owner-impersonation-{profile}",
        "requested_lifecycle_state": "VALIDATED_CURRENT",
        "source_timestamp": "2026-09-26T00:00:00Z",
        "last_verified_at": "2026-09-26T00:00:00Z",
        "verified_by": "OWNER",
    }
    with pytest.raises(AdapterError) as error:
        asyncio.run(adapter.dispatch("memory_store", request))
    assert error.value.code == "WRITE_AUTHORIZATION_DENIED"
    assert engine.idempotency_calls == 0


def test_policy_decision_is_bound_to_validated_operation_and_payload() -> None:
    async def operation_case() -> None:
        class OperationEngine:
            def __init__(self) -> None:
                self.adapter: MemoryAdapter | None = None
                self.idempotency_calls = 0

            async def status(self, scope):
                assert self.adapter is not None
                active = self.adapter._policy_decision_var.get()  # type: ignore[attr-defined]
                token = getattr(active, "token", active)
                await self.adapter._store(  # type: ignore[attr-defined]
                    _policy_token=token,
                    fact="A status decision cannot authorize a store.",
                    scope="CROSS_PROJECT_WORKFLOW",
                    memory_type="USER_FACT",
                    source_provenance={
                        "source_class": "TEST", "source_ref": "policy-op", "source_hash": "a" * 64,
                        "review_ref": "policy-op", "extraction_method": "test",
                    },
                    freshness_policy={"kind": "manual"},
                    idempotency_key="policy-op-bypass",
                )

            async def find_idempotency(self, key: str):
                self.idempotency_calls += 1
                raise AdapterError("BYPASS_REACHED", "operation-bound policy check was bypassed")

        engine = OperationEngine()
        adapter = MemoryAdapter(
            engine, _Provider(),
            trusted_write_context=trusted_write_context_for_profile("delegated-candidate"),
        )  # type: ignore[arg-type]
        engine.adapter = adapter
        with pytest.raises(AdapterError) as error:
            await adapter.dispatch("memory_status", {"scope": "CROSS_PROJECT_WORKFLOW"})
        assert error.value.code == "POLICY_BYPASS_DENIED"
        assert engine.idempotency_calls == 0

    async def payload_case() -> None:
        class PayloadEngine:
            def __init__(self) -> None:
                self.adapter: MemoryAdapter | None = None
                self.idempotency_calls = 0

            async def find_idempotency(self, key: str):
                self.idempotency_calls += 1
                if self.idempotency_calls == 1:
                    assert self.adapter is not None
                    active = self.adapter._policy_decision_var.get()  # type: ignore[attr-defined]
                    token = getattr(active, "token", active)
                    await self.adapter._store(  # type: ignore[attr-defined]
                        _policy_token=token,
                        fact="A different payload cannot reuse a validated store decision.",
                        scope="CROSS_PROJECT_WORKFLOW",
                        memory_type="WORKFLOW_RULE",
                        source_provenance={
                            "source_class": "TEST", "source_ref": "policy-payload-nested",
                            "source_hash": "b" * 64, "review_ref": "policy-payload-nested",
                            "extraction_method": "test",
                        },
                        freshness_policy={"kind": "manual"},
                        idempotency_key="policy-payload-nested",
                    )
                raise AdapterError("BYPASS_REACHED", "payload-bound policy check was bypassed")

        engine = PayloadEngine()
        adapter = MemoryAdapter(
            engine, _Provider(),
            trusted_write_context=trusted_write_context_for_profile("delegated-candidate"),
        )  # type: ignore[arg-type]
        engine.adapter = adapter
        request = {
            "fact": "The validated payload is bound to its dispatch decision.",
            "scope": "CROSS_PROJECT_WORKFLOW",
            "memory_type": "WORKFLOW_RULE",
            "source_provenance": {
                "source_class": "TEST", "source_ref": "policy-payload", "source_hash": "c" * 64,
                "review_ref": "policy-payload", "extraction_method": "test",
            },
            "freshness_policy": {"kind": "manual"},
            "idempotency_key": "policy-payload-outer",
        }
        with pytest.raises(AdapterError) as error:
            await adapter.dispatch("memory_store", request)
        assert error.value.code == "POLICY_BYPASS_DENIED"
        assert engine.idempotency_calls == 1

    asyncio.run(operation_case())
    asyncio.run(payload_case())


@pytest.mark.parametrize(
    "claim",
    ["effective_authority_role", "effective_lifecycle_state", "public_contract_revision_override"],
)
def test_direct_update_nested_reserved_claims_fail_before_idempotency(claim: str) -> None:
    class TripwireEngine:
        def __init__(self) -> None:
            self.idempotency_calls = 0

        async def find_idempotency(self, key: str):
            self.idempotency_calls += 1
            raise AdapterError("BYPASS_REACHED", "reserved nested claim reached idempotency")

    engine = TripwireEngine()
    adapter = MemoryAdapter(
        engine, _Provider(),
        trusted_write_context=trusted_write_context_for_profile("owner-current"),
    )  # type: ignore[arg-type]
    with pytest.raises(AdapterError) as error:
        asyncio.run(adapter.dispatch("memory_update", {
            "memory_id": "00000000-0000-0000-0000-000000000001",
            "scope": "CROSS_PROJECT_WORKFLOW",
            "expected_revision": 1,
            "mode": "REVERIFY",
            "idempotency_key": f"nested-reserved-{claim}",
            "verification": {"verified_by": "OWNER", claim: "spoofed"},
        }))
    assert error.value.code == "WRITE_AUTHORIZATION_DENIED"
    assert engine.idempotency_calls == 0


@pytest.mark.parametrize(
    ("verification", "expected_code"),
    [
        ({"verified_by": "OWNER", "memory_type": "USER_FACT"}, "SCOPE_TYPE_MISMATCH"),
        (
            {
                "verified_by": "OWNER",
                "freshness_policy": {"kind": "ttl", "ttl_seconds": 60},
            },
            "INVALID_FRESHNESS",
        ),
        (
            {
                "verified_by": "OWNER",
                "source_provenance": {
                    "source_class": "TEST",
                    "source_ref": "reverify-policy",
                    "source_hash": "d" * 64,
                    "review_ref": "reverify-policy",
                    "extraction_method": "test",
                    "source_fingerprint": "d" * 64,
                },
            },
            "SCOPE_FRESHNESS_MISMATCH",
        ),
    ],
)
def test_cross_project_reverify_semantics_fail_before_idempotency(
    verification: dict[str, object], expected_code: str
) -> None:
    class TripwireEngine:
        def __init__(self) -> None:
            self.idempotency_calls = 0

        async def find_idempotency(self, key: str):
            self.idempotency_calls += 1
            raise AdapterError("BYPASS_REACHED", "invalid reverify semantics reached idempotency")

    engine = TripwireEngine()
    adapter = MemoryAdapter(
        engine,
        _Provider(),
        trusted_write_context=trusted_write_context_for_profile("owner-current"),
    )  # type: ignore[arg-type]
    with pytest.raises(AdapterError) as error:
        asyncio.run(
            adapter.dispatch(
                "memory_update",
                {
                    "memory_id": "00000000-0000-0000-0000-000000000001",
                    "scope": "CROSS_PROJECT_WORKFLOW",
                    "expected_revision": 1,
                    "mode": "REVERIFY",
                    "idempotency_key": f"reverify-policy-{expected_code}",
                    "verification": verification,
                },
            )
        )
    assert error.value.code == expected_code
    assert engine.idempotency_calls == 0
