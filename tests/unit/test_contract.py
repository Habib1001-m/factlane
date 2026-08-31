from __future__ import annotations

import asyncio

import pytest

from factlane.adapter import MemoryAdapter
from factlane.contract import AdapterError, is_fresh, validate_scope
from factlane.embeddings import OllamaLocalProvider
from factlane.router import TruthRouter


def test_scope_requires_exact_identity() -> None:
    with pytest.raises(AdapterError) as error:
        validate_scope("PROJECT")
    assert error.value.code == "UNKNOWN_PROJECT_ID"
    assert validate_scope("PROJECT", project_id="project-alpha").project_id == "project-alpha"


def test_freshness_does_not_use_access_time() -> None:
    assert not is_fresh(
        {"kind": "ttl", "ttl_seconds": 10, "recheck_ref": None, "source_fingerprint": None},
        "2020-01-01T00:00:00Z",
        {"source_hash": "a" * 64},
    )


def test_router_can_skip_memory() -> None:
    scope = validate_scope("PROJECT", project_id="project-alpha")
    decision = TruthRouter().decide(
        intent_class="CURRENT_PROJECT_STATE",
        operation="memory_search",
        scope=scope,
        direct_truth_available=True,
    )
    assert decision.status == "NO_MEMORY_NEEDED"


def test_normal_agent_surface_is_exactly_five() -> None:
    assert MemoryAdapter.tool_names() == [
        "memory_search",
        "memory_get",
        "memory_store",
        "memory_update",
        "memory_status",
    ]


class _StatusEngine:
    async def status(self, scope: object) -> dict[str, bool]:
        return {"ready": True}


class _StatusProvider:
    document_calls = 0
    query_calls = 0

    def provider_status(self) -> dict[str, bool]:
        return {"ready": True}


def test_status_token_measurement_preserves_existing_metadata_contract() -> None:
    response = asyncio.run(MemoryAdapter(_StatusEngine(), _StatusProvider()).status(scope="PROJECT", project_id="p"))  # type: ignore[arg-type]

    assert response["token_measurement"]["codex_exact_equivalence"] == "UNVERIFIED"
    assert "exact_token_equivalence" not in response["token_measurement"]


def test_provider_rejects_remote_url() -> None:
    with pytest.raises(AdapterError):
        OllamaLocalProvider(
            model="all-minilm:l6-v2",
            profile_id="minilm-384",
            output_dimension=384,
            source_dimension=384,
            model_digest="1b226e2802dbb772b5fc32a58f103ca1804ef7501331012de126ab22f67475ef",
            base_url="https://example.invalid",
        )


def test_provider_profile_is_local_and_cosine() -> None:
    provider = OllamaLocalProvider(
        model="all-minilm:l6-v2",
        profile_id="minilm-384",
        output_dimension=384,
        source_dimension=384,
        model_digest="1b226e2802dbb772b5fc32a58f103ca1804ef7501331012de126ab22f67475ef",
    )
    assert provider.profile.provider_kind == "OLLAMA_LOCAL"
    assert provider.profile.distance_metric == "cosine"
