from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

from .contract import (
    CURRENT_LIFECYCLE,
    MEMORY_TYPES,
    AdapterError,
    ScopeContext,
    canonical_json,
    contains_sensitive,
    digest,
    is_fresh,
    iso_now,
    native_hash,
    parse_iso,
    scope_digest,
    validate_fact,
    validate_freshness,
    validate_identifier,
    validate_provenance,
    validate_scope,
)
from .embeddings import EmbeddingProvider, OllamaLocalProvider
from .router import TruthRouter
from .storage import SQLiteVecEngine

MODEL_DIGESTS = {
    "nomic-embed-text:latest": "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
    "all-minilm:l6-v2": "1b226e2802dbb772b5fc32a58f103ca1804ef7501331012de126ab22f67475ef",
}
PROFILE_DEFINITIONS = {
    "nomic-768": {
        "model": "nomic-embed-text:latest",
        "output_dimension": 768,
        "source_dimension": 768,
        "document_prefix": "search_document: ",
        "query_prefix": "search_query: ",
    },
    "nomic-512": {
        "model": "nomic-embed-text:latest",
        "output_dimension": 512,
        "source_dimension": 768,
        "document_prefix": "search_document: ",
        "query_prefix": "search_query: ",
    },
    "nomic-256": {
        "model": "nomic-embed-text:latest",
        "output_dimension": 256,
        "source_dimension": 768,
        "document_prefix": "search_document: ",
        "query_prefix": "search_query: ",
    },
    "minilm-384": {
        "model": "all-minilm:l6-v2",
        "output_dimension": 384,
        "source_dimension": 384,
        "document_prefix": "",
        "query_prefix": "",
    },
}


class TokenCounter:
    """Optional local token measurement; no network/model download."""

    def __init__(self, tokenizer_path: str | None) -> None:
        self.path = tokenizer_path
        self._tokenizer: Any = None
        if tokenizer_path:
            try:
                from tokenizers import Tokenizer

                self._tokenizer = Tokenizer.from_file(tokenizer_path)
            except Exception as exc:
                raise AdapterError("SCHEMA_MISMATCH", "configured local tokenizer could not be loaded") from exc

    @property
    def available(self) -> bool:
        return self._tokenizer is not None

    def count(self, text: str) -> int | str:
        if self._tokenizer is None:
            return "UNMEASURED"
        return len(self._tokenizer.encode(text).ids)


@dataclass(frozen=True)
class AdapterLimits:
    top_k_default: int = 5
    top_k_hard_max: int = 8
    max_memories_default: int = 5
    max_memories_hard_max: int = 8
    max_bytes_default: int = 6000
    max_bytes_hard_max: int = 8000
    max_tokens_default: int = 1200
    max_tokens_hard_max: int = 1600


class MemoryAdapter:
    """Truth-router-backed, five-operation adapter over SQLite-vec."""

    TOOL_NAMES = ("memory_search", "memory_get", "memory_store", "memory_update", "memory_status")

    def __init__(
        self,
        engine: SQLiteVecEngine,
        provider: EmbeddingProvider,
        *,
        token_counter: TokenCounter | None = None,
        limits: AdapterLimits | None = None,
        runtime_agent_id: str = "factlane-local",
    ) -> None:
        self.engine = engine
        self.provider = provider
        self.token_counter = token_counter or TokenCounter(None)
        self.limits = limits or AdapterLimits()
        self.runtime_agent_id = runtime_agent_id
        self.router = TruthRouter()
        self._write_lock: asyncio.Lock | None = None

    def _get_write_lock(self) -> asyncio.Lock:
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    @classmethod
    async def create(
        cls,
        db_path: str,
        profile_name: str,
        *,
        ollama_url: str = "http://127.0.0.1:11434",
        tokenizer_path: str | None = None,
        runtime_agent_id: str = "factlane-local",
    ) -> MemoryAdapter:
        try:
            definition = PROFILE_DEFINITIONS[profile_name]
        except KeyError as exc:
            raise AdapterError("INVALID_ENVELOPE", "unknown embedding profile") from exc
        model = definition["model"]
        provider = OllamaLocalProvider(
            model=model,
            profile_id=profile_name,
            output_dimension=definition["output_dimension"],
            source_dimension=definition["source_dimension"],
            model_digest=MODEL_DIGESTS[model],
            document_prefix=definition["document_prefix"],
            query_prefix=definition["query_prefix"],
            base_url=ollama_url,
        )
        provider.provider_status()
        engine = SQLiteVecEngine(db_path, provider.profile)
        await engine.open()
        return cls(
            engine,
            provider,
            token_counter=TokenCounter(tokenizer_path),
            runtime_agent_id=runtime_agent_id,
        )

    @staticmethod
    def _request_id(value: str | None) -> str:
        if value:
            validate_identifier(value, "request_id", required=True)
            return value
        return str(uuid.uuid4())

    @staticmethod
    def _safe_scope(
        scope: str,
        project_id: str | None,
        worktree_id: str | None,
        workflow_id: str | None,
        agent_id: str | None,
    ) -> ScopeContext:
        return validate_scope(scope, project_id, worktree_id, workflow_id, agent_id)

    @staticmethod
    def _authority_for(scope: ScopeContext) -> str:
        return {
            "GLOBAL_USER": "OWNER_CURRENT",
            "PROJECT": "PROJECT_CURRENT",
            "WORKFLOW": "WORKFLOW_CURRENT",
            "TOOL_ENVIRONMENT": "TOOL_ENV_CURRENT",
        }[scope.scope]

    @staticmethod
    def _normalize_timestamp(value: str | None, *, required: bool = False) -> str | None:
        parsed = parse_iso(value, required=required)
        return parsed.isoformat().replace("+00:00", "Z") if parsed else None

    @staticmethod
    def _tags(tags: list[str] | None) -> list[str]:
        if tags is None:
            return []
        if not isinstance(tags, list) or len(tags) > 12:
            raise AdapterError("INVALID_ENVELOPE", "tags must contain at most 12 strings")
        result: list[str] = []
        for tag in tags:
            if not isinstance(tag, str) or not tag.strip() or len(tag.encode("utf-8")) > 48:
                raise AdapterError("INVALID_ENVELOPE", "tag is invalid or unbounded")
            value = " ".join(tag.strip().split())
            if contains_sensitive(value):
                raise AdapterError("RAW_OR_SENSITIVE_CONTENT", "tag contains sensitive material")
            if value not in result:
                result.append(value)
        return result

    @staticmethod
    def _subject(subject: str | None, tags: list[str], fact: str) -> str:
        if subject is not None:
            if not isinstance(subject, str) or not subject.strip() or len(subject.encode("utf-8")) > 96:
                raise AdapterError("INVALID_ENVELOPE", "subject is invalid")
            value = " ".join(subject.strip().split()).casefold()
        else:
            subject_tag = next((tag for tag in tags if tag.startswith("subject:")), None)
            value = subject_tag.split(":", 1)[1].strip().casefold() if subject_tag else fact.casefold()[:96]
        if not value or contains_sensitive(value):
            raise AdapterError("INVALID_ENVELOPE", "subject is invalid")
        return value

    @staticmethod
    def _public(record: dict[str, Any], *, relevance_score: float | None = None, retrieval_rank: int | None = None) -> dict[str, Any]:
        provenance = json.loads(record["source_provenance"])
        return {
            "memory_id": record["memory_id"],
            "record_id": record["record_id"],
            "revision": record["revision"],
            "parent_record_id": record["parent_record_id"],
            "scope": {
                "scope": record["scope"],
                "project_id": record["project_id"],
                "worktree_id": record["worktree_id"],
                "workflow_id": record["workflow_id"],
                "agent_id": record["agent_id"],
            },
            "memory_type": record["memory_type"],
            "fact": record["fact"],
            "source_provenance": provenance,
            "source_timestamp": record["source_timestamp"],
            "created_at": record["created_at"],
            "last_verified_at": record["last_verified_at"],
            "verified_by": record["verified_by"],
            "authority_role": record["authority_role"],
            "freshness_policy": json.loads(record["freshness_policy"]),
            "supersedes": json.loads(record["supersedes"]),
            "contradiction_state": record["contradiction_state"],
            "confidence": record["confidence"],
            "tags": json.loads(record["tags"]),
            "lifecycle_state": record["lifecycle_state"],
            "relevance_score": relevance_score,
            "retrieval_rank": retrieval_rank,
        }

    def _base_envelope(self, request_id: str, operation: str, scope: ScopeContext | None) -> dict[str, Any]:
        return {
            "status": "OK",
            "memory_needed": True,
            "scope": scope.to_dict() if scope else None,
            "results": [],
            "contradictions": [],
            "budget": {
                "requested_top_k": None,
                "returned": 0,
                "serialized_bytes": 0,
                "serialized_tokens": "UNMEASURED",
                "truncated": False,
            },
            "degradation": None,
            "audit": {
                "request_id": request_id,
                "operation": operation,
                "scope_digest": scope_digest(scope) if scope else None,
                "source_classes": [],
                "backend": "sqlite_vec",
                "external_llm_calls": 0,
                "local_embedding_calls": self.provider.document_calls + self.provider.query_calls,
                "raw_content_logged": False,
            },
        }

    @staticmethod
    def _payload_fingerprint(data: dict[str, Any]) -> str:
        return digest(data)

    async def store(
        self,
        *,
        fact: str,
        scope: str,
        memory_type: str,
        source_provenance: dict[str, Any],
        freshness_policy: dict[str, Any],
        idempotency_key: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        source_timestamp: str | None = None,
        last_verified_at: str | None = None,
        verified_by: str = "UNVERIFIED",
        authority_role: str | None = None,
        requested_lifecycle_state: str = "CANDIDATE",
        confidence: float = 0.5,
        tags: list[str] | None = None,
        subject: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id(request_id)
        scope_context = self._safe_scope(scope, project_id, worktree_id, workflow_id, agent_id)
        fact = validate_fact(fact)
        if memory_type not in MEMORY_TYPES:
            raise AdapterError("INVALID_ENUM", "memory_type is not supported")
        provenance = validate_provenance(source_provenance)
        freshness = validate_freshness(freshness_policy)
        validate_identifier(idempotency_key, "idempotency_key", required=True)
        source_timestamp = self._normalize_timestamp(source_timestamp)
        last_verified_at = self._normalize_timestamp(last_verified_at)
        if verified_by not in {"OWNER", "CURRENT_REPO_CHECK", "AUTOMATED_CHECK", "UNVERIFIED"}:
            raise AdapterError("INVALID_ENUM", "verified_by is invalid")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise AdapterError("INVALID_ENVELOPE", "confidence must be between 0 and 1")
        tags_value = self._tags(tags)
        subject_value = self._subject(subject, tags_value, fact)
        if requested_lifecycle_state not in {"CANDIDATE", CURRENT_LIFECYCLE}:
            raise AdapterError("INVALID_ENUM", "normal store accepts candidate or validated current only")
        expected_authority = self._authority_for(scope_context)
        if requested_lifecycle_state == CURRENT_LIFECYCLE:
            if not source_timestamp:
                raise AdapterError("PROVENANCE_REQUIRED", "validated current admission requires source_timestamp")
            if not last_verified_at or verified_by == "UNVERIFIED":
                raise AdapterError("UNVERIFIED_CURRENT", "validated current admission requires explicit verification")
            if authority_role not in {None, expected_authority}:
                raise AdapterError("INVALID_ENVELOPE", "authority role does not match exact scope")
            authority = expected_authority
            lifecycle = CURRENT_LIFECYCLE
        else:
            authority = "UNRESOLVED"
            lifecycle = "CANDIDATE"
            last_verified_at = None
            verified_by = "UNVERIFIED"
        fingerprint_input = {
            "fact": fact,
            "scope": scope_context.to_dict(),
            "memory_type": memory_type,
            "source_provenance": provenance,
            "source_timestamp": source_timestamp,
            "freshness_policy": freshness,
            "verified_by": verified_by,
            "authority_role": authority,
            "requested_lifecycle_state": lifecycle,
            "confidence": float(confidence),
            "tags": tags_value,
            "subject": subject_value,
        }
        payload_fingerprint = self._payload_fingerprint(fingerprint_input)
        async with self._get_write_lock():
            existing = await self.engine.find_idempotency(idempotency_key)
            if existing:
                if existing["payload_fingerprint"] != payload_fingerprint:
                    raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is bound to a different payload")
                envelope = self._base_envelope(request_id, "memory_store", scope_context)
                envelope["results"] = [self._public(existing)]
                envelope["idempotent_replay"] = True
                envelope["audit"]["local_embedding_calls"] = self.provider.document_calls + self.provider.query_calls
                return envelope
            stable_hash = native_hash(scope_context, fact)
            exact = await self.engine.find_exact(stable_hash)
            if exact:
                envelope = self._base_envelope(request_id, "memory_store", scope_context)
                envelope["status"] = "DUPLICATE"
                envelope["results"] = [self._public(exact)]
                envelope["audit"]["local_embedding_calls"] = self.provider.document_calls + self.provider.query_calls
                return envelope
            contradiction_key = digest({"scope": scope_context.to_dict(), "memory_type": memory_type, "subject": subject_value})
            conflicts = await self.engine.find_contradictions(contradiction_key, scope_context)
            contradiction_state = "UNRESOLVED" if any(
                existing_row["fact"].casefold() != fact.casefold() for existing_row in conflicts
            ) else "NONE"
            if contradiction_state == "UNRESOLVED":
                lifecycle = "CANDIDATE"
                authority = "UNRESOLVED"
                last_verified_at = None
                verified_by = "UNVERIFIED"
            embedding = self.provider.embed_documents([fact])[0]
            record_id = str(uuid.uuid4())
            memory_id = str(uuid.uuid4())
            created_at = iso_now()
            record = {
                "record_id": record_id,
                "memory_id": memory_id,
                "revision": 1,
                "parent_record_id": None,
                "scope": scope_context.scope,
                "project_id": scope_context.project_id,
                "worktree_id": scope_context.worktree_id,
                "workflow_id": scope_context.workflow_id,
                "agent_id": scope_context.agent_id,
                "memory_type": memory_type,
                "fact": fact,
                "source_provenance": provenance,
                "source_timestamp": source_timestamp,
                "created_at": created_at,
                "last_verified_at": last_verified_at,
                "verified_by": verified_by,
                "authority_role": authority,
                "freshness_policy": freshness,
                "supersedes": [],
                "contradiction_key": contradiction_key,
                "contradiction_state": contradiction_state,
                "confidence": float(confidence),
                "tags": tags_value,
                "lifecycle_state": lifecycle,
                "native_content_hash": stable_hash,
                "payload_fingerprint": payload_fingerprint,
                "idempotency_key": idempotency_key,
                "embedding_profile_id": self.provider.profile.profile_id,
                "embedding_model_digest": self.provider.profile.model_digest,
                "embedding_output_dimension": self.provider.profile.output_dimension,
            }
            await self.engine.write_record(record, embedding)
            rows = await self.engine.get_record(memory_id, scope_context, history=True)
            readback = next((row for row in rows if row["record_id"] == record_id), None)
            if not readback:
                raise AdapterError("WRITE_UNCONFIRMED", "stored record could not be read back")
        envelope = self._base_envelope(request_id, "memory_store", scope_context)
        envelope["status"] = "CONTRADICTION" if contradiction_state == "UNRESOLVED" else "OK"
        envelope["results"] = [self._public(readback)]
        envelope["contradictions"] = await self.engine.contradiction_summary(scope_context)
        envelope["audit"]["local_embedding_calls"] = self.provider.document_calls + self.provider.query_calls
        return envelope

    async def get(
        self,
        *,
        memory_id: str,
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        retrieval_mode: str = "CURRENT",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id(request_id)
        try:
            uuid.UUID(memory_id)
        except (ValueError, AttributeError) as exc:
            raise AdapterError("INVALID_ENVELOPE", "memory_id must be a UUID") from exc
        if retrieval_mode not in {"CURRENT", "REVIEW_HISTORY"}:
            raise AdapterError("INVALID_ENUM", "retrieval mode is invalid")
        scope_context = self._safe_scope(scope, project_id, worktree_id, workflow_id, agent_id)
        rows = await self.engine.get_record(memory_id, scope_context, history=retrieval_mode == "REVIEW_HISTORY")
        if not rows:
            if await self.engine.memory_exists_outside_scope(memory_id, scope_context):
                raise AdapterError("CROSS_SCOPE_DENIED", "memory_id exists outside the requested scope")
            raise AdapterError("NOT_FOUND", "memory_id was not found in the requested scope")
        envelope = self._base_envelope(request_id, "memory_get", scope_context)
        envelope["results"] = [self._public(row) for row in rows[: self.limits.max_memories_hard_max]]
        envelope["budget"]["returned"] = len(envelope["results"])
        return self._fit_budget(envelope)

    @staticmethod
    def _current_record(record: dict[str, Any]) -> bool:
        return record["lifecycle_state"] == CURRENT_LIFECYCLE and record["contradiction_state"] in {"NONE", "RESOLVED"}

    def _fresh_current(self, record: dict[str, Any]) -> bool:
        if not self._current_record(record) or record["verified_by"] == "UNVERIFIED" or not record["last_verified_at"]:
            return False
        return is_fresh(
            json.loads(record["freshness_policy"]),
            record["last_verified_at"],
            json.loads(record["source_provenance"]),
        )

    def _fit_budget(self, envelope: dict[str, Any]) -> dict[str, Any]:
        def encoded() -> bytes:
            return canonical_json(envelope["results"]).encode("utf-8")

        max_bytes = envelope["budget"].get("max_bytes") or self.limits.max_bytes_default
        max_tokens = envelope["budget"].get("max_tokens") or self.limits.max_tokens_default
        max_bytes = min(max_bytes, self.limits.max_bytes_hard_max)
        max_tokens = min(max_tokens, self.limits.max_tokens_hard_max)
        while envelope["results"]:
            raw = encoded()
            token_count = self.token_counter.count(raw.decode("utf-8"))
            if len(raw) <= max_bytes and (token_count == "UNMEASURED" or token_count <= max_tokens):
                envelope["budget"]["serialized_bytes"] = len(raw)
                envelope["budget"]["serialized_tokens"] = token_count
                envelope["budget"]["returned"] = len(envelope["results"])
                return envelope
            envelope["results"].pop()
            envelope["budget"]["truncated"] = True
        raw = encoded()
        envelope["budget"]["serialized_bytes"] = len(raw)
        envelope["budget"]["serialized_tokens"] = self.token_counter.count(raw.decode("utf-8"))
        envelope["budget"]["returned"] = 0
        if envelope["budget"]["truncated"]:
            envelope["status"] = "DEGRADED"
            envelope["degradation"] = "BUDGET_EXCEEDED"
        return envelope

    async def search(
        self,
        *,
        query: str,
        intent_class: str,
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        retrieval_mode: str = "CURRENT",
        retrieval_mode_kind: str = "SEMANTIC",
        top_k: int = 5,
        max_memories: int = 5,
        max_bytes: int = 6000,
        max_tokens: int = 1200,
        include_graph_links: bool = False,
        direct_truth_available: bool = False,
        user_supplied: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id(request_id)
        scope_context = self._safe_scope(scope, project_id, worktree_id, workflow_id, agent_id)
        decision = self.router.decide(
            intent_class=intent_class,
            operation="memory_search",
            scope=scope_context,
            direct_truth_available=direct_truth_available,
            user_supplied=user_supplied,
            retrieval_mode=retrieval_mode,
        )
        if decision.status == "NO_MEMORY_NEEDED":
            return {
                "status": "NO_MEMORY_NEEDED",
                "memory_needed": False,
                "scope": scope_context.to_dict(),
                "results": [],
                "contradictions": [],
                "budget": {"requested_top_k": top_k, "returned": 0, "serialized_bytes": 0, "serialized_tokens": 0, "truncated": False},
                "degradation": None,
                "audit": {"request_id": request_id, "operation": "memory_search", "scope_digest": scope_digest(scope_context), "raw_content_logged": False},
            }
        if not isinstance(query, str) or not query.strip() or len(query.encode("utf-8")) > 512:
            raise AdapterError("INVALID_ENVELOPE", "query must be a bounded non-empty string")
        for name, value in (("top_k", top_k), ("max_memories", max_memories), ("max_bytes", max_bytes), ("max_tokens", max_tokens)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise AdapterError("INVALID_ENVELOPE", f"{name} must be a positive integer")
        if retrieval_mode_kind not in {"EXACT", "KEYWORD", "SEMANTIC", "HYBRID"}:
            raise AdapterError("INVALID_ENUM", "retrieval mode kind is invalid")
        if include_graph_links:
            raise AdapterError("ADMIN_OPERATION_DENIED", "graph expansion is not available in the normal agent path")
        requested_top_k = top_k
        top_k = min(max(1, int(top_k)), self.limits.top_k_hard_max)
        max_memories = min(max(1, int(max_memories)), self.limits.max_memories_hard_max)
        envelope = self._base_envelope(request_id, "memory_search", scope_context)
        envelope["budget"].update({"requested_top_k": requested_top_k, "max_bytes": max_bytes, "max_tokens": max_tokens})
        history = retrieval_mode == "REVIEW_HISTORY"
        if retrieval_mode_kind == "EXACT":
            exact_rows = await self.engine.keyword_candidates(query.strip(), scope_context, limit=top_k * 4, history=history, exact=True)
            scored = [(row, 1.0) for row in exact_rows]
        else:
            vector_scored: list[tuple[dict[str, Any], float]] = []
            keyword_rows: list[dict[str, Any]] = []
            if retrieval_mode_kind in {"SEMANTIC", "HYBRID"}:
                vector = self.provider.embed_query(query.strip())
                vector_scored = await self.engine.vector_candidates(vector, scope_context, limit=max(top_k * 4, 16), history=history)
            if retrieval_mode_kind in {"KEYWORD", "HYBRID"}:
                keyword_rows = await self.engine.keyword_candidates(query.strip(), scope_context, limit=max(top_k * 4, 16), history=history)
            scores: dict[str, tuple[dict[str, Any], float]] = {}
            for row, distance in vector_scored:
                scores[row["record_id"]] = (row, max(0.0, 1.0 - distance / 2.0))
            for rank, row in enumerate(keyword_rows, start=1):
                keyword_score = 1.0 / rank
                if row["record_id"] in scores:
                    old, score = scores[row["record_id"]]
                    scores[row["record_id"]] = (old, (0.6 * score + 0.4 * keyword_score) if retrieval_mode_kind == "HYBRID" else keyword_score)
                else:
                    scores[row["record_id"]] = (row, keyword_score)
            scored = sorted(scores.values(), key=lambda item: (-item[1], item[0]["record_id"]))
        stale_count = 0
        safe: list[tuple[dict[str, Any], float]] = []
        for row, score in scored:
            if not history:
                if row["lifecycle_state"] == CURRENT_LIFECYCLE and not self._fresh_current(row):
                    stale_count += 1
                    continue
                if not self._current_record(row):
                    continue
            safe.append((row, score))
        safe = safe[:max_memories]
        envelope["results"] = [self._public(row, relevance_score=round(score, 6), retrieval_rank=index) for index, (row, score) in enumerate(safe, start=1)]
        envelope["contradictions"] = await self.engine.contradiction_summary(scope_context)
        if not envelope["results"] and stale_count:
            envelope["status"] = "DEGRADED"
            envelope["degradation"] = "STALE_ONLY"
        elif envelope["contradictions"] and not envelope["results"]:
            envelope["status"] = "CONTRADICTION"
            envelope["degradation"] = "CONTRADICTION_UNRESOLVED"
        envelope["audit"]["local_embedding_calls"] = self.provider.document_calls + self.provider.query_calls
        return self._fit_budget(envelope)

    async def update(
        self,
        *,
        memory_id: str,
        scope: str,
        expected_revision: int,
        mode: str,
        idempotency_key: str,
        replacement: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id(request_id)
        scope_context = self._safe_scope(scope, project_id, worktree_id, workflow_id, agent_id)
        if mode not in {"REVERIFY", "REPLACE"}:
            raise AdapterError("INVALID_ENUM", "update mode is invalid")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise AdapterError("INVALID_ENVELOPE", "expected_revision is invalid")
        validate_identifier(idempotency_key, "idempotency_key", required=True)
        try:
            uuid.UUID(memory_id)
        except (ValueError, AttributeError) as exc:
            raise AdapterError("INVALID_ENVELOPE", "memory_id must be a UUID") from exc
        update_request_fingerprint = self._payload_fingerprint({
            "memory_id": memory_id,
            "scope": scope_context.to_dict(),
            "expected_revision": expected_revision,
            "mode": mode,
            "replacement": replacement or {},
            "verification": verification or {},
        })
        replay = await self.engine.find_idempotency(idempotency_key)
        if replay:
            if replay["payload_fingerprint"] != update_request_fingerprint:
                raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is bound to a different update")
            envelope = self._base_envelope(request_id, "memory_update", scope_context)
            envelope["results"] = [self._public(replay)]
            envelope["idempotent_replay"] = True
            return envelope
        current_rows = await self.engine.get_record(memory_id, scope_context, history=False)
        if not current_rows:
            if await self.engine.memory_exists_outside_scope(memory_id, scope_context):
                raise AdapterError("CROSS_SCOPE_DENIED", "memory_id exists outside the requested scope")
            raise AdapterError("NOT_FOUND", "current memory_id was not found")
        old = current_rows[0]
        if old["revision"] != expected_revision:
            raise AdapterError("VERSION_CONFLICT", "expected_revision does not match current revision")
        old_provenance = json.loads(old["source_provenance"])
        old_freshness = json.loads(old["freshness_policy"])
        if mode == "REVERIFY":
            data = verification or {}
            fact = validate_fact(old["fact"])
            provenance = validate_provenance(data.get("source_provenance", old_provenance))
            freshness = validate_freshness(data.get("freshness_policy", old_freshness))
            source_timestamp = self._normalize_timestamp(data.get("source_timestamp", old["source_timestamp"]))
            verified_by = data.get("verified_by", "AUTOMATED_CHECK")
            authority = self._authority_for(scope_context)
            new_memory_id = memory_id
            supersedes: list[str] = []
            contradiction_key = old["contradiction_key"]
        else:
            data = replacement or {}
            fact = validate_fact(data.get("fact"))
            provenance = validate_provenance(data.get("source_provenance"))
            freshness = validate_freshness(data.get("freshness_policy"))
            source_timestamp = self._normalize_timestamp(data.get("source_timestamp"))
            verified_by = data.get("verified_by", "UNVERIFIED")
            new_memory_id = str(uuid.uuid4())
            supersedes = [memory_id]
            subject_value = self._subject(data.get("subject"), self._tags(data.get("tags", json.loads(old["tags"]))), fact)
            contradiction_key = digest({"scope": scope_context.to_dict(), "memory_type": data.get("memory_type", old["memory_type"]), "subject": subject_value})
            authority = self._authority_for(scope_context)
        if not source_timestamp:
            raise AdapterError("PROVENANCE_REQUIRED", "updated current record requires source_timestamp")
        if verified_by not in {"OWNER", "CURRENT_REPO_CHECK", "AUTOMATED_CHECK"}:
            raise AdapterError("UNVERIFIED_CURRENT", "update requires explicit current verification")
        last_verified_at = self._normalize_timestamp(data.get("last_verified_at", iso_now()), required=True)
        memory_type = data.get("memory_type", old["memory_type"])
        if memory_type not in MEMORY_TYPES:
            raise AdapterError("INVALID_ENUM", "memory_type is not supported")
        tags_value = self._tags(data.get("tags", json.loads(old["tags"])))
        confidence = float(data.get("confidence", old["confidence"]))
        if not 0 <= confidence <= 1:
            raise AdapterError("INVALID_ENVELOPE", "confidence must be between 0 and 1")
        fingerprint = update_request_fingerprint
        async with self._get_write_lock():
            existing = await self.engine.find_idempotency(idempotency_key)
            if existing:
                if existing["payload_fingerprint"] != fingerprint:
                    raise AdapterError("IDEMPOTENCY_CONFLICT", "idempotency key is bound to a different update")
                envelope = self._base_envelope(request_id, "memory_update", scope_context)
                envelope["results"] = [self._public(existing)]
                envelope["idempotent_replay"] = True
                return envelope
            record_id = str(uuid.uuid4())
            record = {
                "record_id": record_id,
                "memory_id": new_memory_id,
                "revision": old["revision"] + 1 if mode == "REVERIFY" else 1,
                "parent_record_id": old["record_id"],
                "scope": old["scope"],
                "project_id": old["project_id"],
                "worktree_id": old["worktree_id"],
                "workflow_id": old["workflow_id"],
                "agent_id": old["agent_id"],
                "memory_type": memory_type,
                "fact": fact,
                "source_provenance": provenance,
                "source_timestamp": source_timestamp,
                "created_at": iso_now(),
                "last_verified_at": last_verified_at,
                "verified_by": verified_by,
                "authority_role": authority,
                "freshness_policy": freshness,
                "supersedes": supersedes,
                "contradiction_key": contradiction_key,
                "contradiction_state": "RESOLVED" if mode == "REPLACE" else "NONE",
                "confidence": confidence,
                "tags": tags_value,
                "lifecycle_state": CURRENT_LIFECYCLE,
                "native_content_hash": native_hash(scope_context, fact, revision_key=record_id),
                "payload_fingerprint": fingerprint,
                "idempotency_key": idempotency_key,
                "embedding_profile_id": self.provider.profile.profile_id,
                "embedding_model_digest": self.provider.profile.model_digest,
                "embedding_output_dimension": self.provider.profile.output_dimension,
            }
            embedding = self.provider.embed_documents([fact])[0]
            await self.engine.write_record(record, embedding, supersede_record_id=old["record_id"])
            readback_rows = await self.engine.get_record(new_memory_id, scope_context, history=True)
            readback = next((row for row in readback_rows if row["record_id"] == record_id), None)
            if not readback or not self._fresh_current(readback):
                raise AdapterError("WRITE_UNCONFIRMED", "updated record failed current read-back")
        envelope = self._base_envelope(request_id, "memory_update", scope_context)
        envelope["results"] = [self._public(readback)]
        envelope["audit"]["local_embedding_calls"] = self.provider.document_calls + self.provider.query_calls
        return self._fit_budget(envelope)

    async def status(
        self,
        *,
        scope: str | None = None,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._request_id(request_id)
        if scope is None:
            raise AdapterError("UNKNOWN_SCOPE", "memory_status requires an exact scope")
        scope_context = None
        if scope is not None:
            scope_context = self._safe_scope(scope, project_id, worktree_id, workflow_id, agent_id)
        backend = await self.engine.status(scope_context)
        provider_status = self.provider.provider_status()
        envelope = self._base_envelope(request_id, "memory_status", scope_context)
        envelope["memory_needed"] = False
        envelope["status"] = "OK"
        envelope["backend_status"] = backend
        envelope["provider_status"] = provider_status
        envelope["token_measurement"] = {
            "available": self.token_counter.available,
            "profile": "PORTABLE_MEASUREMENT_PROFILE" if self.token_counter.available else "UNMEASURED",
            "codex_exact_equivalence": "UNVERIFIED",
        }
        return envelope

    async def close(self) -> None:
        await self.engine.close()

    @classmethod
    def tool_names(cls) -> list[str]:
        return list(cls.TOOL_NAMES)
