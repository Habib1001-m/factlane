from __future__ import annotations

import pytest

from factlane.adapter import MODEL_DIGESTS, PROFILE_DEFINITIONS
from factlane.contract import AdapterError
from factlane.embeddings import (
    OllamaLocalProvider,
    _context_length_from_model_info,
    _embedding_length_from_model_info,
)


NOMIC_DIGEST = "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f"
EMBEDDINGGEMMA_DIGEST = "85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1"


class RecordingProvider(OllamaLocalProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.requests: list[tuple[str, dict[str, object] | None]] = []

    def _request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        self.requests.append((path, payload))
        if path == "/api/embed":
            assert payload is not None
            dimensions = int(payload["dimensions"])
            inputs = payload["input"]
            assert isinstance(inputs, list)
            unit = [1.0] + [0.0] * (dimensions - 1)
            return {"embeddings": [unit[:] for _ in range(len(inputs))]}
        raise AssertionError(path)


def make_provider(provider_type=RecordingProvider):
    return provider_type(
        model="nomic-embed-text:latest",
        profile_id="nomic-256",
        output_dimension=256,
        source_dimension=768,
        model_digest=NOMIC_DIGEST,
        document_prefix="search_document: ",
        query_prefix="search_query: ",
    )


def test_nomic_prefixes_are_applied_exactly_once() -> None:
    provider = make_provider()
    provider.embed_documents(["alpha"])
    provider.embed_query("beta")
    assert provider.requests[0][1]["input"] == ["search_document: alpha"]
    assert provider.requests[1][1]["input"] == ["search_query: beta"]


def test_embed_requests_disable_provider_truncation() -> None:
    provider = make_provider()
    provider.embed_documents(["alpha"])
    assert provider.requests[0][1]["truncate"] is False


def test_context_length_prefers_explicit_nomic_key() -> None:
    assert _context_length_from_model_info({"nomic-bert.context_length": 2048}) == 2048


def test_context_length_accepts_suffix_fallback() -> None:
    assert _context_length_from_model_info({"custom.encoder.max_position_embeddings": 4096}) == 4096


def test_context_length_missing_is_unknown() -> None:
    assert _context_length_from_model_info({"nomic-bert.embedding_length": 768}) is None


def test_embedding_length_single_architecture_key_is_supported() -> None:
    assert _embedding_length_from_model_info({"gemma3.embedding_length": 768}) == 768


def test_embedding_length_missing_is_unknown() -> None:
    assert _embedding_length_from_model_info({"gemma3.context_length": 2048}) is None


def test_embedding_length_conflict_is_fail_closed() -> None:
    assert _embedding_length_from_model_info({"gemma3.embedding_length": 768, "bert.embedding_length": 384}) is None


def test_embedding_length_legacy_nomic_and_bert_keys_are_unchanged() -> None:
    assert _embedding_length_from_model_info({"nomic-bert.embedding_length": 768}) == 768
    assert _embedding_length_from_model_info({"bert.embedding_length": 384}) == 384


def test_selected_embeddinggemma_profile_definition_is_exact() -> None:
    definition = PROFILE_DEFINITIONS["embeddinggemma-300m-768"]
    assert MODEL_DIGESTS["embeddinggemma:300m"] == EMBEDDINGGEMMA_DIGEST
    assert definition == {
        "model": "embeddinggemma:300m",
        "output_dimension": 768,
        "source_dimension": 768,
        "document_prefix": "title: none | text: ",
        "query_prefix": "task: search result | query: ",
    }


class RejectingProvider(OllamaLocalProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.payloads: list[dict[str, object]] = []

    def _request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        if path != "/api/embed":
            raise AssertionError(path)
        assert payload is not None
        self.payloads.append(payload)
        # Mirror the production _request contract after an Ollama error envelope.
        raise AdapterError("EMBEDDING_UNAVAILABLE", "local embedding provider rejected the request")


def test_provider_rejection_does_not_retry_with_truncation() -> None:
    provider = make_provider(RejectingProvider)
    with pytest.raises(AdapterError) as error:
        provider.embed_documents(["alpha"])
    assert error.value.code == "EMBEDDING_UNAVAILABLE"
    assert len(provider.payloads) == 1
    assert provider.payloads[0]["truncate"] is False
