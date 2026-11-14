from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import socket
import stat
import struct
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .windows_security import (
    WindowsSecurityError,
    current_user_sid,
    ensure_owner_private_directory as ensure_windows_owner_private_directory,
    owner_only_security_attributes,
    process_identity as windows_process_identity,
    protect_owner_private_path,
    require_process_identity,
    validate_local_named_pipe_name,
    validate_owner_only_kernel_object,
    validate_owner_private_path,
)


PROTOCOL_VERSION = 1
MAX_CONTROL_MESSAGE_BYTES = 8 * 1024
CONTROL_COMMANDS = frozenset({"INSPECT", "PREPARE_CLOSE", "ABORT", "RESUME"})
_LIFECYCLE_STATES = frozenset({"PARKED", "OPENING", "OPEN", "PREPARING", "CLOSED", "CLOSE_FAILED", "REOPENING"})
_REQUEST_COMMON_KEYS = frozenset(
    {"protocol_version", "request_id", "operation_id", "instance_id", "expected_generation", "command"}
)
_RESPONSE_KEYS = frozenset(
    {
        "protocol_version",
        "request_id",
        "operation_id",
        "instance_id",
        "state",
        "actual_generation",
        "status_code",
        "lease_open",
    }
)
_IDENTITY_KEYS = frozenset(
    {
        "protocol_version",
        "instance_id",
        "owner_uid",
        "pid",
        "process_start_ticks",
        "boot_id",
        "database_path",
        "device",
        "inode",
        "generation",
        "control_socket_path",
        "lifecycle_state",
    }
)
_WINDOWS_RUNTIME_PIPE_PREFIX = "factlane-runtime-"


class RuntimeControlError(RuntimeError):
    """Fail-closed runtime-control protocol or identity error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RuntimeControlError("INVALID_CONTROL_MESSAGE", f"{field} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise RuntimeControlError("INVALID_CONTROL_MESSAGE", f"{field} must be a UUID string") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise RuntimeControlError("INVALID_CONTROL_MESSAGE", f"{field} must use canonical UUID form")
    return canonical


def _bounded_text(value: object, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise RuntimeControlError("INVALID_CONTROL_MESSAGE", f"{field} is invalid or oversized")
    if any(ord(char) < 32 for char in value):
        raise RuntimeControlError("INVALID_CONTROL_MESSAGE", f"{field} contains control characters")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeControlError("INVALID_CONTROL_MESSAGE", f"{field} must be a non-negative integer")
    return value


def _positive_int(value: object, field: str) -> int:
    result = _nonnegative_int(value, field)
    if result == 0:
        raise RuntimeControlError("INVALID_CONTROL_MESSAGE", f"{field} must be positive")
    return result


def _ensure_owner_private_directory(path: Path) -> None:
    if os.name == "nt":
        try:
            ensure_windows_owner_private_directory(path, owner_sid=current_user_sid())
        except WindowsSecurityError as exc:
            raise RuntimeControlError("PRIVATE_STATE_UNAVAILABLE", exc.safe_message) from exc
        return
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise RuntimeControlError("PRIVATE_STATE_UNAVAILABLE", "runtime-control directory cannot be protected") from exc


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    _ensure_owner_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name == "nt":
                protect_owner_private_path(temporary, owner_sid=current_user_sid(), directory=False)
            os.replace(temporary, path)
            if os.name == "nt":
                protect_owner_private_path(path, owner_sid=current_user_sid(), directory=False)
            else:
                os.chmod(path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
    except OSError as exc:
        raise RuntimeControlError("PRIVATE_STATE_UNAVAILABLE", "runtime-control state could not be persisted") from exc


def current_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise RuntimeControlError("PROCESS_IDENTITY_UNAVAILABLE", "Linux boot identity is unavailable") from exc


def process_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        raise RuntimeControlError("PROCESS_IDENTITY_UNAVAILABLE", "process start identity is unavailable") from exc
    closing = raw.rfind(")")
    if closing < 0:
        raise RuntimeControlError("PROCESS_IDENTITY_UNAVAILABLE", "process start identity is malformed")
    fields = raw[closing + 2 :].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError) as exc:
        raise RuntimeControlError("PROCESS_IDENTITY_UNAVAILABLE", "process start identity is malformed") from exc


def windows_runtime_pipe_name(instance_id: str) -> str:
    canonical = _canonical_uuid(instance_id, "instance_id")
    return rf"\\.\pipe\{_WINDOWS_RUNTIME_PIPE_PREFIX}{uuid.UUID(canonical).hex}"


def _is_windows_runtime_pipe(value: str) -> bool:
    return isinstance(value, str) and value.casefold().startswith(r"\\.\pipe\factlane-runtime-".casefold())


def _canonical_control_endpoint(value: object, instance_id: str) -> str:
    endpoint = _bounded_text(value, "control_socket_path", maximum=4096)
    if os.name == "nt" and _is_windows_runtime_pipe(endpoint):
        expected = windows_runtime_pipe_name(instance_id)
        if endpoint.casefold() != expected.casefold():
            raise RuntimeControlError(
                "PATH_IDENTITY_MISMATCH",
                "runtime-control Named Pipe does not match the registered instance identity",
            )
        try:
            return validate_local_named_pipe_name(endpoint, prefix=_WINDOWS_RUNTIME_PIPE_PREFIX)
        except WindowsSecurityError as exc:
            raise RuntimeControlError("PATH_IDENTITY_MISMATCH", exc.safe_message) from exc
    return os.path.abspath(endpoint)


@dataclass(frozen=True)
class BarrierSnapshot:
    state: str
    operation_id: str | None
    generation: int
    original_generation: int | None
    intent_revision: int
    database_path: str
    device: int
    inode: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "BarrierSnapshot":
        expected = {
            "state",
            "operation_id",
            "generation",
            "original_generation",
            "intent_revision",
            "database_path",
            "device",
            "inode",
        }
        if set(value) != expected:
            raise RuntimeControlError("INVALID_BARRIER_STATE", "maintenance barrier state has unexpected fields")
        operation = value["operation_id"]
        if operation is not None:
            operation = _canonical_uuid(operation, "operation_id")
        original = value["original_generation"]
        if original is not None:
            original = _nonnegative_int(original, "original_generation")
        return cls(
            state=_bounded_text(value["state"], "state", maximum=64),
            operation_id=operation,
            generation=_nonnegative_int(value["generation"], "generation"),
            original_generation=original,
            intent_revision=_nonnegative_int(value["intent_revision"], "intent_revision"),
            database_path=os.path.realpath(os.path.abspath(_bounded_text(value["database_path"], "database_path", maximum=4096))),
            device=_nonnegative_int(value["device"], "device"),
            inode=_nonnegative_int(value["inode"], "inode"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "state": self.state,
            "operation_id": self.operation_id,
            "generation": self.generation,
            "original_generation": self.original_generation,
            "intent_revision": self.intent_revision,
            "database_path": self.database_path,
            "device": self.device,
            "inode": self.inode,
        }


class BarrierAccess(Protocol):
    def guard(self) -> AbstractContextManager[None]: ...

    def read_locked(self) -> BarrierSnapshot: ...

    def read(self) -> BarrierSnapshot: ...


@dataclass(frozen=True)
class RuntimeIdentity:
    protocol_version: int
    instance_id: str
    owner_uid: int
    pid: int
    process_start_ticks: int
    boot_id: str
    database_path: str
    device: int
    inode: int
    generation: int
    control_socket_path: str
    lifecycle_state: str

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise RuntimeControlError("UNSUPPORTED_PROTOCOL", "runtime registration protocol version is unsupported")
        instance_id = _canonical_uuid(self.instance_id, "instance_id")
        _nonnegative_int(self.owner_uid, "owner_uid")
        _positive_int(self.pid, "pid")
        _nonnegative_int(self.process_start_ticks, "process_start_ticks")
        _bounded_text(self.boot_id, "boot_id", maximum=128)
        _nonnegative_int(self.device, "device")
        _nonnegative_int(self.inode, "inode")
        _nonnegative_int(self.generation, "generation")
        if self.lifecycle_state not in _LIFECYCLE_STATES:
            raise RuntimeControlError("INVALID_RUNTIME_STATE", "runtime lifecycle state is unsupported")
        if self.database_path != os.path.realpath(os.path.abspath(self.database_path)):
            raise RuntimeControlError("PATH_IDENTITY_MISMATCH", "database path must be canonical")
        if _canonical_control_endpoint(self.control_socket_path, instance_id) != self.control_socket_path:
            raise RuntimeControlError("PATH_IDENTITY_MISMATCH", "control endpoint must use canonical identity")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "RuntimeIdentity":
        if set(value) != _IDENTITY_KEYS:
            raise RuntimeControlError("INVALID_REGISTRATION", "runtime registration has unexpected fields")
        instance_id = _canonical_uuid(value["instance_id"], "instance_id")
        return cls(
            protocol_version=_positive_int(value["protocol_version"], "protocol_version"),
            instance_id=instance_id,
            owner_uid=_nonnegative_int(value["owner_uid"], "owner_uid"),
            pid=_positive_int(value["pid"], "pid"),
            process_start_ticks=_nonnegative_int(value["process_start_ticks"], "process_start_ticks"),
            boot_id=_bounded_text(value["boot_id"], "boot_id", maximum=128),
            database_path=os.path.realpath(os.path.abspath(_bounded_text(value["database_path"], "database_path", maximum=4096))),
            device=_nonnegative_int(value["device"], "device"),
            inode=_nonnegative_int(value["inode"], "inode"),
            generation=_nonnegative_int(value["generation"], "generation"),
            control_socket_path=_canonical_control_endpoint(value["control_socket_path"], instance_id),
            lifecycle_state=_bounded_text(value["lifecycle_state"], "lifecycle_state", maximum=64),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "instance_id": self.instance_id,
            "owner_uid": self.owner_uid,
            "pid": self.pid,
            "process_start_ticks": self.process_start_ticks,
            "boot_id": self.boot_id,
            "database_path": self.database_path,
            "device": self.device,
            "inode": self.inode,
            "generation": self.generation,
            "control_socket_path": self.control_socket_path,
            "lifecycle_state": self.lifecycle_state,
        }

    def with_state(self, state: str) -> "RuntimeIdentity":
        return replace(self, lifecycle_state=state)

    def with_generation(self, generation: int, *, state: str | None = None) -> "RuntimeIdentity":
        target = Path(self.database_path)
        try:
            info = target.stat()
        except OSError as exc:
            raise RuntimeControlError("DATABASE_IDENTITY_UNAVAILABLE", "database identity cannot be refreshed") from exc
        return replace(
            self,
            generation=_nonnegative_int(generation, "generation"),
            device=info.st_dev,
            inode=info.st_ino,
            lifecycle_state=self.lifecycle_state if state is None else state,
        )


class RuntimeRegistry:
    """Owner-private per-runtime registration store; never a liveness lease."""

    def __init__(
        self,
        runtime_root: str | os.PathLike[str],
        *,
        owner_uid: int | None = None,
        boot_id_provider: Callable[[], str] = current_boot_id,
        process_start_provider: Callable[[int], int | None] = process_start_ticks,
        endpoint_validator: Callable[[RuntimeIdentity], None] | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root).absolute()
        self.registry_root = self.runtime_root / "registry"
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        self._boot_id_provider = boot_id_provider
        self._process_start_provider = process_start_provider
        self._endpoint_validator = endpoint_validator
        _ensure_owner_private_directory(self.runtime_root)
        _ensure_owner_private_directory(self.registry_root)

    def _path(self, instance_id: str) -> Path:
        canonical = _canonical_uuid(instance_id, "instance_id")
        return self.registry_root / f"{canonical}.json"

    def _validate_socket_location(self, identity: RuntimeIdentity) -> None:
        if os.name == "nt" and _is_windows_runtime_pipe(identity.control_socket_path):
            expected = windows_runtime_pipe_name(identity.instance_id)
            if identity.control_socket_path.casefold() != expected.casefold():
                raise RuntimeControlError(
                    "PATH_IDENTITY_MISMATCH",
                    "runtime-control Named Pipe does not match the registered instance identity",
                )
            return
        root = os.path.realpath(str(self.runtime_root))
        socket_parent = os.path.realpath(os.path.dirname(identity.control_socket_path))
        try:
            common = os.path.commonpath([root, socket_parent])
        except ValueError as exc:
            raise RuntimeControlError("PATH_IDENTITY_MISMATCH", "control socket is outside the private runtime directory") from exc
        if common != root:
            raise RuntimeControlError("PATH_IDENTITY_MISMATCH", "control socket is outside the private runtime directory")

    def _validate_control_endpoint(self, identity: RuntimeIdentity) -> None:
        if self._endpoint_validator is not None:
            self._endpoint_validator(identity)
            return
        self._validate_socket_location(identity)

    def register(self, identity: RuntimeIdentity) -> RuntimeIdentity:
        if identity.owner_uid != self.owner_uid:
            raise RuntimeControlError("OWNER_UID_MISMATCH", "runtime registration Owner UID does not match this registry")
        self._validate_control_endpoint(identity)
        _atomic_json(self._path(identity.instance_id), identity.to_mapping())
        return identity

    update = register

    def load(self, instance_id: str) -> RuntimeIdentity:
        path = self._path(instance_id)
        try:
            if os.name == "nt" and path.exists():
                validate_owner_private_path(path, owner_sid=current_user_sid(), directory=False)
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeControlError("REGISTRATION_NOT_FOUND", "runtime registration was not found") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeControlError("INVALID_REGISTRATION", "runtime registration is unreadable") from exc
        if not isinstance(raw, dict):
            raise RuntimeControlError("INVALID_REGISTRATION", "runtime registration must be an object")
        return RuntimeIdentity.from_mapping(raw)

    def registrations(self, *, database_path: str | None = None, require_live: bool = False) -> list[RuntimeIdentity]:
        canonical_db = None if database_path is None else os.path.realpath(os.path.abspath(database_path))
        identities: list[RuntimeIdentity] = []
        for path in sorted(self.registry_root.glob("*.json")):
            identity = self.load(path.stem)
            if canonical_db is not None and identity.database_path != canonical_db:
                continue
            if require_live:
                self.validate_live(identity)
            identities.append(identity)
        return identities

    def validate_live(
        self,
        identity: RuntimeIdentity,
        *,
        peer_uid: int | None = None,
        peer_pid: int | None = None,
    ) -> RuntimeIdentity:
        if identity.owner_uid != self.owner_uid:
            raise RuntimeControlError("OWNER_UID_MISMATCH", "runtime registration Owner UID does not match this registry")
        if peer_uid is not None and peer_uid != identity.owner_uid:
            raise RuntimeControlError("PEER_UID_MISMATCH", "runtime-control peer UID does not match the registered Owner")
        if peer_pid is not None and peer_pid != identity.pid:
            raise RuntimeControlError("PEER_PROCESS_MISMATCH", "runtime-control peer PID does not match the registered process")
        if self._boot_id_provider() != identity.boot_id:
            raise RuntimeControlError("STALE_BOOT_IDENTITY", "runtime registration belongs to another boot")
        observed_start = self._process_start_provider(identity.pid)
        if observed_start is None or observed_start != identity.process_start_ticks:
            raise RuntimeControlError("STALE_PROCESS_IDENTITY", "runtime registration PID/start identity is stale")
        self._validate_control_endpoint(identity)
        return identity

    def remove_stale(self, instance_id: str, *, exclusion_released: bool) -> None:
        identity = self.load(instance_id)
        try:
            self.validate_live(identity)
        except RuntimeControlError as exc:
            if exc.code not in {"STALE_BOOT_IDENTITY", "STALE_PROCESS_IDENTITY"}:
                raise
            if not exclusion_released:
                raise RuntimeControlError(
                    "EXCLUSION_UNPROVEN",
                    "stale registry entry cannot be removed until database-handle exclusion is proven",
                ) from exc
            self._path(instance_id).unlink(missing_ok=True)
            return
        raise RuntimeControlError("RUNTIME_STILL_LIVE", "live runtime registration cannot be removed as stale")


@dataclass(frozen=True)
class RuntimeControlRequest:
    protocol_version: int
    request_id: str
    operation_id: str
    instance_id: str
    expected_generation: int
    command: str
    drain_deadline: float | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "RuntimeControlRequest":
        command_value = payload.get("command")
        if not isinstance(command_value, str) or command_value not in CONTROL_COMMANDS:
            raise RuntimeControlError("UNSUPPORTED_COMMAND", "runtime-control command is unsupported")
        allowed = set(_REQUEST_COMMON_KEYS)
        if command_value == "PREPARE_CLOSE":
            allowed.add("drain_deadline")
        if set(payload) != allowed:
            raise RuntimeControlError("INVALID_CONTROL_MESSAGE", "runtime-control request has unexpected or missing fields")
        version = _positive_int(payload["protocol_version"], "protocol_version")
        if version != PROTOCOL_VERSION:
            raise RuntimeControlError("UNSUPPORTED_PROTOCOL", "runtime-control protocol version is unsupported")
        deadline: float | None = None
        if command_value == "PREPARE_CLOSE":
            raw_deadline = payload["drain_deadline"]
            if isinstance(raw_deadline, bool) or not isinstance(raw_deadline, (int, float)):
                raise RuntimeControlError("INVALID_CONTROL_MESSAGE", "drain_deadline must be an absolute numeric timestamp")
            deadline = float(raw_deadline)
            if not math.isfinite(deadline) or deadline <= 0:
                raise RuntimeControlError("INVALID_CONTROL_MESSAGE", "drain_deadline must be a positive finite timestamp")
        return cls(
            protocol_version=version,
            request_id=_canonical_uuid(payload["request_id"], "request_id"),
            operation_id=_canonical_uuid(payload["operation_id"], "operation_id"),
            instance_id=_canonical_uuid(payload["instance_id"], "instance_id"),
            expected_generation=_nonnegative_int(payload["expected_generation"], "expected_generation"),
            command=command_value,
            drain_deadline=deadline,
        )

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "instance_id": self.instance_id,
            "expected_generation": self.expected_generation,
            "command": self.command,
        }
        if self.command == "PREPARE_CLOSE":
            payload["drain_deadline"] = self.drain_deadline
        return payload

    def semantic_fingerprint(self) -> str:
        payload = self.to_mapping()
        payload.pop("request_id", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_bytes(self) -> bytes:
        return _encode_mapping(self.to_mapping())


def _decode_mapping(data: bytes) -> dict[str, object]:
    if len(data) > MAX_CONTROL_MESSAGE_BYTES:
        raise RuntimeControlError("CONTROL_MESSAGE_TOO_LARGE", "runtime-control message exceeds 8 KiB")
    if not data.endswith(b"\n") or data.count(b"\n") != 1:
        raise RuntimeControlError("INVALID_CONTROL_MESSAGE", "runtime-control message must be one newline-delimited JSON object")
    try:
        text = data[:-1].decode("utf-8", errors="strict")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeControlError("INVALID_CONTROL_MESSAGE", "runtime-control message is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise RuntimeControlError("INVALID_CONTROL_MESSAGE", "runtime-control message must be a JSON object")
    return payload


def _encode_mapping(payload: Mapping[str, object]) -> bytes:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_CONTROL_MESSAGE_BYTES:
        raise RuntimeControlError("CONTROL_MESSAGE_TOO_LARGE", "runtime-control message exceeds 8 KiB")
    return encoded


def decode_request(data: bytes) -> RuntimeControlRequest:
    return RuntimeControlRequest.from_mapping(_decode_mapping(data))


def validate_response(payload: Mapping[str, object], request: RuntimeControlRequest | None = None) -> dict[str, object]:
    if set(payload) != _RESPONSE_KEYS:
        raise RuntimeControlError("INVALID_CONTROL_RESPONSE", "runtime-control response has unexpected fields")
    version = _positive_int(payload["protocol_version"], "protocol_version")
    if version != PROTOCOL_VERSION:
        raise RuntimeControlError("UNSUPPORTED_PROTOCOL", "runtime-control response protocol version is unsupported")
    normalized = {
        "protocol_version": version,
        "request_id": _canonical_uuid(payload["request_id"], "request_id"),
        "operation_id": _canonical_uuid(payload["operation_id"], "operation_id"),
        "instance_id": _canonical_uuid(payload["instance_id"], "instance_id"),
        "state": _bounded_text(payload["state"], "state", maximum=64),
        "actual_generation": _nonnegative_int(payload["actual_generation"], "actual_generation"),
        "status_code": _bounded_text(payload["status_code"], "status_code", maximum=96),
        "lease_open": payload["lease_open"],
    }
    if not isinstance(normalized["lease_open"], bool):
        raise RuntimeControlError("INVALID_CONTROL_RESPONSE", "lease_open must be a boolean")
    if request is not None:
        for field in ("request_id", "operation_id", "instance_id"):
            if normalized[field] != getattr(request, field):
                raise RuntimeControlError("CONTROL_RESPONSE_MISMATCH", f"runtime-control response {field} does not match request")
    return normalized


def decode_response(data: bytes, request: RuntimeControlRequest | None = None) -> dict[str, object]:
    return validate_response(_decode_mapping(data), request)


def make_response(
    request: RuntimeControlRequest,
    *,
    state: str,
    actual_generation: int,
    status_code: str,
    lease_open: bool,
) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request.request_id,
        "operation_id": request.operation_id,
        "instance_id": request.instance_id,
        "state": state,
        "actual_generation": actual_generation,
        "status_code": status_code,
        "lease_open": lease_open,
    }


class RuntimeLifecycleController:
    """Serialize one runtime's engine attach/close/reopen lifecycle."""

    def __init__(
        self,
        identity: RuntimeIdentity,
        *,
        barrier: BarrierAccess,
        registry: RuntimeRegistry,
        open_adapter: Callable[[int], Awaitable[None]],
        close_adapter: Callable[[], Awaitable[None]],
        lease_is_open: Callable[[], bool],
    ) -> None:
        self.identity = identity
        self._barrier = barrier
        self._registry = registry
        self._open_adapter = open_adapter
        self._close_adapter = close_adapter
        self._lease_is_open = lease_is_open
        self._lifecycle_mutex = asyncio.Lock()
        self._idempotent: dict[tuple[str, str], tuple[str, dict[str, object]]] = {}

    @property
    def admits_normal_requests(self) -> bool:
        if self.identity.lifecycle_state != "OPEN" or not self._lease_is_open():
            return False
        try:
            snapshot = self._barrier.read()
        except RuntimeControlError:
            return False
        return (
            snapshot.state == "RUNNING"
            and snapshot.generation == self.identity.generation
            and snapshot.device == self.identity.device
            and snapshot.inode == self.identity.inode
        )

    def _persist_state(self, state: str) -> None:
        self.identity = self.identity.with_state(state)
        self._registry.update(self.identity)

    def _persist_generation(self, generation: int, state: str) -> None:
        self.identity = self.identity.with_generation(generation, state=state)
        self._registry.update(self.identity)

    async def startup_open(self) -> str:
        async with self._lifecycle_mutex:
            with self._barrier.guard():
                snapshot = self._barrier.read_locked()
                if snapshot.state != "RUNNING" or snapshot.generation != self.identity.generation:
                    self._persist_state("PARKED")
                    return "PARKED"
                self._persist_state("OPENING")
                try:
                    await self._open_adapter(snapshot.generation)
                except Exception as exc:
                    self._persist_state("PARKED")
                    raise RuntimeControlError("OPEN_FAILED", "runtime adapter could not open") from exc
                if not self._lease_is_open():
                    self._persist_state("PARKED")
                    raise RuntimeControlError("OPEN_FAILED", "runtime adapter opened without a verified lease")
                self._persist_state("OPEN")
                return "OPEN"

    async def handle(self, request: RuntimeControlRequest) -> dict[str, object]:
        if request.instance_id != self.identity.instance_id:
            return make_response(
                request,
                state=self.identity.lifecycle_state,
                actual_generation=self.identity.generation,
                status_code="INSTANCE_MISMATCH",
                lease_open=self._lease_is_open(),
            )
        if request.command == "INSPECT":
            return make_response(
                request,
                state=self.identity.lifecycle_state,
                actual_generation=self.identity.generation,
                status_code="OK" if request.expected_generation == self.identity.generation else "STALE_GENERATION",
                lease_open=self._lease_is_open(),
            )

        key = (request.operation_id, request.command)
        fingerprint = request.semantic_fingerprint()
        cached = self._idempotent.get(key)
        if cached is not None:
            previous_fingerprint, response = cached
            if previous_fingerprint != fingerprint:
                return make_response(
                    request,
                    state=self.identity.lifecycle_state,
                    actual_generation=self.identity.generation,
                    status_code="IDEMPOTENCY_CONFLICT",
                    lease_open=self._lease_is_open(),
                )
            replay = dict(response)
            replay["request_id"] = request.request_id
            return replay

        if request.command == "PREPARE_CLOSE":
            response = await self._prepare_close(request)
        elif request.command == "ABORT":
            response = await self._abort(request)
        elif request.command == "RESUME":
            response = await self._resume(request)
        else:  # RuntimeControlRequest construction already rejects this.
            response = make_response(
                request,
                state=self.identity.lifecycle_state,
                actual_generation=self.identity.generation,
                status_code="UNSUPPORTED_COMMAND",
                lease_open=self._lease_is_open(),
            )
        self._idempotent[key] = (fingerprint, dict(response))
        return response

    async def _prepare_close(self, request: RuntimeControlRequest) -> dict[str, object]:
        async with self._lifecycle_mutex:
            if request.expected_generation != self.identity.generation:
                return make_response(
                    request,
                    state=self.identity.lifecycle_state,
                    actual_generation=self.identity.generation,
                    status_code="STALE_GENERATION",
                    lease_open=self._lease_is_open(),
                )
            with self._barrier.guard():
                snapshot = self._barrier.read_locked()
                if (
                    snapshot.state != "PREPARING"
                    or snapshot.operation_id != request.operation_id
                    or snapshot.original_generation != request.expected_generation
                ):
                    return make_response(
                        request,
                        state=self.identity.lifecycle_state,
                        actual_generation=self.identity.generation,
                        status_code="BARRIER_MISMATCH",
                        lease_open=self._lease_is_open(),
                    )
                self._persist_state("PREPARING")

            if not self._lease_is_open():
                self._persist_state("CLOSED")
                return make_response(
                    request,
                    state="CLOSED",
                    actual_generation=self.identity.generation,
                    status_code="OK",
                    lease_open=False,
                )

            assert request.drain_deadline is not None
            remaining = request.drain_deadline - time.time()
            if remaining <= 0:
                self._persist_state("CLOSE_FAILED")
                return make_response(
                    request,
                    state="CLOSE_FAILED",
                    actual_generation=self.identity.generation,
                    status_code="CLOSE_FAILED",
                    lease_open=self._lease_is_open(),
                )
            try:
                await asyncio.wait_for(self._close_adapter(), timeout=remaining)
            except (TimeoutError, asyncio.CancelledError):
                self._persist_state("CLOSE_FAILED")
                return make_response(
                    request,
                    state="CLOSE_FAILED",
                    actual_generation=self.identity.generation,
                    status_code="CLOSE_FAILED",
                    lease_open=self._lease_is_open(),
                )
            except Exception:
                self._persist_state("CLOSE_FAILED")
                return make_response(
                    request,
                    state="CLOSE_FAILED",
                    actual_generation=self.identity.generation,
                    status_code="CLOSE_FAILED",
                    lease_open=self._lease_is_open(),
                )
            if self._lease_is_open():
                self._persist_state("CLOSE_FAILED")
                return make_response(
                    request,
                    state="CLOSE_FAILED",
                    actual_generation=self.identity.generation,
                    status_code="CLOSE_FAILED",
                    lease_open=True,
                )
            self._persist_state("CLOSED")
            return make_response(
                request,
                state="CLOSED",
                actual_generation=self.identity.generation,
                status_code="OK",
                lease_open=False,
            )

    async def _abort(self, request: RuntimeControlRequest) -> dict[str, object]:
        async with self._lifecycle_mutex:
            if request.expected_generation != self.identity.generation:
                return make_response(
                    request,
                    state=self.identity.lifecycle_state,
                    actual_generation=self.identity.generation,
                    status_code="STALE_GENERATION",
                    lease_open=self._lease_is_open(),
                )
            with self._barrier.guard():
                snapshot = self._barrier.read_locked()
                if (
                    snapshot.state != "ABORTING"
                    or snapshot.operation_id != request.operation_id
                    or snapshot.original_generation != request.expected_generation
                    or snapshot.generation != request.expected_generation
                ):
                    return make_response(
                        request,
                        state=self.identity.lifecycle_state,
                        actual_generation=self.identity.generation,
                        status_code="BARRIER_MISMATCH",
                        lease_open=self._lease_is_open(),
                    )
                if self._lease_is_open() and self.identity.lifecycle_state == "OPEN":
                    return make_response(
                        request,
                        state="OPEN",
                        actual_generation=self.identity.generation,
                        status_code="OK",
                        lease_open=True,
                    )
                self._persist_state("REOPENING")
                try:
                    await self._open_adapter(request.expected_generation)
                except Exception:
                    self._persist_state("CLOSED")
                    return make_response(
                        request,
                        state="CLOSED",
                        actual_generation=self.identity.generation,
                        status_code="REOPEN_FAILED",
                        lease_open=self._lease_is_open(),
                    )
                if not self._lease_is_open():
                    self._persist_state("CLOSED")
                    return make_response(
                        request,
                        state="CLOSED",
                        actual_generation=self.identity.generation,
                        status_code="REOPEN_FAILED",
                        lease_open=False,
                    )
                self._persist_state("OPEN")
                return make_response(
                    request,
                    state="OPEN",
                    actual_generation=self.identity.generation,
                    status_code="OK",
                    lease_open=True,
                )

    async def _resume(self, request: RuntimeControlRequest) -> dict[str, object]:
        async with self._lifecycle_mutex:
            with self._barrier.guard():
                snapshot = self._barrier.read_locked()
                if (
                    snapshot.state not in {"ACTIVATING", "RUNNING"}
                    or snapshot.operation_id != request.operation_id
                    or snapshot.generation != request.expected_generation
                ):
                    return make_response(
                        request,
                        state=self.identity.lifecycle_state,
                        actual_generation=self.identity.generation,
                        status_code="BARRIER_MISMATCH",
                        lease_open=self._lease_is_open(),
                    )
                if self.identity.generation == request.expected_generation and self._lease_is_open():
                    self._persist_state("OPEN")
                    return make_response(
                        request,
                        state="READY",
                        actual_generation=self.identity.generation,
                        status_code="OK",
                        lease_open=True,
                    )
                if self._lease_is_open():
                    return make_response(
                        request,
                        state=self.identity.lifecycle_state,
                        actual_generation=self.identity.generation,
                        status_code="REOPEN_FAILED",
                        lease_open=True,
                    )
                self._persist_state("REOPENING")
                try:
                    await self._open_adapter(request.expected_generation)
                except Exception:
                    self._persist_state("CLOSED")
                    return make_response(
                        request,
                        state="CLOSED",
                        actual_generation=self.identity.generation,
                        status_code="REOPEN_FAILED",
                        lease_open=self._lease_is_open(),
                    )
                if not self._lease_is_open():
                    self._persist_state("CLOSED")
                    return make_response(
                        request,
                        state="CLOSED",
                        actual_generation=self.identity.generation,
                        status_code="REOPEN_FAILED",
                        lease_open=False,
                    )
                self._persist_generation(request.expected_generation, "OPEN")
                return make_response(
                    request,
                    state="READY",
                    actual_generation=self.identity.generation,
                    status_code="OK",
                    lease_open=True,
                )


def _peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeControlError("PEER_IDENTITY_UNAVAILABLE", "SO_PEERCRED is required for runtime control")
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


class RuntimeControlServer:
    """Private AF_UNIX control server for one MCP runtime; no memory operations."""

    def __init__(self, *, identity: RuntimeIdentity, registry: RuntimeRegistry, controller: RuntimeLifecycleController) -> None:
        self.identity = identity
        self.registry = registry
        self.controller = controller
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        path = Path(self.identity.control_socket_path)
        _ensure_owner_private_directory(path.parent)
        if path.exists() or path.is_symlink():
            raise RuntimeControlError("CONTROL_SOCKET_EXISTS", "runtime-control socket path already exists")
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_connection,
                path=str(path),
                limit=MAX_CONTROL_MESSAGE_BYTES + 1,
            )
            os.chmod(path, 0o600)
        except OSError as exc:
            raise RuntimeControlError("CONTROL_SOCKET_UNAVAILABLE", "runtime-control socket could not be created") from exc

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        path = Path(self.identity.control_socket_path)
        try:
            if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
                path.unlink()
        except OSError:
            pass

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw_socket = writer.get_extra_info("socket")
            if raw_socket is None:
                raise RuntimeControlError("PEER_IDENTITY_UNAVAILABLE", "runtime-control peer socket is unavailable")
            _peer_pid, peer_uid, _peer_gid = _peer_credentials(raw_socket)
            if peer_uid != self.identity.owner_uid:
                raise RuntimeControlError("PEER_UID_MISMATCH", "runtime-control caller is not the registered Owner UID")
            try:
                raw = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                raise RuntimeControlError("CONTROL_MESSAGE_TOO_LARGE", "runtime-control message exceeds 8 KiB") from exc
            request = decode_request(raw)
            response = await self.controller.handle(request)
            writer.write(_encode_mapping(response))
            await writer.drain()
        except RuntimeControlError:
            # Invalid unauthenticated/unparseable requests have no trustworthy
            # correlation identifiers; fail closed without reflecting payloads.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


class RuntimeControlClient:
    """Coordinator-side AF_UNIX client that authenticates the registered runtime peer."""

    def __init__(self, registry: RuntimeRegistry) -> None:
        self.registry = registry

    async def call(self, identity: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
        self.registry.validate_live(identity)
        if request.instance_id != identity.instance_id:
            raise RuntimeControlError("INSTANCE_MISMATCH", "runtime-control request targets another instance")
        try:
            reader, writer = await asyncio.open_unix_connection(
                identity.control_socket_path,
                limit=MAX_CONTROL_MESSAGE_BYTES + 1,
            )
        except OSError as exc:
            raise RuntimeControlError("CONTROL_SOCKET_UNAVAILABLE", "registered runtime-control socket is unavailable") from exc
        try:
            raw_socket = writer.get_extra_info("socket")
            if raw_socket is None:
                raise RuntimeControlError("PEER_IDENTITY_UNAVAILABLE", "runtime-control peer socket is unavailable")
            peer_pid, peer_uid, _peer_gid = _peer_credentials(raw_socket)
            self.registry.validate_live(identity, peer_uid=peer_uid, peer_pid=peer_pid)
            writer.write(request.to_bytes())
            await writer.drain()
            try:
                raw = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                raise RuntimeControlError("CONTROL_MESSAGE_TOO_LARGE", "runtime-control response exceeds 8 KiB") from exc
            if not raw:
                raise RuntimeControlError("CONTROL_RESPONSE_MISSING", "runtime-control peer closed without a response")
            return decode_response(raw, request)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


def _windows_pipe_modules() -> tuple[Any, Any, Any]:
    if os.name != "nt":
        raise RuntimeControlError("CONTROL_SOCKET_UNAVAILABLE", "Windows Named Pipes are unavailable on this platform")
    try:
        import pywintypes
        import win32file
        import win32pipe
    except ImportError as exc:  # pragma: no cover - native-Windows packaging guard
        raise RuntimeControlError(
            "CONTROL_SOCKET_UNAVAILABLE",
            "pywin32 Named Pipe primitives are unavailable",
        ) from exc
    return pywintypes, win32file, win32pipe


def _windows_close_handle(handle: Any) -> None:
    close = getattr(handle, "Close", None)
    if callable(close):
        close()


def _windows_read_line(handle: Any, *, deadline: float) -> bytes:
    pywintypes, win32file, _win32pipe = _windows_pipe_modules()
    data = bytearray()
    while len(data) <= MAX_CONTROL_MESSAGE_BYTES:
        try:
            _result, chunk = win32file.ReadFile(handle, min(1024, MAX_CONTROL_MESSAGE_BYTES + 1 - len(data)))
        except pywintypes.error as exc:
            if getattr(exc, "winerror", None) in {232, 536}:
                if time.monotonic() >= deadline:
                    raise RuntimeControlError(
                        "CONTROL_SOCKET_UNAVAILABLE",
                        "runtime-control Named Pipe response timed out",
                    ) from exc
                time.sleep(0.01)
                continue
            raise RuntimeControlError("CONTROL_RESPONSE_MISSING", "runtime-control peer closed without a response") from exc
        except Exception as exc:
            raise RuntimeControlError("CONTROL_RESPONSE_MISSING", "runtime-control peer closed without a response") from exc
        if not chunk:
            break
        data.extend(chunk)
        newline = data.find(b"\n")
        if newline >= 0:
            return bytes(data[: newline + 1])
    if len(data) > MAX_CONTROL_MESSAGE_BYTES:
        raise RuntimeControlError("CONTROL_MESSAGE_TOO_LARGE", "runtime-control message exceeds 8 KiB")
    if not data:
        raise RuntimeControlError("CONTROL_RESPONSE_MISSING", "runtime-control peer closed without a response")
    return bytes(data)


class WindowsRuntimeControlServer:
    """Owner-only Windows Named Pipe runtime-control server using the existing NDJSON protocol."""

    def __init__(
        self,
        *,
        identity: RuntimeIdentity,
        registry: RuntimeRegistry,
        controller: RuntimeLifecycleController,
        owner_sid: str | None = None,
    ) -> None:
        if not _is_windows_runtime_pipe(identity.control_socket_path):
            raise RuntimeControlError(
                "PATH_IDENTITY_MISMATCH",
                "Windows runtime-control requires an exact local Named Pipe endpoint",
            )
        self.identity = identity
        self.registry = registry
        self.controller = controller
        self.owner_sid = current_user_sid() if owner_sid is None else owner_sid
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown = threading.Event()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None

    def _validate_server_process(self) -> None:
        try:
            current = windows_process_identity(os.getpid())
        except WindowsSecurityError as exc:
            raise RuntimeControlError("PEER_IDENTITY_UNAVAILABLE", exc.safe_message) from exc
        if current.sid.casefold() != self.owner_sid.casefold():
            raise RuntimeControlError("OWNER_UID_MISMATCH", "runtime-control server process is not owned by the Windows Owner")
        if current.pid != self.identity.pid or current.creation_time_us != self.identity.process_start_ticks:
            raise RuntimeControlError(
                "STALE_PROCESS_IDENTITY",
                "runtime-control server process identity does not match the registration",
            )

    def _create_pipe(self) -> Any:
        _pywintypes, win32file, win32pipe = _windows_pipe_modules()
        security = owner_only_security_attributes(self.owner_sid)
        open_mode = win32pipe.PIPE_ACCESS_DUPLEX | getattr(win32file, "FILE_FLAG_FIRST_PIPE_INSTANCE", 0x00080000)
        pipe_mode = (
            win32pipe.PIPE_TYPE_BYTE
            | win32pipe.PIPE_READMODE_BYTE
            | win32pipe.PIPE_WAIT
            | getattr(win32pipe, "PIPE_REJECT_REMOTE_CLIENTS", 0x00000008)
        )
        try:
            handle = win32pipe.CreateNamedPipe(
                self.identity.control_socket_path,
                open_mode,
                pipe_mode,
                1,
                MAX_CONTROL_MESSAGE_BYTES + 1,
                MAX_CONTROL_MESSAGE_BYTES + 1,
                10_000,
                security,
            )
            validate_owner_only_kernel_object(handle, self.owner_sid)
            return handle
        except WindowsSecurityError as exc:
            raise RuntimeControlError("CONTROL_SOCKET_UNAVAILABLE", exc.safe_message) from exc
        except Exception as exc:
            raise RuntimeControlError(
                "CONTROL_SOCKET_UNAVAILABLE",
                "runtime-control Named Pipe could not be created exclusively",
            ) from exc

    async def start(self) -> None:
        if self._thread is not None:
            return
        self._validate_server_process()
        self._loop = asyncio.get_running_loop()
        self._shutdown.clear()
        self._ready.clear()
        self._startup_error = None
        thread = threading.Thread(
            target=self._serve,
            name="factlane-runtime-control-pipe",
            daemon=False,
        )
        self._thread = thread
        thread.start()
        ready = await asyncio.to_thread(self._ready.wait, 5.0)
        if not ready:
            await self.close()
            raise RuntimeControlError("CONTROL_SOCKET_UNAVAILABLE", "runtime-control Named Pipe did not become ready")
        if self._startup_error is not None:
            error = self._startup_error
            await self.close()
            if isinstance(error, RuntimeControlError):
                raise error
            raise RuntimeControlError("CONTROL_SOCKET_UNAVAILABLE", "runtime-control Named Pipe failed to start") from error

    def _serve(self) -> None:
        pywintypes, _win32file, win32pipe = _windows_pipe_modules()
        error_pipe_connected = getattr(pywintypes, "ERROR_PIPE_CONNECTED", 535)
        handle: Any | None = None
        try:
            handle = self._create_pipe()
            self._ready.set()
            while not self._shutdown.is_set():
                try:
                    win32pipe.SetNamedPipeHandleState(
                        handle,
                        win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                        None,
                        None,
                    )
                    try:
                        win32pipe.ConnectNamedPipe(handle, None)
                    except pywintypes.error as exc:
                        if getattr(exc, "winerror", None) != error_pipe_connected:
                            if self._shutdown.is_set():
                                return
                            raise
                    if self._shutdown.is_set():
                        return
                    win32pipe.SetNamedPipeHandleState(
                        handle,
                        win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_NOWAIT,
                        None,
                        None,
                    )
                    self._handle_pipe(handle, win32pipe)
                finally:
                    try:
                        win32pipe.DisconnectNamedPipe(handle)
                    except Exception:
                        pass
        except BaseException as exc:
            if not self._ready.is_set():
                self._startup_error = exc
                self._ready.set()
            elif not self._shutdown.is_set():
                self._startup_error = exc
        finally:
            if handle is not None:
                try:
                    win32pipe.DisconnectNamedPipe(handle)
                except Exception:
                    pass
                _windows_close_handle(handle)

    def _handle_pipe(self, handle: Any, win32pipe: Any) -> None:
        try:
            validate_owner_only_kernel_object(handle, self.owner_sid)
            peer_pid = int(win32pipe.GetNamedPipeClientProcessId(handle))
            require_process_identity(peer_pid, owner_sid=self.owner_sid)
            raw = _windows_read_line(handle, deadline=time.monotonic() + 10.0)
            request = decode_request(raw)
            loop = self._loop
            if loop is None:
                raise RuntimeControlError("CONTROL_SOCKET_UNAVAILABLE", "runtime-control event loop is unavailable")
            future = asyncio.run_coroutine_threadsafe(self.controller.handle(request), loop)
            response = future.result()
            _pywintypes, win32file, _win32pipe = _windows_pipe_modules()
            try:
                win32file.WriteFile(handle, _encode_mapping(response))
                win32file.FlushFileBuffers(handle)
            except Exception:
                # A peer can disappear after an authenticated request but before
                # consuming the response.  That is a per-connection failure;
                # it must not tear down the persistent first pipe instance.
                return
        except (RuntimeControlError, WindowsSecurityError):
            # Invalid unauthenticated/unparseable requests have no trustworthy
            # correlation identifiers; fail closed without reflecting payloads.
            return

    async def close(self) -> None:
        thread = self._thread
        self._thread = None
        self._shutdown.set()
        if thread is None:
            return
        await asyncio.to_thread(self._wake)
        await asyncio.to_thread(thread.join, 5.0)
        if thread.is_alive():
            raise RuntimeControlError("CONTROL_SOCKET_UNAVAILABLE", "runtime-control Named Pipe did not stop")

    def _wake(self) -> None:
        try:
            _pywintypes, win32file, win32pipe = _windows_pipe_modules()
            win32pipe.WaitNamedPipe(self.identity.control_socket_path, 200)
            handle = win32file.CreateFile(
                self.identity.control_socket_path,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
        except Exception:
            return
        _windows_close_handle(handle)


class WindowsRuntimeControlClient:
    """Coordinator-side Windows Named Pipe client authenticating the registered runtime process."""

    def __init__(self, registry: RuntimeRegistry, *, owner_sid: str | None = None, timeout_seconds: float = 10.0) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or timeout_seconds > 10.0
        ):
            raise ValueError("timeout_seconds must be greater than zero and at most 10 seconds")
        self.registry = registry
        self.owner_sid = current_user_sid() if owner_sid is None else owner_sid
        self.timeout_seconds = float(timeout_seconds)

    async def call(self, identity: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
        self.registry.validate_live(identity)
        if request.instance_id != identity.instance_id:
            raise RuntimeControlError("INSTANCE_MISMATCH", "runtime-control request targets another instance")
        return await asyncio.to_thread(self._call_blocking, identity, request)

    def _open_pipe(self, pywintypes: Any, win32file: Any, win32pipe: Any, pipe_name: str) -> Any:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeControlError(
                    "CONTROL_SOCKET_UNAVAILABLE",
                    "registered runtime-control Named Pipe did not become available before timeout",
                )
            try:
                win32pipe.WaitNamedPipe(pipe_name, max(1, int(remaining * 1000)))
                return win32file.CreateFile(
                    pipe_name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
            except pywintypes.error as exc:
                if getattr(exc, "winerror", None) not in {2, 231}:
                    raise
                time.sleep(min(0.01, max(0.0, remaining)))

    def _call_blocking(self, identity: RuntimeIdentity, request: RuntimeControlRequest) -> dict[str, object]:
        if not _is_windows_runtime_pipe(identity.control_socket_path):
            raise RuntimeControlError(
                "PATH_IDENTITY_MISMATCH",
                "Windows runtime-control requires an exact local Named Pipe endpoint",
            )
        pywintypes, win32file, win32pipe = _windows_pipe_modules()
        handle: Any | None = None
        try:
            handle = self._open_pipe(pywintypes, win32file, win32pipe, identity.control_socket_path)
            try:
                validate_owner_only_kernel_object(handle, self.owner_sid)
                server_pid = int(win32pipe.GetNamedPipeServerProcessId(handle))
                if server_pid != identity.pid:
                    raise RuntimeControlError(
                        "PEER_PROCESS_MISMATCH",
                        "runtime-control Named Pipe is owned by another process",
                    )
                require_process_identity(
                    server_pid,
                    owner_sid=self.owner_sid,
                    creation_time_us=identity.process_start_ticks,
                )
            except WindowsSecurityError as exc:
                code = "PEER_UID_MISMATCH" if exc.code == "WINDOWS_PROCESS_SID_MISMATCH" else "PEER_IDENTITY_UNAVAILABLE"
                if exc.code == "WINDOWS_PROCESS_IDENTITY_MISMATCH":
                    code = "STALE_PROCESS_IDENTITY"
                raise RuntimeControlError(code, exc.safe_message) from exc
            win32pipe.SetNamedPipeHandleState(
                handle,
                win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_NOWAIT,
                None,
                None,
            )
            win32file.WriteFile(handle, request.to_bytes())
            raw = _windows_read_line(handle, deadline=time.monotonic() + self.timeout_seconds)
            return decode_response(raw, request)
        except RuntimeControlError:
            raise
        except Exception as exc:
            raise RuntimeControlError(
                "CONTROL_SOCKET_UNAVAILABLE",
                "registered runtime-control Named Pipe is unavailable",
            ) from exc
        finally:
            if handle is not None:
                _windows_close_handle(handle)
