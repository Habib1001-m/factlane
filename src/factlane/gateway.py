from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

from .adapter import MemoryAdapter
from .contract import AdapterError, contains_sensitive

_MAX_BINDING_BYTES = 128
_BINDING_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RESERVED_IDENTITY_CLAIMS = frozenset(
    {
        "host_id",
        "bound_host_id",
        "host_identity",
        "transport_identity",
        "gateway_instance_id",
        "runtime_agent_id",
        "transport_kind",
    }
)
_OPERATION_METHODS = {
    "memory_search": "search",
    "memory_get": "get",
    "memory_store": "store",
    "memory_update": "update",
    "memory_status": "status",
}


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


class MemoryGateway:
    """Project-neutral request gateway over the existing five-operation adapter."""

    TOOL_NAMES = MemoryAdapter.TOOL_NAMES
    __slots__ = ("_adapter", "_binding")

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_binding" and hasattr(self, "_binding"):
            raise AttributeError("gateway binding is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name == "_binding":
            raise AttributeError("gateway binding is immutable")
        object.__delattr__(self, name)

    def __init__(
        self,
        adapter: Any,
        binding: HostBinding | None,
        *,
        transport_kind: str,
    ) -> None:
        if binding is not None and not isinstance(binding, HostBinding):
            raise AdapterError("UNBOUND_GATEWAY", "gateway binding is not valid")
        _validate_binding_value(transport_kind, "transport_kind")
        if binding is not None and binding.transport_kind != transport_kind:
            raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "gateway transport does not match its binding")
        self._adapter = adapter
        self._binding = binding

    @property
    def binding(self) -> HostBinding | None:
        return self._binding

    def require_binding(self) -> HostBinding:
        if self._binding is None:
            raise AdapterError("UNBOUND_GATEWAY", "gateway has no trusted host binding")
        return self._binding

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
        return request

    async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        binding = self.require_binding()
        method_name = _OPERATION_METHODS.get(operation)
        if method_name is None:
            raise AdapterError("INVALID_OPERATION", "operation is not part of the public gateway surface")
        safe_request = self._validate_request(request)
        handler = getattr(self._adapter, method_name, None)
        if not callable(handler):
            raise AdapterError("INVALID_OPERATION", "adapter does not implement the requested operation")
        async_handler = cast(Callable[..., Awaitable[dict[str, Any]]], handler)
        result = await async_handler(**dict(safe_request))
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
        envelope["audit"] = audit
        return envelope
