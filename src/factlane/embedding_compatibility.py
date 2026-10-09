from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .contract import AdapterError, canonical_json
from .embeddings import EmbeddingProfile


EMBEDDING_PROFILE_METADATA_REVISION = 2
ANCHOR_BUNDLE_ID = "embeddinggemma-300m-768-v0.1.3-space-v1"
ANCHOR_BUNDLE_SHA256 = "660ad7fbe2823d984f95c753ac3279d7c9149216ab5ee6d3f9b4988576d1ea48"
ANCHOR_BUNDLE_PATH = (
    Path(__file__).with_name("data")
    / "embedding_compatibility"
    / "embeddinggemma-300m-768-v013-v1.json"
)


_LEGACY_EMBEDDINGGEMMA_PROFILE = {
    "profile_id": "embeddinggemma-300m-768",
    "provider_kind": "OLLAMA_LOCAL",
    "base_model_identity": "embeddinggemma:300m",
    "model_digest": "85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1",
    "source_dimension": 768,
    "output_dimension": 768,
    "normalization_policy": "OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
    "distance_metric": "cosine",
    "projection_version": "ollama-dimensions-v1",
}


class CompatibilityProvider(Protocol):
    profile: EmbeddingProfile

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class LegacyEmbeddingSpace:
    store_key: str
    semantic_identity: dict[str, object]
    legacy_profile: dict[str, object]
    anchor_bundle_id: str
    anchor_bundle_sha256: str


@dataclass(frozen=True)
class CompatibilityProof:
    state: str
    basis: str
    reason: str | None
    anchor_bundle_id: str | None
    anchor_bundle_sha256: str | None
    minimum_document_cross_space_cosine: float | None
    minimum_query_match_cosine: float | None
    minimum_query_top1_margin: float | None

    @property
    def compatible(self) -> bool:
        return self.state == "COMPATIBLE"

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "basis": self.basis,
            "reason": self.reason,
            "anchor_bundle_id": self.anchor_bundle_id,
            "anchor_bundle_sha256": self.anchor_bundle_sha256,
            "minimum_document_cross_space_cosine": self.minimum_document_cross_space_cosine,
            "minimum_query_match_cosine": self.minimum_query_match_cosine,
            "minimum_query_top1_margin": self.minimum_query_top1_margin,
        }


def semantic_identity(profile: EmbeddingProfile) -> dict[str, object]:
    return profile.semantic_identity()


def runtime_provenance(provider_status: dict[str, object]) -> dict[str, object]:
    return {
        "provider_kind": provider_status.get("provider_kind"),
        "model": provider_status.get("model"),
        "model_digest": provider_status.get("digest"),
        "ollama_version": provider_status.get("ollama_version"),
        "semantic_family": provider_status.get("semantic_family"),
        "native_dimension": provider_status.get("native_dimension"),
        "output_dimension": provider_status.get("output_dimension"),
        "effective_context_window": provider_status.get("effective_context_window"),
    }


def runtime_fingerprint(provider_status: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json(runtime_provenance(provider_status)).encode("utf-8")).hexdigest()


def _embeddinggemma_semantic_identity() -> dict[str, object]:
    return {
        "base_model_identity": "embeddinggemma:300m",
        "semantic_family": "gemma3",
        "source_dimension": 768,
        "output_dimension": 768,
        "document_prefix": "title: none | text: ",
        "query_prefix": "task: search result | query: ",
        "normalization_policy": "OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
        "distance_metric": "cosine",
        "projection_version": "ollama-dimensions-v1",
        "compatibility_revision": 1,
    }


def map_legacy_profile(value: object) -> LegacyEmbeddingSpace | None:
    if value != _LEGACY_EMBEDDINGGEMMA_PROFILE:
        return None
    return LegacyEmbeddingSpace(
        store_key="embeddinggemma-300m-768",
        semantic_identity=_embeddinggemma_semantic_identity(),
        legacy_profile=dict(_LEGACY_EMBEDDINGGEMMA_PROFILE),
        anchor_bundle_id=ANCHOR_BUNDLE_ID,
        anchor_bundle_sha256=ANCHOR_BUNDLE_SHA256,
    )


def _load_anchor_bundle() -> dict[str, Any]:
    try:
        raw = ANCHOR_BUNDLE_PATH.read_bytes()
    except OSError as exc:
        raise AdapterError("PROFILE_MISMATCH", "qualified embedding compatibility anchors are unavailable") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if actual != ANCHOR_BUNDLE_SHA256:
        raise AdapterError("PROFILE_MISMATCH", "qualified embedding compatibility anchors failed integrity validation")
    try:
        bundle = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("PROFILE_MISMATCH", "qualified embedding compatibility anchors are invalid") from exc
    if not isinstance(bundle, dict) or bundle.get("bundle_id") != ANCHOR_BUNDLE_ID:
        raise AdapterError("PROFILE_MISMATCH", "qualified embedding compatibility anchor identity is invalid")
    return bundle


def qualified_anchor_available(identity: dict[str, object]) -> bool:
    return identity == _embeddinggemma_semantic_identity()


def _decode_vector(anchor: dict[str, Any], dimension: int) -> list[float]:
    encoded = anchor.get("legacy_document_vector_f32le_base64")
    expected_sha = anchor.get("legacy_document_vector_sha256")
    if not isinstance(encoded, str) or not isinstance(expected_sha, str):
        raise AdapterError("PROFILE_MISMATCH", "qualified embedding compatibility anchor vector is invalid")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise AdapterError("PROFILE_MISMATCH", "qualified embedding compatibility anchor vector is invalid") from exc
    if hashlib.sha256(raw).hexdigest() != expected_sha or len(raw) != dimension * 4:
        raise AdapterError("PROFILE_MISMATCH", "qualified embedding compatibility anchor vector failed integrity validation")
    return list(struct.unpack(f"<{dimension}f", raw))


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return -1.0
    return numerator / (left_norm * right_norm)


def evaluate_qualified_anchor(
    provider: CompatibilityProvider,
    identity: dict[str, object],
) -> CompatibilityProof:
    if not qualified_anchor_available(identity):
        return CompatibilityProof(
            state="UNPROVEN",
            basis="NO_QUALIFIED_ANCHOR",
            reason="no qualified cross-space anchor exists for this semantic identity",
            anchor_bundle_id=None,
            anchor_bundle_sha256=None,
            minimum_document_cross_space_cosine=None,
            minimum_query_match_cosine=None,
            minimum_query_top1_margin=None,
        )
    bundle = _load_anchor_bundle()
    reference = bundle.get("reference_runtime")
    anchors = bundle.get("anchors")
    acceptance = bundle.get("acceptance")
    if not isinstance(reference, dict) or not isinstance(anchors, list) or not anchors or not isinstance(acceptance, dict):
        raise AdapterError("PROFILE_MISMATCH", "qualified embedding compatibility anchor bundle is incomplete")
    bundle_identity = {
        "base_model_identity": reference.get("model"),
        "semantic_family": reference.get("semantic_family"),
        "source_dimension": reference.get("source_dimension"),
        "output_dimension": reference.get("output_dimension"),
        "document_prefix": reference.get("document_prefix"),
        "query_prefix": reference.get("query_prefix"),
        "normalization_policy": reference.get("normalization_policy"),
        "distance_metric": reference.get("distance_metric"),
        "projection_version": reference.get("projection_version"),
        "compatibility_revision": reference.get("factlane_embedding_compatibility_revision"),
    }
    if bundle_identity != identity:
        return CompatibilityProof(
            state="INCOMPATIBLE",
            basis="QUALIFIED_CROSS_SPACE_ANCHOR",
            reason="runtime semantic identity differs from the qualified anchor identity",
            anchor_bundle_id=ANCHOR_BUNDLE_ID,
            anchor_bundle_sha256=ANCHOR_BUNDLE_SHA256,
            minimum_document_cross_space_cosine=None,
            minimum_query_match_cosine=None,
            minimum_query_top1_margin=None,
        )
    dimension = int(identity["output_dimension"])
    old_documents: list[list[float]] = []
    documents: list[str] = []
    queries: list[str] = []
    for raw_anchor in anchors:
        if not isinstance(raw_anchor, dict):
            raise AdapterError("PROFILE_MISMATCH", "qualified embedding compatibility anchor is invalid")
        document = raw_anchor.get("document")
        query = raw_anchor.get("query")
        if not isinstance(document, str) or not document.strip() or not isinstance(query, str) or not query.strip():
            raise AdapterError("PROFILE_MISMATCH", "qualified embedding compatibility anchor text is invalid")
        documents.append(document)
        queries.append(query)
        old_documents.append(_decode_vector(raw_anchor, dimension))

    new_documents = provider.embed_documents(documents)
    if len(new_documents) != len(old_documents):
        return CompatibilityProof(
            state="INCOMPATIBLE",
            basis="QUALIFIED_CROSS_SPACE_ANCHOR",
            reason="runtime did not produce the required anchor document vectors",
            anchor_bundle_id=ANCHOR_BUNDLE_ID,
            anchor_bundle_sha256=ANCHOR_BUNDLE_SHA256,
            minimum_document_cross_space_cosine=None,
            minimum_query_match_cosine=None,
            minimum_query_top1_margin=None,
        )
    document_scores = [_cosine(old, new) for old, new in zip(old_documents, new_documents, strict=True)]
    query_matches: list[float] = []
    query_margins: list[float] = []
    all_top1 = True
    for expected_index, query in enumerate(queries):
        vector = provider.embed_query(query)
        scores = [_cosine(vector, old) for old in old_documents]
        ranking = sorted(range(len(scores)), key=lambda index: scores[index], reverse=True)
        all_top1 = all_top1 and ranking[0] == expected_index
        query_matches.append(scores[expected_index])
        query_margins.append(scores[ranking[0]] - scores[ranking[1]] if len(ranking) > 1 else scores[ranking[0]])

    minimum_document = min(document_scores)
    minimum_match = min(query_matches)
    minimum_margin = min(query_margins)
    required_document = float(acceptance.get("minimum_document_cross_space_cosine", 1.0))
    required_match = float(acceptance.get("minimum_query_match_cosine", 1.0))
    required_margin = float(acceptance.get("minimum_query_top1_margin", 1.0))
    require_top1 = acceptance.get("require_all_query_matches_top1") is True
    compatible = (
        minimum_document >= required_document
        and minimum_match >= required_match
        and minimum_margin >= required_margin
        and (all_top1 or not require_top1)
    )
    return CompatibilityProof(
        state="COMPATIBLE" if compatible else "INCOMPATIBLE",
        basis="QUALIFIED_CROSS_SPACE_ANCHOR",
        reason=None if compatible else "runtime failed qualified cross-space anchor acceptance",
        anchor_bundle_id=ANCHOR_BUNDLE_ID,
        anchor_bundle_sha256=ANCHOR_BUNDLE_SHA256,
        minimum_document_cross_space_cosine=minimum_document,
        minimum_query_match_cosine=minimum_match,
        minimum_query_top1_margin=minimum_margin,
    )


def build_embedding_profile_metadata(
    profile: EmbeddingProfile,
    *,
    store_key: str,
    proof: CompatibilityProof,
    provider_status: dict[str, object],
) -> dict[str, object]:
    return {
        "metadata_revision": EMBEDDING_PROFILE_METADATA_REVISION,
        "store_key": store_key,
        "semantic_identity": semantic_identity(profile),
        "compatibility_proof": proof.to_dict(),
        "runtime_provenance": runtime_provenance(provider_status),
        "runtime_fingerprint": runtime_fingerprint(provider_status),
    }


def parse_embedding_profile_metadata(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or value.get("metadata_revision") != EMBEDDING_PROFILE_METADATA_REVISION:
        return None
    if not isinstance(value.get("store_key"), str) or not value["store_key"]:
        return None
    if not isinstance(value.get("semantic_identity"), dict):
        return None
    if not isinstance(value.get("compatibility_proof"), dict):
        return None
    if not isinstance(value.get("runtime_provenance"), dict):
        return None
    if not isinstance(value.get("runtime_fingerprint"), str):
        return None
    return value
