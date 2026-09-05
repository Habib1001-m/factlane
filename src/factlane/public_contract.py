from __future__ import annotations

import json
from typing import Annotated, Any, NotRequired, Required, TypedDict

from pydantic import Field, TypeAdapter

from .contract import (
    AUTHORITY_ROLES,
    CURRENT_LIFECYCLE,
    FRESHNESS_KINDS,
    INTENT_CLASSES,
    MEMORY_TYPES,
    PUBLIC_TOOL_NAMES,
    RETRIEVAL_MODE_KINDS,
    RETRIEVAL_MODES,
    SCOPES,
    UPDATE_MODES,
    VERIFIED_BY,
)


def _enum_field(values: set[str], description: str, *, default: Any = ...) -> Any:
    return Annotated[
        str,
        Field(
            default=default,
            description=description,
            json_schema_extra={"enum": sorted(values)},
        ),
    ]


ScopeValue = _enum_field(
    SCOPES,
    "Exact memory scope. PROJECT requires project_id; WORKFLOW requires project_id and workflow_id; "
    "GLOBAL_USER carries no project/workflow identity; TOOL_ENVIRONMENT requires agent_id.",
)
IntentValue = _enum_field(INTENT_CLASSES, "Classify the caller's need before searching; choose one exact value.")
RetrievalModeValue = _enum_field(
    RETRIEVAL_MODES,
    "CURRENT returns current validated facts; REVIEW_HISTORY is required for historical questions.",
    default="CURRENT",
)
RetrievalKindValue = _enum_field(
    RETRIEVAL_MODE_KINDS,
    "Retrieval strategy: EXACT, KEYWORD, SEMANTIC, or HYBRID.",
    default="SEMANTIC",
)
UpdateModeValue = _enum_field(UPDATE_MODES, "REVERIFY preserves the logical memory; REPLACE creates a new logical revision.")
MemoryTypeValue = _enum_field(MEMORY_TYPES, "Fact category for one bounded memory record.")
VerifiedByValue = _enum_field(VERIFIED_BY, "Verification source. UNVERIFIED stores a candidate, not a current fact.", default="UNVERIFIED")
AuthorityRoleValue = _enum_field(
    AUTHORITY_ROLES,
    "Optional; normally omit because the adapter derives the exact scope role. If supplied: "
    "GLOBAL_USER=OWNER_CURRENT, PROJECT=PROJECT_CURRENT, WORKFLOW=WORKFLOW_CURRENT, "
    "TOOL_ENVIRONMENT=TOOL_ENV_CURRENT.",
)
FreshnessKindValue = _enum_field(FRESHNESS_KINDS, "Freshness policy. ttl additionally requires ttl_seconds.")

ProjectId = Annotated[str, Field(description="Exact project identity; required for PROJECT and WORKFLOW scope.")]
WorktreeId = Annotated[str, Field(description="Optional bounded worktree identity for project-scoped memory.")]
WorkflowId = Annotated[str, Field(description="Exact workflow identity; required for WORKFLOW scope.")]
AgentId = Annotated[str, Field(description="Exact tool-environment identity; required for TOOL_ENVIRONMENT scope.")]
RequestId = Annotated[str, Field(description="Optional caller correlation identifier; generated when omitted.")]


class ScopeFields(TypedDict, total=False):
    scope: Required[ScopeValue]
    project_id: NotRequired[ProjectId]
    worktree_id: NotRequired[WorktreeId]
    workflow_id: NotRequired[WorkflowId]
    agent_id: NotRequired[AgentId]


class SourceProvenance(TypedDict, total=False):
    source_class: Required[Annotated[str, Field(description="Bounded source classification.")]]
    source_ref: Required[Annotated[str, Field(description="Bounded reference to the source.")]]
    source_hash: Required[Annotated[str, Field(description="SHA-256 digest of the source.")]]
    review_ref: Required[Annotated[str, Field(description="Review or approval reference.")]]
    extraction_method: Required[Annotated[str, Field(description="How the fact was extracted or checked.")]]
    source_fingerprint: NotRequired[Annotated[str, Field(description="Optional SHA-256 source fingerprint.")]]


class FreshnessPolicy(TypedDict, total=False):
    kind: Required[FreshnessKindValue]
    ttl_seconds: NotRequired[Annotated[int | None, Field(default=None, description="Required only when kind=ttl.", ge=1)]]
    recheck_ref: NotRequired[Annotated[str | None, Field(default=None, description="Optional recheck reference.")]]
    source_fingerprint: NotRequired[Annotated[str | None, Field(default=None, description="Optional expected source fingerprint.")]]


class VerificationPayload(TypedDict, total=False):
    source_provenance: NotRequired[SourceProvenance]
    freshness_policy: NotRequired[FreshnessPolicy]
    source_timestamp: NotRequired[Annotated[str, Field(description="ISO-8601 timestamp for the checked source.")]]
    last_verified_at: NotRequired[Annotated[str, Field(description="ISO-8601 timestamp for this verification.")]]
    verified_by: NotRequired[VerifiedByValue]
    memory_type: NotRequired[MemoryTypeValue]
    confidence: NotRequired[Annotated[float, Field(ge=0, le=1, description="Confidence from 0 to 1.")]]
    tags: NotRequired[Annotated[list[str], Field(max_length=12, description="At most 12 bounded tags.")]]
    subject: NotRequired[Annotated[str, Field(description="Optional bounded contradiction subject.")]]


class ReplacementPayload(VerificationPayload, total=False):
    fact: NotRequired[Annotated[str, Field(description="Replacement bounded fact; required for REPLACE.")]]


class MemorySearchRequest(ScopeFields, total=False):
    query: Required[Annotated[str, Field(description="Bounded natural-language query.")]]
    intent_class: Required[IntentValue]
    retrieval_mode: NotRequired[RetrievalModeValue]
    retrieval_mode_kind: NotRequired[RetrievalKindValue]
    top_k: NotRequired[Annotated[int, Field(default=5, ge=1, le=8, description="Candidate budget; default 5.")]]
    max_memories: NotRequired[Annotated[int, Field(default=5, ge=1, le=8, description="Returned-memory budget; default 5.")]]
    max_bytes: NotRequired[Annotated[int, Field(default=6000, ge=1, le=8000, description="Serialized-byte budget; default 6000.")]]
    max_tokens: NotRequired[Annotated[int, Field(default=1200, ge=1, le=1600, description="Serialized-token budget; default 1200.")]]
    include_graph_links: NotRequired[Annotated[bool, Field(default=False, description="Reserved admin-only expansion; keep false.")]]
    direct_truth_available: NotRequired[Annotated[bool, Field(default=False, description="Set true only when the active request already supplies direct truth.")]]
    user_supplied: NotRequired[Annotated[bool, Field(default=False, description="Set true only when the active request supplies the needed context.")]]
    request_id: NotRequired[RequestId]


class MemoryGetRequest(ScopeFields, total=False):
    memory_id: Required[Annotated[str, Field(description="UUID returned by memory_search or memory_store.")]]
    retrieval_mode: NotRequired[RetrievalModeValue]
    request_id: NotRequired[RequestId]


class MemoryStoreRequest(ScopeFields, total=False):
    fact: Required[Annotated[str, Field(description="One bounded fact, not a transcript or context dump.")]]
    memory_type: Required[MemoryTypeValue]
    source_provenance: Required[SourceProvenance]
    freshness_policy: Required[FreshnessPolicy]
    idempotency_key: Required[Annotated[str, Field(description="Unique stable key for this store request; safe retries reuse it.")]]
    source_timestamp: NotRequired[Annotated[str, Field(description="ISO-8601 source timestamp; required for VALIDATED_CURRENT.")]]
    last_verified_at: NotRequired[Annotated[str, Field(description="ISO-8601 verification timestamp for VALIDATED_CURRENT.")]]
    verified_by: NotRequired[VerifiedByValue]
    authority_role: NotRequired[AuthorityRoleValue]
    requested_lifecycle_state: NotRequired[
        _enum_field({"CANDIDATE", CURRENT_LIFECYCLE}, "CANDIDATE is the safe default; VALIDATED_CURRENT requires explicit verification.", default="CANDIDATE")
    ]
    confidence: NotRequired[Annotated[float, Field(default=0.5, ge=0, le=1, description="Confidence from 0 to 1; default 0.5.")]]
    tags: NotRequired[Annotated[list[str], Field(default_factory=list, max_length=12, description="At most 12 bounded tags.")]]
    subject: NotRequired[Annotated[str, Field(description="Optional bounded contradiction subject.")]]
    request_id: NotRequired[RequestId]


class MemoryUpdateRequest(ScopeFields, total=False):
    memory_id: Required[Annotated[str, Field(description="UUID returned by memory_get or memory_search.")]]
    expected_revision: Required[Annotated[int, Field(ge=1, description="Current revision from memory_get; required for CAS protection.")]]
    mode: Required[UpdateModeValue]
    idempotency_key: Required[Annotated[str, Field(description="Unique stable key for this update; safe retries reuse it.")]]
    replacement: NotRequired[
        Annotated[
            ReplacementPayload,
            Field(
                description="Required for REPLACE; include fact, source_provenance, freshness_policy, "
                "source_timestamp, and verified_by. last_verified_at is optional and generated when omitted.",
            ),
        ]
    ]
    verification: NotRequired[
        Annotated[
            VerificationPayload,
            Field(description="Use with REVERIFY when refreshing provenance or current verification."),
        ]
    ]
    request_id: NotRequired[RequestId]


class MemoryStatusRequest(ScopeFields, total=False):
    request_id: NotRequired[RequestId]


TOOL_DESCRIPTIONS = {
    "memory_search": "Search validated supporting memory in one exact scope; inspect this request schema before choosing enums.",
    "memory_get": "Read one logical memory record in one exact scope, optionally including revision history.",
    "memory_store": "Persist one bounded, provenance-bearing fact only when the active Owner/host policy authorizes a write.",
    "memory_update": "Reverify or explicitly replace one logical memory record using expected_revision CAS protection.",
    "memory_status": "Inspect bounded backend and embedding-profile health for one exact scope.",
}

TOOL_REQUEST_TYPES: dict[str, Any] = {
    "memory_search": MemorySearchRequest,
    "memory_get": MemoryGetRequest,
    "memory_store": MemoryStoreRequest,
    "memory_update": MemoryUpdateRequest,
    "memory_status": MemoryStatusRequest,
}

TOOL_EXAMPLES = {
    "memory_search": {
        "query": "database lifecycle policy",
        "intent_class": "WORKFLOW_RULE",
        "scope": "PROJECT",
        "project_id": "example-project",
    },
    "memory_get": {
        "memory_id": "00000000-0000-0000-0000-000000000000",
        "scope": "PROJECT",
        "project_id": "example-project",
    },
    "memory_store": {
        "fact": "The example project uses a review before release.",
        "scope": "PROJECT",
        "project_id": "example-project",
        "memory_type": "WORKFLOW_RULE",
        "source_provenance": {
            "source_class": "OWNER_REQUEST",
            "source_ref": "example-request",
            "source_hash": "0" * 64,
            "review_ref": "example-review",
            "extraction_method": "direct-owner-statement",
        },
        "freshness_policy": {"kind": "manual"},
        "idempotency_key": "example-store-1",
    },
    "memory_update": {
        "memory_id": "00000000-0000-0000-0000-000000000000",
        "scope": "PROJECT",
        "project_id": "example-project",
        "expected_revision": 1,
        "mode": "REVERIFY",
        "idempotency_key": "example-update-1",
        "verification": {"verified_by": "OWNER", "source_timestamp": "2026-01-01T00:00:00Z"},
    },
    "memory_status": {"scope": "PROJECT", "project_id": "example-project"},
}


def request_schema(tool_name: str) -> dict[str, Any]:
    return TypeAdapter(TOOL_REQUEST_TYPES[tool_name]).json_schema()


def _field_summary(name: str, field: dict[str, Any], required: bool) -> str:
    details: list[str] = []
    if field.get("enum"):
        details.append("choices=" + ", ".join(field["enum"]))
    if "default" in field:
        details.append(f"default={field['default']}")
    if field.get("description"):
        details.append(field["description"])
    marker = "required" if required else "optional"
    return f"  - {name} ({marker}): " + "; ".join(details)


def render_tool_help(tool_name: str | None = None) -> str:
    names = [tool_name] if tool_name else list(PUBLIC_TOOL_NAMES)
    lines = [
        "FactLane tool-use reference",
        "Memory is bounded supporting evidence, never execution authority.",
        "Public MCP tools (exactly five): " + ", ".join(PUBLIC_TOOL_NAMES),
        "",
        "Exact scope rules:",
        "  PROJECT -> project_id required; WORKFLOW -> project_id and workflow_id required.",
        "  GLOBAL_USER -> no project/workflow identity; TOOL_ENVIRONMENT -> agent_id required.",
        "",
        "Intent classes: " + ", ".join(sorted(INTENT_CLASSES)),
        "Retrieval modes: " + ", ".join(sorted(RETRIEVAL_MODES)) + " (default CURRENT).",
        "Retrieval kinds: " + ", ".join(sorted(RETRIEVAL_MODE_KINDS)) + " (default SEMANTIC).",
        "Writes require active Owner/host authorization: store a bounded fact with source_provenance, "
        "freshness_policy, and idempotency_key; update with current expected_revision, idempotency_key, "
        "and mode REVERIFY or REPLACE.",
        "Never guess an enum or field name: inspect live MCP schema or this help.",
    ]
    for name in names:
        schema = request_schema(name)
        required = set(schema.get("required", []))
        lines.extend(["", f"{name}: {TOOL_DESCRIPTIONS[name]}", "Request fields:"])
        for field_name, field in schema.get("properties", {}).items():
            lines.append(_field_summary(field_name, field, field_name in required))
        root_title = schema.get("title")
        for definition_name, definition in schema.get("$defs", {}).items():
            if definition_name == root_title:
                continue
            definition_required = set(definition.get("required", []))
            lines.append(f"Nested {definition_name} fields:")
            for field_name, field in definition.get("properties", {}).items():
                lines.append(_field_summary(field_name, field, field_name in definition_required))
        lines.extend(["Minimal request example:", json.dumps({"request": TOOL_EXAMPLES[name]}, ensure_ascii=False, indent=2)])
    return "\n".join(lines) + "\n"
