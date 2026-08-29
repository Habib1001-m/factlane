from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import Any, Self, cast
from uuid import uuid4

from .contract import AdapterError, contains_sensitive


class _ImmutableType(type):
    def __setattr__(cls, name: str, value: object) -> None:
        raise AttributeError(f"{cls.__name__} is immutable")

    def __delattr__(cls, name: str) -> None:
        raise AttributeError(f"{cls.__name__} is immutable")


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


class HostBinding(tuple, metaclass=_ImmutableType):
    """Immutable, trusted transport binding for one gateway instance."""

    __slots__ = ()

    def __new__(cls, bound_host_id: object, transport_kind: object, binding_source: object) -> Self:
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

    def __getattribute__(self, name: str) -> object:
        if name == "bound_host_id":
            return tuple.__getitem__(self, 0)
        if name == "transport_kind":
            return tuple.__getitem__(self, 1)
        if name == "binding_source":
            return tuple.__getitem__(self, 2)
        if name == "gateway_instance_id":
            return tuple.__getitem__(self, 3)
        return tuple.__getattribute__(self, name)

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
            "host_id": self.bound_host_id,
            "transport": self.transport_kind,
            "gateway_instance_id": self.gateway_instance_id,
            "binding_source": self.binding_source,
        }


def _binding_from_values(values: tuple[object, ...]) -> HostBinding:
    if len(values) != 4:
        raise AdapterError("INVALID_HOST_BINDING", "gateway binding is malformed")
    return tuple.__new__(
        HostBinding,
        (
            _validate_binding_value(values[0], "bound_host_id"),
            _validate_binding_value(values[1], "transport_kind"),
            _validate_binding_value(values[2], "binding_source"),
            _validate_binding_value(values[3], "gateway_instance_id"),
        ),
    )


class MemoryGateway(tuple, metaclass=_ImmutableType):
    """Project-neutral request gateway over the existing five-operation adapter."""

    __slots__ = ()

    def __new__(
        cls,
        adapter: Any,
        binding: HostBinding | None,
        *,
        transport_kind: str,
    ) -> Self:
        if binding is not None and type(binding) is not HostBinding:
            raise AdapterError("UNBOUND_GATEWAY", "gateway binding is not valid")
        binding_values = tuple(binding) if binding is not None else None
        if binding_values is not None:
            if len(binding_values) != 4:
                raise AdapterError("UNBOUND_GATEWAY", "gateway binding is malformed")
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
        return tuple.__new__(cls, (adapter, binding_values))

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("MemoryGateway cannot be subclassed")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("gateway state is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("gateway state is immutable")

    def _binding_values(self) -> tuple[str, str, str, str]:
        values = tuple.__getitem__(self, 1)
        if values is None:
            raise AdapterError("UNBOUND_GATEWAY", "gateway has no trusted host binding")
        return values

    @property
    def _adapter(self) -> Any:
        return tuple.__getitem__(self, 0)

    @property
    def _binding(self) -> HostBinding | None:
        values = tuple.__getitem__(self, 1)
        return _binding_from_values(values) if values is not None else None

    def require_transport(self, selected_transport: str) -> HostBinding:
        _validate_binding_value(selected_transport, "transport_kind")
        values = self._binding_values()
        if selected_transport != values[1]:
            raise AdapterError("HOST_TRANSPORT_IDENTITY_MISMATCH", "selected transport does not match its binding")
        return _binding_from_values(values)

    @property
    def binding(self) -> HostBinding | None:
        return self._binding

    def require_binding(self) -> HostBinding:
        return _binding_from_values(self._binding_values())

    @classmethod
    def tool_names(cls) -> list[str]:
        return ["memory_search", "memory_get", "memory_store", "memory_update", "memory_status"]

    @staticmethod
    def _validate_request(request: object) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise AdapterError("INVALID_ENVELOPE", "request must be an object")
        if any(not isinstance(key, str) for key in request):
            raise AdapterError("INVALID_ENVELOPE", "request keys must be strings")
        reserved_claims = frozenset(
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
        if reserved_claims.intersection(request):
            raise AdapterError("HOST_IDENTITY_CLAIM_DENIED", "request cannot claim transport identity")
        return request

    async def dispatch(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        binding_values = self._binding_values()
        if operation == "memory_search":
            method_name = "search"
        elif operation == "memory_get":
            method_name = "get"
        elif operation == "memory_store":
            method_name = "store"
        elif operation == "memory_update":
            method_name = "update"
        elif operation == "memory_status":
            method_name = "status"
        else:
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
        audit["host_binding"] = {
            "host_id": binding_values[0],
            "transport": binding_values[1],
            "gateway_instance_id": binding_values[3],
            "binding_source": binding_values[2],
        }
        envelope["audit"] = audit
        return envelope
