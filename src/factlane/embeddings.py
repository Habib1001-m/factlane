from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Protocol

from .contract import AdapterError, finite_vector


EMBEDDING_COMPATIBILITY_REVISION = 1
MIN_OLLAMA_VERSION = (0, 22, 1)


def _parse_semver(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", value.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _context_length_from_model_info(model_info: dict[str, object]) -> int | None:
    preferred = (
        "nomic-bert.context_length",
        "bert.context_length",
        "nomic-bert.max_position_embeddings",
        "bert.max_position_embeddings",
    )
    for key in preferred:
        value = model_info.get(key)
        if isinstance(value, int) and value > 0:
            return value
    for key, value in model_info.items():
        if (
            isinstance(key, str)
            and key.endswith((".context_length", ".max_position_embeddings"))
            and isinstance(value, int)
            and value > 0
        ):
            return value
    return None


def _embedding_length_from_model_info(model_info: dict[str, object]) -> int | None:
    legacy_keys = {"nomic-bert.embedding_length", "bert.embedding_length"}
    values = [
        value
        for key, value in model_info.items()
        if isinstance(key, str)
        and (key in legacy_keys or key.endswith(".embedding_length"))
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    ]
    if not values or len(set(values)) != 1:
        return None
    return values[0]


@dataclass(frozen=True)
class EmbeddingProfile:
    profile_id: str
    provider_kind: str
    base_model_identity: str
    model_digest: str
    source_dimension: int
    output_dimension: int
    normalization_policy: str
    distance_metric: str
    projection_version: str
    document_prefix: str
    query_prefix: str
    semantic_family: str = "UNSPECIFIED"
    compatibility_revision: int = EMBEDDING_COMPATIBILITY_REVISION
    minimum_context_window: int = 1

    def semantic_identity(self) -> dict[str, object]:
        return {
            "base_model_identity": self.base_model_identity,
            "semantic_family": self.semantic_family,
            "source_dimension": self.source_dimension,
            "output_dimension": self.output_dimension,
            "document_prefix": self.document_prefix,
            "query_prefix": self.query_prefix,
            "normalization_policy": self.normalization_policy,
            "distance_metric": self.distance_metric,
            "projection_version": self.projection_version,
            "compatibility_revision": self.compatibility_revision,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "provider_kind": self.provider_kind,
            "base_model_identity": self.base_model_identity,
            "model_digest": self.model_digest,
            "source_dimension": self.source_dimension,
            "output_dimension": self.output_dimension,
            "normalization_policy": self.normalization_policy,
            "distance_metric": self.distance_metric,
            "projection_version": self.projection_version,
            "document_prefix": self.document_prefix,
            "query_prefix": self.query_prefix,
            "semantic_family": self.semantic_family,
            "compatibility_revision": self.compatibility_revision,
            "minimum_context_window": self.minimum_context_window,
        }


class EmbeddingProvider(Protocol):
    profile: EmbeddingProfile
    document_calls: int
    query_calls: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def provider_status(self) -> dict[str, object]: ...


class OllamaLocalProvider:
    """Provider-neutral local Ollama boundary; no remote fallback exists."""

    def __init__(
        self,
        *,
        model: str,
        profile_id: str,
        output_dimension: int,
        source_dimension: int,
        model_digest: str,
        document_prefix: str = "",
        query_prefix: str = "",
        semantic_family: str = "UNSPECIFIED",
        minimum_context_window: int = 1,
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 120.0,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
            raise AdapterError("INVALID_ENVELOPE", "embedding provider must use local HTTP only")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise AdapterError("INVALID_ENVELOPE", "embedding provider URL is invalid")
        if output_dimension <= 0 or source_dimension < output_dimension:
            raise AdapterError("INVALID_ENVELOPE", "embedding dimensions are invalid")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.document_prefix = document_prefix
        self.query_prefix = query_prefix
        self.document_calls = 0
        self.query_calls = 0
        self.profile = EmbeddingProfile(
            profile_id=profile_id,
            provider_kind="OLLAMA_LOCAL",
            base_model_identity=model,
            model_digest=model_digest,
            source_dimension=source_dimension,
            output_dimension=output_dimension,
            normalization_policy="OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
            distance_metric="cosine",
            projection_version="ollama-dimensions-v1",
            document_prefix=document_prefix,
            query_prefix=query_prefix,
            semantic_family=semantic_family,
            minimum_context_window=minimum_context_window,
        )

    def _request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        url = self.base_url + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AdapterError("EMBEDDING_UNAVAILABLE", "local embedding provider is unavailable") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError("SCHEMA_MISMATCH", "local embedding provider returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise AdapterError("SCHEMA_MISMATCH", "local embedding provider returned an invalid envelope")
        if "error" in value:
            raise AdapterError("EMBEDDING_UNAVAILABLE", "local embedding provider rejected the request")
        return value

    def provider_status(self) -> dict[str, object]:
        version_payload = self._request("/api/version")
        version = version_payload.get("version")
        parsed_version = _parse_semver(version)
        if parsed_version is None:
            raise AdapterError("SCHEMA_MISMATCH", "local embedding provider version is invalid")
        if parsed_version < MIN_OLLAMA_VERSION:
            required = ".".join(str(part) for part in MIN_OLLAMA_VERSION)
            raise AdapterError(
                "EMBEDDING_UNAVAILABLE",
                f"local embedding provider requires Ollama >= {required}",
            )
        tags = self._request("/api/tags")
        models = tags.get("models")
        if not isinstance(models, list):
            raise AdapterError("SCHEMA_MISMATCH", "local model list has an invalid shape")
        model_row = next((row for row in models if isinstance(row, dict) and row.get("name") == self.model), None)
        if not isinstance(model_row, dict):
            raise AdapterError("EMBEDDING_UNAVAILABLE", "configured local embedding model is not present")
        digest = model_row.get("digest")
        if not isinstance(digest, str) or len(digest) != 64:
            raise AdapterError("SCHEMA_MISMATCH", "local model digest is unavailable")
        if any(character not in "0123456789abcdef" for character in digest):
            raise AdapterError("SCHEMA_MISMATCH", "local model digest is invalid")
        details = model_row.get("details")
        family = details.get("family") if isinstance(details, dict) else None
        if (
            self.profile.semantic_family != "UNSPECIFIED"
            and family != self.profile.semantic_family
        ):
            raise AdapterError("SCHEMA_MISMATCH", "local model semantic family differs from the profile")
        show = self._request("/api/show", {"name": self.model})
        model_info = show.get("model_info")
        if not isinstance(model_info, dict):
            raise AdapterError("SCHEMA_MISMATCH", "local model metadata has an invalid shape")
        native = _embedding_length_from_model_info(model_info)
        if native is None or native != self.profile.source_dimension:
            raise AdapterError("SCHEMA_MISMATCH", "local model native dimension differs from the profile")
        capabilities = show.get("capabilities")
        if not isinstance(capabilities, list) or "embedding" not in capabilities:
            raise AdapterError("EMBEDDING_UNAVAILABLE", "local model does not advertise embedding capability")
        context_window = _context_length_from_model_info(model_info)
        if context_window is None or context_window < self.profile.minimum_context_window:
            raise AdapterError("SCHEMA_MISMATCH", "local model context window differs from the qualified profile")
        # Digest and provider version are observed provenance, not semantic identity.
        self.profile = replace(self.profile, model_digest=digest)
        size = model_row.get("size")
        return {
            "local_only": True,
            "provider_kind": self.profile.provider_kind,
            "model": self.model,
            "digest": digest,
            "ollama_version": str(version),
            "semantic_family": family,
            "size_bytes": size if isinstance(size, int) else None,
            "native_dimension": native,
            "output_dimension": self.profile.output_dimension,
            "capabilities": [str(item) for item in capabilities],
            "document_prefix": self.document_prefix,
            "query_prefix": self.query_prefix,
            "distance_metric": "cosine",
            "effective_context_window": context_window,
            "truncate_policy": "FAIL_CLOSED_PROVIDER_REJECTION",
        }

    def _embed(self, texts: list[str], prefix: str) -> list[list[float]]:
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise AdapterError("INVALID_ENVELOPE", "embedding input contains an empty item")
        payload = {
            "model": self.model,
            "input": [prefix + text for text in texts],
            "truncate": False,
            "dimensions": self.profile.output_dimension,
        }
        response = self._request("/api/embed", payload)
        embeddings = response.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise AdapterError("SCHEMA_MISMATCH", "local embedding count does not match input count")
        result: list[list[float]] = []
        for vector in embeddings:
            if not isinstance(vector, list) or len(vector) != self.profile.output_dimension:
                raise AdapterError("SCHEMA_MISMATCH", "local embedding dimension does not match profile")
            values = [float(value) for value in vector]
            finite_vector(values)
            norm = math.sqrt(sum(value * value for value in values))
            if abs(norm - 1.0) > 1e-3:
                raise AdapterError("SCHEMA_MISMATCH", "local embedding is not normalized")
            result.append(values)
        return result

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        result = self._embed(texts, self.document_prefix)
        self.document_calls += len(result)
        return result

    def embed_query(self, text: str) -> list[float]:
        result = self._embed([text], self.query_prefix)
        self.query_calls += 1
        return result[0]
