from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import tarfile
from zipfile import ZipFile

import pytest

import factlane.embedding_compatibility as compatibility
from factlane.contract import AdapterError
from factlane.embedding_compatibility import (
    ANCHOR_BUNDLE_ID,
    ANCHOR_BUNDLE_PATH,
    ANCHOR_BUNDLE_SHA256,
    evaluate_qualified_anchor,
    map_legacy_profile,
    semantic_identity,
)
from factlane.embeddings import EmbeddingProfile


LEGACY_DIGEST = "85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1"


def _profile(*, profile_id: str = "embeddinggemma-300m-768", digest: str = LEGACY_DIGEST) -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id=profile_id,
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


def _bundle() -> dict[str, object]:
    return json.loads(ANCHOR_BUNDLE_PATH.read_text(encoding="utf-8"))


def _legacy_vectors(bundle: dict[str, object]) -> tuple[list[dict[str, object]], list[list[float]]]:
    anchors = bundle["anchors"]
    assert isinstance(anchors, list)
    vectors: list[list[float]] = []
    typed_anchors: list[dict[str, object]] = []
    for anchor in anchors:
        assert isinstance(anchor, dict)
        typed_anchors.append(anchor)
        encoded = anchor["legacy_document_vector_f32le_base64"]
        assert isinstance(encoded, str)
        raw = base64.b64decode(encoded)
        vectors.append(list(struct.unpack("<768f", raw)))
    return typed_anchors, vectors


class FrozenCompatibleProvider:
    def __init__(self, *, rotate: bool = False) -> None:
        self.profile = _profile(digest="b" * 64)
        bundle = _bundle()
        self.anchors, self.vectors = _legacy_vectors(bundle)
        self.rotate = rotate
        self.document_calls = 0
        self.query_calls = 0

    def _candidate(self, vector: list[float]) -> list[float]:
        if not self.rotate:
            return vector[:]
        return vector[1:] + vector[:1]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        mapping = {anchor["document"]: vector for anchor, vector in zip(self.anchors, self.vectors, strict=True)}
        return [self._candidate(mapping[text]) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        mapping = {anchor["query"]: vector for anchor, vector in zip(self.anchors, self.vectors, strict=True)}
        return self._candidate(mapping[text])


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    )


def test_semantic_identity_excludes_provider_provenance_and_profile_alias() -> None:
    first = _profile(profile_id="alias-a", digest="a" * 64)
    second = _profile(profile_id="alias-b", digest="b" * 64)
    assert semantic_identity(first) == semantic_identity(second)
    assert "profile_id" not in semantic_identity(first)
    assert "provider_kind" not in semantic_identity(first)
    assert "model_digest" not in semantic_identity(first)
    assert "minimum_context_window" not in semantic_identity(first)


def test_anchor_bundle_is_integrity_bound_and_complete() -> None:
    raw = ANCHOR_BUNDLE_PATH.read_bytes()
    bundle = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == ANCHOR_BUNDLE_SHA256
    assert bundle["bundle_id"] == ANCHOR_BUNDLE_ID
    assert bundle["reference_runtime"]["ollama_version"] == "0.22.1"
    assert bundle["qualification_basis"]["reference_ollama_version_basis"] == (
        "RELEASE_ENVIRONMENT_PROVENANCE"
    )
    assert bundle["qualification_basis"]["reference_environment_provenance_sha256"] == (
        "b4866c02e085380c665fc0fdefbc9e87d27c5d93bdc87d8c271d21997311f343"
    )
    assert {item["sha256"] for item in bundle["qualification_basis"]["source_artifacts"]} == {
        "548333239b5c20edb4fca289efa2fc82cdbd54a86232e471643dc7035a3d09d5",
    }
    assert len(bundle["anchors"]) == 4
    assert bundle["acceptance"] == {
        "minimum_document_cross_space_cosine": 0.999,
        "minimum_query_match_cosine": 0.60,
        "minimum_query_top1_margin": 0.05,
        "require_all_query_matches_top1": True,
    }


def test_exact_known_v013_profile_maps_to_qualified_legacy_space() -> None:
    legacy = {
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
    mapped = map_legacy_profile(legacy)
    assert mapped is not None
    assert mapped.store_key == "embeddinggemma-300m-768"
    assert mapped.semantic_identity == semantic_identity(_profile())
    drifted = dict(legacy, model_digest="f" * 64)
    assert map_legacy_profile(drifted) is None


def test_qualified_anchor_accepts_cross_space_exact_alignment() -> None:
    provider = FrozenCompatibleProvider()
    proof = evaluate_qualified_anchor(provider, semantic_identity(provider.profile))
    assert proof.compatible is True
    assert proof.state == "COMPATIBLE"
    assert proof.minimum_document_cross_space_cosine is not None
    assert proof.minimum_document_cross_space_cosine >= 0.999
    assert proof.minimum_query_match_cosine == pytest.approx(1.0)
    assert proof.minimum_query_top1_margin is not None
    assert proof.minimum_query_top1_margin >= 0.05
    assert provider.document_calls == 4
    assert provider.query_calls == 4


def test_common_rotation_is_rejected_by_cross_space_alignment_even_when_new_pairwise_geometry_is_preserved() -> None:
    provider = FrozenCompatibleProvider(rotate=True)
    old = provider.vectors
    rotated = [provider._candidate(vector) for vector in old]
    assert _cosine(old[0], old[1]) == pytest.approx(_cosine(rotated[0], rotated[1]), abs=1e-7)
    proof = evaluate_qualified_anchor(provider, semantic_identity(provider.profile))
    assert proof.compatible is False
    assert proof.state == "INCOMPATIBLE"
    assert proof.minimum_document_cross_space_cosine is not None
    assert proof.minimum_document_cross_space_cosine < 0.999


def test_anchor_bundle_tamper_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    tampered = tmp_path / "anchors.json"
    tampered.write_bytes(ANCHOR_BUNDLE_PATH.read_bytes() + b"\n")
    monkeypatch.setattr(compatibility, "ANCHOR_BUNDLE_PATH", tampered)
    provider = FrozenCompatibleProvider()
    with pytest.raises(AdapterError) as error:
        evaluate_qualified_anchor(provider, semantic_identity(provider.profile))
    assert error.value.code == "PROFILE_MISMATCH"


def test_anchor_bundle_ships_byte_identical_in_wheel_and_sdist(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--out-dir", str(dist)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expected = ANCHOR_BUNDLE_PATH.read_bytes()
    relative = "factlane/data/embedding_compatibility/embeddinggemma-300m-768-v013-v1.json"

    wheel = next(dist.glob("*.whl"))
    with ZipFile(wheel) as archive:
        matches = [name for name in archive.namelist() if name.endswith(relative)]
        assert len(matches) == 1
        assert archive.read(matches[0]) == expected

    sdist = next(dist.glob("*.tar.gz"))
    with tarfile.open(sdist, "r:gz") as archive:
        suffix = f"/src/{relative}"
        matches = [name for name in archive.getnames() if name.endswith(suffix)]
        assert len(matches) == 1
        member = archive.extractfile(matches[0])
        assert member is not None
        assert member.read() == expected
