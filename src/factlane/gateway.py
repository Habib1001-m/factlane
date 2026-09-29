from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .adapter import MemoryAdapter
from .contract import PUBLIC_TOOL_NAMES, AdapterError, contains_sensitive, validate_identifier

_MAX_BINDING_BYTES = 128
_BINDING_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RESERVED_IDENTITY_CLAIMS = frozenset(
    {
        "bound_agent_id",
        "host_id",
        "bound_host_id",
        "bound_project_id",
        "bound_workflow_id",
        "context_binding",
        "context_binding_source",
        "host_identity",
        "session_context",
        "transport_identity",
        "gateway_instance_id",
        "runtime_agent_id",
        "transport_kind",
    }
)
_RESERVED_TRUST_CLAIMS = frozenset(
    {
        "authority_role",
        "contribution_origin",
        "contributor_class",
        "contributor_ref",
        "effective_authority_role",
        "effective_lifecycle_state",
        "effective_verified_by",
        "grant_ref",
        "owner_authorized",
        "principal_class",
        "public_contract_revision_override",
        "runtime_contract_marker",
        "storage_contract_version",
        "trusted_actor",
        "trusted_write_context",
        "verification_authority",
        "write_authorization",
        "write_capability",
    }
)


def _validate_binding_value(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AdapterError("INVALID_HOST_BINDING", f"{field} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AdapterError("INVALID_HOST_BINDING", f"{field} is not valid UTF-8") from exc
    if len(encoded) > _MAX_BINDING_BYTES:
        raise AdapterError("INVALID_HOST_BINDING", f"{field} is oversized")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise AdapterError("INVALID_HOST_BINDING", f"{field} contains control characters")
    if not _BINDING_VALUE_RE.fullmatch(value) or contains_sensitive(value):
        raise AdapterError("INVALID_HOST_BINDING", f"{field} is not a safe non-secret value")
    return value


@dataclass(frozen=True, slots=True)
class HostBinding:
    """Immutable, trusted transport binding for one gateway instance."""

    bound_host_id: str
    transport_kind: str
    binding_source: str
    gateway_instance_id: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_binding_value(self.bound_host_id, "bound_host_id")
        _validate_binding_value(self.transport_kind, "transport_kind")
        _validate_binding_value(self.binding_source, "binding_source")
        object.__setattr__(self, "gateway_instance_id", uuid4().hex)

    def audit_projection(self) -> dict[str, str]:
        return {
            "host_id": self.bound_host_id,
            "transport": self.transport_kind,
            "gateway_instance_id": self.gateway_instance_id,
            "binding_source": self.binding_source,
        }


def _validate_context_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    try:
        return validate_identifier(value, field)  # type: ignore[arg-type]
    except AdapterError as exc:
        raise AdapterError(
            "INVALID_CONTEXT_BINDING",
            f"{field} is not a valid trusted context identity",
        ) from exc


@dataclass(frozen=True, slots=True)
class TrustedContextBinding:
    """Immutable exact current-context identities supplied by the trusted host/session boundary."""

    project_id: str | None = None
    workflow_id: str | None = None
    agent_id: str | None = None
    project_id_source: str | None = None
    workflow_id_source: str | None = None
    agent_id_source: str | None = None
    binding_source: str = "explicit-launcher"

    def __post_init__(self) -> None:
        project_id = _validate_context_identifier(self.project_id, "project_id")
        workflow_id = _validate_context_identifier(self.workflow_id, "workflow_id")
        agent_id = _validate_context_identifier(self.agent_id, "agent_id")
        _validate_binding_value(self.binding_source, "context_binding_source")
        project_id_source = self.project_id_source or (self.binding_source if project_id is not None else None)
        workflow_id_source = self.workflow_id_source or (self.binding_source if workflow_id is not None else None)
        agent_id_source = self.agent_id_source or (self.binding_source if agent_id is not None else None)
        for source, field in (
            (project_id_source, "project_id_source"),
            (workflow_id_source, "workflow_id_source"),
            (agent_id_source, "agent_id_source"),
        ):
            if source is not None:
                _validate_binding_value(source, field)
        if self.project_id_source is not None and project_id is None:
            raise AdapterError("INVALID_CONTEXT_BINDING", "project_id_source requires project_id")
        if self.workflow_id_source is not None and workflow_id is None:
            raise AdapterError("INVALID_CONTEXT_BINDING", "workflow_id_source requires workflow_id")
        if self.agent_id_source is not None and agent_id is None:
            raise AdapterError("INVALID_CONTEXT_BINDING", "agent_id_source requires agent_id")
        if project_id is None and workflow_id is None and agent_id is None:
            raise AdapterError(
                "INVALID_CONTEXT_BINDING",
                "trusted context binding must contain at least one identity",
            )
        if workflow_id is not None and project_id is None:
            raise AdapterError(
                "INVALID_CONTEXT_BINDING",
                "workflow_id requires a bound project_id",
            )
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "workflow_id", workflow_id)
        object.__setattr__(self, "agent_id", agent_id)
        object.__setattr__(self, "project_id_source", project_id_source)
        object.__setattr__(self, "workflow_id_source", workflow_id_source)
        object.__setattr__(self, "agent_id_source", agent_id_source)

    def audit_projection(self) -> dict[str, str]:
        projection = {"binding_source": self.binding_source}
        if self.project_id is not None:
            projection["project_id"] = self.project_id
            projection["project_id_source"] = self.project_id_source or self.binding_source
        if self.workflow_id is not None:
            projection["workflow_id"] = self.workflow_id
            projection["workflow_id_source"] = self.workflow_id_source or self.binding_source
        if self.agent_id is not None:
            projection["agent_id"] = self.agent_id
            projection["agent_id_source"] = self.agent_id_source or self.binding_source
        return projection


class MemoryGateway:
    """Project-neutral request gateway over the existing five-operation adapter."""

    TOOL_NAMES = MemoryAdapter.TOOL_NAMES
    __slots__ = ("_adapter", "_binding_value", "_context_binding_value")

    def __init__(
        self,
        adapter: Any,
        binding: HostBinding | None,
        *,
        transport_kind: str,
        context_binding: TrustedContextBinding | None = None,
    ) -> None:
        if binding is not None and not isinstance(binding, HostBinding):
            raise AdapterError("UNBOUND_GATEWAY", "gateway binding is not valid")
        if context_binding is not None and not isinstance(context_binding, TrustedContextBinding):
            raise AdapterError("INVALID_CONTEXT_BINDING", "gateway trusted context binding is not valid")
        _validate_binding_value(transport_kind, "transport_kind")
        if transport_kind != "stdio":
            raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "gateway transport is not supported")
        if binding is not None and binding.transport_kind != transport_kind:
            raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "gateway transport does not match its binding")
        self._adapter = adapter
        self._binding_value = binding
        self._context_binding_value = context_binding

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"_binding_value", "_context_binding_value"} and hasattr(self, name):
            raise AttributeError("gateway binding is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in {"_binding", "_binding_value", "_context_binding", "_context_binding_value"}:
            raise AttributeError("gateway binding is immutable")
        object.__delattr__(self, name)

    @property
    def _binding(self) -> HostBinding | None:
        return self._binding_value

    @property
    def binding(self) -> HostBinding | None:
        return self._binding_value

    @property
    def context_binding(self) -> TrustedContextBinding | None:
        return self._context_binding_value

    def require_binding(self) -> HostBinding:
        if self._binding_value is None:
            raise AdapterError("UNBOUND_GATEWAY", "gateway has no trusted host binding")
        return self._binding_value

    def require_transport(self, selected_transport: str) -> HostBinding:
        binding = self.require_binding()
        _validate_binding_value(selected_transport, "transport_kind")
        if selected_transport != "stdio" or binding.transport_kind != selected_transport:
            raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "selected transport does not match its binding")
        return binding

    @classmethod
    def tool_names(cls) -> list[str]:
        return list(cls.TOOL_NAMES)

    @staticmethod
    def _validate_request(request: object) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise AdapterError("INVALID_ENVELOPE", "request must be an object")
        if any(not isinstance(key, str) for key in request):
            raise AdapterError("INVALID_ENVELOPE", "request keys must be strings")
        if _RESERVED_IDENTITY_CLAIMS.intersection(request):
            raise AdapterError("HOST_IDENTITY_CLAIM_DENIED", "request cannot claim transport identity")
        if _RESERVED_TRUST_CLAIMS.intersection(request):
            raise AdapterError("WRITE_AUTHORIZATION_DENIED", "request cannot claim trusted or derived authority")
        return request

    def _bind_trusted_context(self, request: dict[str, Any]) -> dict[str, Any]:
        context = self._context_binding_value
        if context is None:
            return dict(request)
        bound_request = dict(request)
        scope = bound_request.get("scope")
        required: tuple[tuple[str, str | None], ...]
        if scope == "PROJECT":
            required = (("project_id", context.project_id),)
        elif scope == "WORKFLOW":
            required = (("project_id", context.project_id), ("workflow_id", context.workflow_id))
        elif scope == "TOOL_ENVIRONMENT":
            required = (("agent_id", context.agent_id),)
        else:
            return bound_request
        for field, trusted_value in required:
            if trusted_value is None:
                continue
            if field in bound_request:
                if bound_request[field] != trusted_value:
                    raise AdapterError(
                        "BOUND_CONTEXT_IDENTITY_MISMATCH",
                        f"{field} conflicts with trusted current context",
                    )
            else:
                bound_request[field] = trusted_value
        return bound_request

    async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        binding = self.require_transport("stdio")
        if operation not in PUBLIC_TOOL_NAMES:
            raise AdapterError("INVALID_OPERATION", "operation is not part of the public gateway surface")
        safe_request = self._bind_trusted_context(self._validate_request(request))
        if operation in {"memory_store", "memory_update"} and "provenance" in safe_request:
            raise AdapterError(
                "INVALID_ENVELOPE",
                "use source_provenance (an object); provenance is not a FactLane request field",
            )
        handler = getattr(self._adapter, "dispatch", None)
        if not callable(handler):
            raise AdapterError("INVALID_OPERATION", "adapter does not implement the requested operation")
        result = await handler(operation, dict(safe_request))
        if not isinstance(result, dict):
            raise AdapterError("INVALID_ADAPTER_RESPONSE", "adapter returned an invalid response")
        try:
            envelope = deepcopy(result)
        except Exception as exc:
            raise AdapterError("INVALID_ADAPTER_RESPONSE", "adapter response could not be safely copied") from exc
        audit = envelope.get("audit")
        if audit is None:
            audit = {}
        if not isinstance(audit, dict):
            raise AdapterError("INVALID_ADAPTER_RESPONSE", "adapter audit envelope is invalid")
        audit["host_binding"] = binding.audit_projection()
        if self._context_binding_value is not None:
            audit["context_binding"] = self._context_binding_value.audit_projection()
        envelope["audit"] = audit
        return envelope
