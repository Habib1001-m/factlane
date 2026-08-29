from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

SCOPES = {"GLOBAL_USER", "PROJECT", "WORKFLOW", "TOOL_ENVIRONMENT"}
MEMORY_TYPES = {
    "USER_FACT",
    "PREFERENCE",
    "WORKFLOW_RULE",
    "PROJECT_LEARNED_FACT",
    "DECISION_RATIONALE",
    "TOOL_ENVIRONMENT_FACT",
}
VERIFIED_BY = {"OWNER", "CURRENT_REPO_CHECK", "AUTOMATED_CHECK", "UNVERIFIED"}
AUTHORITY_ROLES = {
    "OWNER_CURRENT",
    "PROJECT_CURRENT",
    "WORKFLOW_CURRENT",
    "TOOL_ENV_CURRENT",
    "SUPPORTING",
    "HISTORICAL",
    "UNRESOLVED",
}
LIFECYCLE_STATES = {
    "CANDIDATE",
    "VALIDATED_CURRENT",
    "SUPERSEDED",
    "STALE",
    "QUARANTINED",
    "HISTORICAL",
}
CONTRADICTION_STATES = {"NONE", "UNRESOLVED", "RESOLVED", "QUARANTINED"}
FRESHNESS_KINDS = {"never", "ttl", "on_change", "manual"}
CURRENT_LIFECYCLE = "VALIDATED_CURRENT"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~-]{20,}"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"(?i)(?:api[_-]?key|password|secret|token)\s*[:=]\s*[^\s]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
)


class AdapterError(Exception):
    """Stable, safe error returned by the adapter boundary."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.details = details

    def envelope(self, request_id: str | None = None) -> dict[str, Any]:
        return {
            "status": "BLOCKED",
            "error_code": self.code,
            "message": self.safe_message,
            "results": [],
            "degradation": None,
            "audit": {
                "request_id": request_id,
                "retryable": self.code in {"BACKEND_BUSY", "BACKEND_UNAVAILABLE", "TIMEOUT"},
                "raw_content_logged": False,
            },
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None, *, required: bool = False) -> datetime | None:
    if value is None or value == "":
        if required:
            raise AdapterError("INVALID_TIMESTAMP", "A timestamp is required")
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise AdapterError("INVALID_TIMESTAMP", "Timestamp must be a bounded ISO-8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AdapterError("INVALID_TIMESTAMP", "Timestamp is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise AdapterError("INVALID_TIMESTAMP", "Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def contains_sensitive(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def validate_identifier(value: str | None, field: str, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise AdapterError("INVALID_ENVELOPE", f"{field} is required")
        return None
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise AdapterError("INVALID_ENVELOPE", f"{field} is invalid or unbounded")
    if contains_sensitive(value):
        raise AdapterError("RAW_OR_SENSITIVE_CONTENT", f"{field} contains sensitive material")
    return value


@dataclass(frozen=True)
class ScopeContext:
    scope: str
    project_id: str | None = None
    worktree_id: str | None = None
    workflow_id: str | None = None
    agent_id: str | None = None

    def key(self) -> str:
        return canonical_json(self.to_dict())

    def to_dict(self) -> dict[str, str | None]:
        return {
            "scope": self.scope,
            "project_id": self.project_id,
            "worktree_id": self.worktree_id,
            "workflow_id": self.workflow_id,
            "agent_id": self.agent_id,
        }


def validate_scope(
    scope: str,
    project_id: str | None = None,
    worktree_id: str | None = None,
    workflow_id: str | None = None,
    agent_id: str | None = None,
) -> ScopeContext:
    if scope not in SCOPES:
        raise AdapterError("INVALID_ENUM", "scope is not supported")
    project_id = validate_identifier(project_id, "project_id")
    worktree_id = validate_identifier(worktree_id, "worktree_id")
    workflow_id = validate_identifier(workflow_id, "workflow_id")
    agent_id = validate_identifier(agent_id, "agent_id")
    if scope == "GLOBAL_USER":
        if project_id or worktree_id or workflow_id:
            raise AdapterError("CROSS_SCOPE_DENIED", "GLOBAL_USER cannot carry project identity")
    elif scope == "PROJECT":
        if not project_id or workflow_id:
            raise AdapterError("UNKNOWN_PROJECT_ID", "PROJECT requires one exact project_id")
    elif scope == "WORKFLOW":
        if not project_id or not workflow_id:
            raise AdapterError("UNKNOWN_PROJECT_ID", "WORKFLOW requires exact project and workflow identities")
    elif scope == "TOOL_ENVIRONMENT" and not agent_id:
        raise AdapterError("UNKNOWN_AGENT", "TOOL_ENVIRONMENT requires an exact agent_id")
    return ScopeContext(scope, project_id, worktree_id, workflow_id, agent_id)


def validate_fact(fact: str) -> str:
    if not isinstance(fact, str):
        raise AdapterError("INVALID_ENVELOPE", "fact must be a string")
    normalized = " ".join(fact.strip().split())
    if not normalized:
        raise AdapterError("INVALID_ENVELOPE", "fact must not be empty")
    if len(normalized.encode("utf-8")) > 2000:
        raise AdapterError("INVALID_ENVELOPE", "fact exceeds the 2000-byte limit")
    if contains_sensitive(normalized):
        raise AdapterError("RAW_OR_SENSITIVE_CONTENT", "fact contains sensitive material")
    lowered = normalized.casefold()
    if any(token in lowered for token in ("full transcript", "hidden reasoning", "whole repository")):
        raise AdapterError("RAW_OR_SENSITIVE_CONTENT", "raw source material is not admissible")
    return normalized


def validate_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(provenance, dict):
        raise AdapterError("PROVENANCE_REQUIRED", "source_provenance must be an object")
    required = {"source_class", "source_ref", "source_hash", "review_ref", "extraction_method"}
    if not required.issubset(provenance):
        raise AdapterError("PROVENANCE_REQUIRED", "source provenance is incomplete")
    allowed = required | {"source_fingerprint"}
    if set(provenance) - allowed:
        raise AdapterError("INVALID_ENVELOPE", "source provenance contains unknown fields")
    result: dict[str, Any] = {}
    for field, limit in (("source_class", 96), ("source_ref", 256), ("review_ref", 256), ("extraction_method", 96)):
        value = provenance.get(field)
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
            raise AdapterError("PROVENANCE_REQUIRED", f"{field} is invalid")
        value = value.strip()
        if "\n" in value or "\r" in value or contains_sensitive(value):
            raise AdapterError("RAW_OR_SENSITIVE_CONTENT", "source provenance contains unsafe material")
        result[field] = value
    source_hash = provenance.get("source_hash")
    if not isinstance(source_hash, str) or not _SHA256_RE.fullmatch(source_hash):
        raise AdapterError("PROVENANCE_REQUIRED", "source_hash must be a SHA-256 digest")
    result["source_hash"] = source_hash.lower()
    if "source_fingerprint" in provenance:
        fingerprint = provenance["source_fingerprint"]
        if not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint):
            raise AdapterError("PROVENANCE_REQUIRED", "source_fingerprint must be a SHA-256 digest")
        result["source_fingerprint"] = fingerprint.lower()
    return result


def validate_freshness(policy: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(policy, dict) or set(policy) - {"kind", "ttl_seconds", "recheck_ref", "source_fingerprint"}:
        raise AdapterError("INVALID_ENVELOPE", "freshness_policy is invalid")
    kind = policy.get("kind")
    if kind not in FRESHNESS_KINDS:
        raise AdapterError("INVALID_ENUM", "freshness policy kind is invalid")
    ttl = policy.get("ttl_seconds")
    if kind == "ttl":
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 31_536_000:
            raise AdapterError("INVALID_ENVELOPE", "ttl_seconds is invalid")
    elif ttl is not None:
        raise AdapterError("INVALID_ENVELOPE", "ttl_seconds is only valid for ttl policy")
    result = {"kind": kind, "ttl_seconds": ttl}
    for field in ("recheck_ref", "source_fingerprint"):
        value = policy.get(field)
        if value is not None:
            if not isinstance(value, str) or len(value.encode("utf-8")) > 256 or contains_sensitive(value):
                raise AdapterError("INVALID_ENVELOPE", f"{field} is invalid")
            result[field] = value
        else:
            result[field] = None
    return result


def scope_digest(scope: ScopeContext) -> str:
    return hashlib.sha256(scope.key().encode("utf-8")).hexdigest()


def native_hash(scope: ScopeContext, fact: str, *, revision_key: str | None = None) -> str:
    suffix = f"|{revision_key}" if revision_key else ""
    return hashlib.sha256((scope.key() + "|" + " ".join(fact.casefold().split()) + suffix).encode("utf-8")).hexdigest()


def is_fresh(
    freshness_policy: dict[str, Any],
    last_verified_at: str | None,
    source_provenance: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    kind = freshness_policy.get("kind")
    if kind in {"never", "manual"}:
        return True
    verified = parse_iso(last_verified_at)
    if verified is None:
        return False
    if kind == "ttl":
        return (now or utc_now()) <= verified + timedelta(seconds=freshness_policy["ttl_seconds"])
    expected = freshness_policy.get("source_fingerprint")
    actual = source_provenance.get("source_hash")
    return bool(expected and actual and expected == actual)


def finite_vector(vector: list[float]) -> None:
    if not vector or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector):
        raise AdapterError("EMBEDDING_UNAVAILABLE", "embedding is empty or contains invalid values")
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise AdapterError("EMBEDDING_UNAVAILABLE", "embedding norm is invalid")
