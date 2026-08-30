from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import FrozenInstanceError
from functools import partial
from typing import Any, Self, cast
from uuid import uuid4

from .contract import AdapterError, contains_sensitive


def _validate_binding_value(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AdapterError("INVALID_HOST_BINDING", f"{field} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AdapterError("INVALID_HOST_BINDING", f"{field} is not valid UTF-8") from exc
    if len(encoded) > 128:
        raise AdapterError("INVALID_HOST_BINDING", f"{field} is oversized")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise AdapterError("INVALID_HOST_BINDING", f"{field} contains control characters")
    if not re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", value) or contains_sensitive(value):
        raise AdapterError("INVALID_HOST_BINDING", f"{field} is not a safe non-secret value")
    return value


class HostBinding:
    """Public construction boundary for an immutable transport binding."""

    __slots__ = ()

    def __new__(
        cls,
        bound_host_id: object,
        transport_kind: object,
        binding_source: object,
    ) -> _HostBinding:
        if cls is not HostBinding:
            raise TypeError("HostBinding cannot be subclassed")
        return _HostBinding(bound_host_id, transport_kind, binding_source)


class _HostBinding(HostBinding, tuple):
    """Immutable transport binding returned by the public construction boundary."""

    __slots__ = ()

    def __new__(
        cls,
        bound_host_id: object,
        transport_kind: object,
        binding_source: object,
    ) -> Self:
        return tuple.__new__(
            cls,
            (
                _validate_binding_value(bound_host_id, "bound_host_id"),
                _validate_binding_value(transport_kind, "transport_kind"),
                _validate_binding_value(binding_source, "binding_source"),
                uuid4().hex,
            ),
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError(f"cannot assign to field {name!r}")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError(f"cannot delete field {name!r}")

    @property
    def bound_host_id(self) -> str:
        return tuple.__getitem__(self, 0)

    @property
    def transport_kind(self) -> str:
        return tuple.__getitem__(self, 1)

    @property
    def binding_source(self) -> str:
        return tuple.__getitem__(self, 2)

    @property
    def gateway_instance_id(self) -> str:
        return tuple.__getitem__(self, 3)

    def audit_projection(self) -> dict[str, str]:
        return {
            "host_id": tuple.__getitem__(self, 0),
            "transport": tuple.__getitem__(self, 1),
            "gateway_instance_id": tuple.__getitem__(self, 3),
            "binding_source": tuple.__getitem__(self, 2),
        }


def _binding_from_values(values: tuple[object, ...]) -> _HostBinding:
    if len(values) != 4:
        raise AdapterError("INVALID_HOST_BINDING", "gateway binding is malformed")
    return tuple.__new__(
        _HostBinding,
        (
            _validate_binding_value(values[0], "bound_host_id"),
            _validate_binding_value(values[1], "transport_kind"),
            _validate_binding_value(values[2], "binding_source"),
            _validate_binding_value(values[3], "gateway_instance_id"),
        ),
    )


def _validate_request(request: object) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise AdapterError("INVALID_ENVELOPE", "request must be an object")
    if any(not isinstance(key, str) for key in request):
        raise AdapterError("INVALID_ENVELOPE", "request keys must be strings")
    if {
        "host_id",
        "bound_host_id",
        "host_identity",
        "transport_identity",
        "gateway_instance_id",
        "runtime_agent_id",
        "transport_kind",
    }.intersection(request):
        raise AdapterError("HOST_IDENTITY_CLAIM_DENIED", "request cannot claim transport identity")
    return request


def _require_transport(values: tuple[str, str, str, str] | None, selected_transport: str) -> _HostBinding:
    if values is None:
        raise AdapterError("UNBOUND_GATEWAY", "gateway has no trusted host binding")
    _validate_binding_value(selected_transport, "transport_kind")
    if selected_transport != values[1]:
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "selected transport does not match its binding")
    return _binding_from_values(values)


def _require_binding(values: tuple[str, str, str, str] | None) -> _HostBinding:
    if values is None:
        raise AdapterError("UNBOUND_GATEWAY", "gateway has no trusted host binding")
    return _binding_from_values(values)


async def _dispatch(
    adapter: Any,
    binding_values: tuple[str, str, str, str] | None,
    operation: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    if binding_values is None:
        raise AdapterError("UNBOUND_GATEWAY", "gateway has no trusted host binding")
    method_name = {
        "memory_search": "search",
        "memory_get": "get",
        "memory_store": "store",
        "memory_update": "update",
        "memory_status": "status",
    }.get(operation)
    if method_name is None:
        raise AdapterError("INVALID_OPERATION", "operation is not part of the public gateway surface")
    safe_request = _validate_request(request)
    handler = getattr(adapter, method_name, None)
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
    audit["host_binding"] = {
        "host_id": binding_values[0],
        "transport": binding_values[1],
        "gateway_instance_id": binding_values[3],
        "binding_source": binding_values[2],
    }
    envelope["audit"] = audit
    return envelope


class MemoryGateway:
    """Public construction boundary for a transport-bound memory gateway."""

    __slots__ = ()

    def __new__(
        cls,
        adapter: Any,
        binding: Any,
        *,
        transport_kind: str,
    ) -> _MemoryGateway:
        if cls is not MemoryGateway:
            raise TypeError("MemoryGateway cannot be subclassed")
        return _make_memory_gateway(adapter, binding, transport_kind=transport_kind)

    @classmethod
    def tool_names(cls) -> list[str]:
        return ["memory_search", "memory_get", "memory_store", "memory_update", "memory_status"]


class _MemoryGateway(MemoryGateway):
    """Private gateway handle with immutable authoritative binding properties."""

    __slots__ = ("__dict__",)

    @property
    def _adapter(self) -> Any:
        return object.__getattribute__(self, "_state")[0]

    @property
    def _binding_values(self) -> tuple[str, str, str, str] | None:
        return object.__getattribute__(self, "_state")[1]

    @property
    def _binding(self) -> _HostBinding | None:
        values = self._binding_values
        return _binding_from_values(values) if values is not None else None

    @property
    def binding(self) -> _HostBinding | None:
        return self._binding

    @property
    def dispatch(self) -> Any:
        return partial(_dispatch, self._adapter, self._binding_values)

    @property
    def require_transport(self) -> Any:
        return partial(_require_transport, self._binding_values)

    @property
    def require_binding(self) -> Any:
        return partial(_require_binding, self._binding_values)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("gateway state is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("gateway state is immutable")


def _make_memory_gateway(
    adapter: Any,
    binding: _HostBinding | None,
    *,
    transport_kind: str,
) -> _MemoryGateway:
    if binding is not None and type(binding) is not _HostBinding:
        raise AdapterError("UNBOUND_GATEWAY", "gateway binding is not valid")
    binding_values = (
        tuple.__getitem__(binding, 0),
        tuple.__getitem__(binding, 1),
        tuple.__getitem__(binding, 2),
        tuple.__getitem__(binding, 3),
    ) if binding is not None else None
    if binding_values is not None:
        binding_values = (
            _validate_binding_value(binding_values[0], "bound_host_id"),
            _validate_binding_value(binding_values[1], "transport_kind"),
            _validate_binding_value(binding_values[2], "binding_source"),
            _validate_binding_value(binding_values[3], "gateway_instance_id"),
        )
    _validate_binding_value(transport_kind, "transport_kind")
    if transport_kind != "stdio":
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "gateway transport is not supported")
    if binding_values is not None and binding_values[1] != transport_kind:
        raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "gateway transport does not match its binding")

    gateway = object.__new__(_MemoryGateway)
    object.__getattribute__(gateway, "__dict__")["_state"] = (adapter, binding_values)
    return gateway
