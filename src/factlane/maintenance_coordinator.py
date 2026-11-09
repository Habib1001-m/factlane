from __future__ import annotations

import asyncio
import errno
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .runtime_control import (
    PROTOCOL_VERSION,
    BarrierSnapshot,
    RuntimeControlError,
    RuntimeControlRequest,
    RuntimeIdentity,
    RuntimeRegistry,
    validate_response,
)
from .windows_maintenance import WindowsBarrierGuard, WindowsMaintenanceError
from .windows_security import (
    WindowsSecurityError,
    current_user_sid,
    ensure_owner_private_directory,
    owner_only_security_attributes,
    validate_owner_only_kernel_object,
    validate_owner_private_path,
    validate_windows_path_no_reparse,
)


_RUNTIME_RESUME_TIMEOUT_SECONDS = 5.0


class MaintenanceCoordinatorError(RuntimeControlError):
    """Fail-closed maintenance coordination error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


def _canonical_uuid(value: str, field: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise MaintenanceCoordinatorError("INVALID_OPERATION_ID", f"{field} must be a UUID") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise MaintenanceCoordinatorError("INVALID_OPERATION_ID", f"{field} must use canonical UUID form")
    return canonical


def _ensure_private_directory(path: Path) -> None:
    if os.name == "nt":
        try:
            ensure_owner_private_directory(path, owner_sid=current_user_sid())
        except WindowsSecurityError as exc:
            raise MaintenanceCoordinatorError(
                "BARRIER_UNAVAILABLE",
                "maintenance state directory cannot be protected",
            ) from exc
        return
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise MaintenanceCoordinatorError("BARRIER_UNAVAILABLE", "maintenance state directory cannot be protected") from exc


def _open_windows_private_file(path: Path, *, owner_sid: str) -> int:
    try:
        validate_windows_path_no_reparse(path)
        import msvcrt
        import win32api
        import win32con
        import win32file

        handle = None
        raw_handle: int | None = None
        try:
            handle = win32file.CreateFile(
                str(path),
                win32con.GENERIC_WRITE,
                0,
                owner_only_security_attributes(owner_sid),
                win32con.CREATE_NEW,
                win32con.FILE_ATTRIBUTE_NORMAL,
                None,
            )
            validate_owner_only_kernel_object(handle, owner_sid)
            raw_handle = int(handle.Detach())
            handle = None
            fd = msvcrt.open_osfhandle(raw_handle, os.O_WRONLY | getattr(os, "O_BINARY", 0))
            raw_handle = None
            return fd
        finally:
            if handle is not None:
                handle.Close()
            if raw_handle is not None:
                try:
                    win32api.CloseHandle(raw_handle)
                except Exception:
                    pass
    except MaintenanceCoordinatorError:
        raise
    except (ImportError, OSError, WindowsSecurityError, ValueError) as exc:
        raise MaintenanceCoordinatorError(
            "BARRIER_UNAVAILABLE",
            "maintenance barrier state could not be persisted",
        ) from exc
    except Exception as exc:
        raise MaintenanceCoordinatorError(
            "BARRIER_UNAVAILABLE",
            "maintenance barrier state could not be persisted",
        ) from exc


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    _ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    owner_sid: str | None = None
    try:
        if os.name == "nt":
            owner_sid = current_user_sid()
            validate_windows_path_no_reparse(path)
            if path.exists():
                validate_owner_private_path(path, owner_sid=owner_sid, directory=False)
            fd = _open_windows_private_file(temporary, owner_sid=owner_sid)
        else:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if owner_sid is not None:
                validate_owner_private_path(temporary, owner_sid=owner_sid, directory=False)
            os.replace(temporary, path)
            if owner_sid is not None:
                validate_owner_private_path(path, owner_sid=owner_sid, directory=False)
            else:
                os.chmod(path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
    except MaintenanceCoordinatorError:
        raise
    except (OSError, WindowsSecurityError) as exc:
        raise MaintenanceCoordinatorError("BARRIER_UNAVAILABLE", "maintenance barrier state could not be persisted") from exc


class MaintenanceBarrierStore:
    """Durable same-host barrier and generation CAS for one canonical database."""

    def __init__(self, state_root: str | os.PathLike[str], database_path: str) -> None:
        self.state_root = Path(state_root).absolute()
        self.state_path = self.state_root / "barrier.json"
        self.lock_path = self.state_root / "barrier.lock"
        self.database_path = os.path.realpath(os.path.abspath(database_path))
        _ensure_private_directory(self.state_root)

    @contextmanager
    def guard(self) -> Iterator[None]:
        if os.name == "nt":
            try:
                with WindowsBarrierGuard.acquire(self.lock_path) as guard:
                    guard.revalidate()
                    yield
                return
            except WindowsMaintenanceError as exc:
                if exc.code == "BARRIER_BUSY":
                    raise MaintenanceCoordinatorError(
                        "BARRIER_BUSY",
                        "maintenance barrier guard is currently owned by another lifecycle operation",
                    ) from exc
                raise MaintenanceCoordinatorError(
                    "BARRIER_UNAVAILABLE",
                    "maintenance barrier guard is unavailable",
                ) from exc
        try:
            import fcntl
        except ImportError as exc:
            raise MaintenanceCoordinatorError("BARRIER_UNAVAILABLE", "maintenance barrier requires POSIX flock") from exc
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(fd, "r+b", buffering=0)
        locked = False
        try:
            os.chmod(self.lock_path, 0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise MaintenanceCoordinatorError(
                        "BARRIER_BUSY",
                        "maintenance barrier guard is currently owned by another lifecycle operation",
                    ) from exc
                raise
            yield
        finally:
            try:
                if locked:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _database_identity(self) -> tuple[int, int]:
        try:
            info = os.stat(self.database_path)
        except OSError as exc:
            raise MaintenanceCoordinatorError("DATABASE_IDENTITY_UNAVAILABLE", "maintenance database identity is unavailable") from exc
        return info.st_dev, info.st_ino

    def initialize(self, *, generation: int) -> BarrierSnapshot:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise MaintenanceCoordinatorError("INVALID_GENERATION", "maintenance generation must be non-negative")
        with self.guard():
            if self.state_path.exists():
                existing = self.read_locked()
                if existing.database_path != self.database_path:
                    raise MaintenanceCoordinatorError("DATABASE_IDENTITY_MISMATCH", "maintenance state belongs to another database")
                return existing
            device, inode = self._database_identity()
            snapshot = BarrierSnapshot(
                state="RUNNING",
                operation_id=None,
                generation=generation,
                original_generation=None,
                intent_revision=0,
                database_path=self.database_path,
                device=device,
                inode=inode,
            )
            self._write_locked(snapshot)
            return snapshot

    def read_locked(self) -> BarrierSnapshot:
        if os.name == "nt" and self.state_path.exists():
            try:
                validate_owner_private_path(self.state_path, owner_sid=current_user_sid(), directory=False)
            except WindowsSecurityError as exc:
                raise MaintenanceCoordinatorError(
                    "INVALID_BARRIER_STATE",
                    "maintenance barrier state is not owner-private",
                ) from exc
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise MaintenanceCoordinatorError("BARRIER_UNINITIALIZED", "maintenance barrier is not initialized") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise MaintenanceCoordinatorError("INVALID_BARRIER_STATE", "maintenance barrier state is unreadable") from exc
        if not isinstance(raw, dict):
            raise MaintenanceCoordinatorError("INVALID_BARRIER_STATE", "maintenance barrier state must be an object")
        try:
            snapshot = BarrierSnapshot.from_mapping(raw)
        except RuntimeControlError as exc:
            raise MaintenanceCoordinatorError("INVALID_BARRIER_STATE", exc.safe_message) from exc
        if snapshot.database_path != self.database_path:
            raise MaintenanceCoordinatorError("DATABASE_IDENTITY_MISMATCH", "maintenance barrier belongs to another database")
        return snapshot

    def read(self) -> BarrierSnapshot:
        with self.guard():
            return self.read_locked()

    def _write_locked(self, snapshot: BarrierSnapshot) -> None:
        _atomic_json(self.state_path, snapshot.to_mapping())

    def begin_prepare(self, operation_id: str, *, expected_generation: int) -> BarrierSnapshot:
        operation_id = _canonical_uuid(operation_id, "operation_id")
        with self.guard():
            current = self.read_locked()
            if current.state == "PREPARING" and current.operation_id == operation_id:
                if current.original_generation != expected_generation:
                    raise MaintenanceCoordinatorError("STALE_GENERATION", "retry does not match the preparing generation")
                return current
            if current.state != "RUNNING":
                raise MaintenanceCoordinatorError("MAINTENANCE_BUSY", "another maintenance operation owns the database barrier")
            if current.generation != expected_generation:
                raise MaintenanceCoordinatorError("STALE_GENERATION", "maintenance generation changed before prepare")
            prepared = BarrierSnapshot(
                state="PREPARING",
                operation_id=operation_id,
                generation=current.generation,
                original_generation=current.generation,
                intent_revision=current.intent_revision + 1,
                database_path=current.database_path,
                device=current.device,
                inode=current.inode,
            )
            self._write_locked(prepared)
            return prepared

    def publish_abort(self, operation_id: str) -> BarrierSnapshot:
        operation_id = _canonical_uuid(operation_id, "operation_id")
        with self.guard():
            current = self.read_locked()
            if current.operation_id != operation_id:
                raise MaintenanceCoordinatorError("OPERATION_MISMATCH", "abort does not own the maintenance barrier")
            if current.state == "ABORTING":
                return current
            if current.state != "PREPARING" or current.original_generation is None:
                raise MaintenanceCoordinatorError("INVALID_BARRIER_TRANSITION", "maintenance barrier cannot enter abort")
            aborting = BarrierSnapshot(
                state="ABORTING",
                operation_id=operation_id,
                generation=current.original_generation,
                original_generation=current.original_generation,
                intent_revision=current.intent_revision,
                database_path=current.database_path,
                device=current.device,
                inode=current.inode,
            )
            self._write_locked(aborting)
            return aborting

    def publish_running(self, operation_id: str, *, generation: int) -> BarrierSnapshot:
        operation_id = _canonical_uuid(operation_id, "operation_id")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise MaintenanceCoordinatorError("INVALID_GENERATION", "maintenance generation must be non-negative")
        with self.guard():
            current = self.read_locked()
            if current.state == "RUNNING" and current.operation_id == operation_id and current.generation == generation:
                return current
            if current.operation_id != operation_id or current.state not in {"ACTIVATING", "ABORTING"}:
                raise MaintenanceCoordinatorError("OPERATION_MISMATCH", "running publication does not own the maintenance barrier")
            if current.original_generation is None:
                raise MaintenanceCoordinatorError("INVALID_BARRIER_STATE", "maintenance barrier lost its original generation")
            if current.state == "ABORTING" and generation != current.original_generation:
                raise MaintenanceCoordinatorError("STALE_GENERATION", "abort may only restore the original generation")
            if current.state == "ACTIVATING" and generation != current.generation:
                raise MaintenanceCoordinatorError("STALE_GENERATION", "running publication must match the activating generation")
            device, inode = self._database_identity()
            if current.state == "ACTIVATING" and (device, inode) != (current.device, current.inode):
                raise MaintenanceCoordinatorError(
                    "DATABASE_IDENTITY_MISMATCH",
                    "active database identity changed after activation was published",
                )
            running = BarrierSnapshot(
                state="RUNNING",
                operation_id=operation_id,
                generation=generation,
                original_generation=current.original_generation,
                intent_revision=current.intent_revision,
                database_path=current.database_path,
                device=device,
                inode=inode,
            )
            self._write_locked(running)
            return running

    def begin_activation(self, operation_id: str, *, generation: int) -> BarrierSnapshot:
        operation_id = _canonical_uuid(operation_id, "operation_id")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise MaintenanceCoordinatorError("INVALID_GENERATION", "maintenance generation must be non-negative")
        with self.guard():
            current = self.read_locked()
            if current.state == "ACTIVATING" and current.operation_id == operation_id:
                if current.generation != generation:
                    raise MaintenanceCoordinatorError("STALE_GENERATION", "activation retry generation does not match")
                device, inode = self._database_identity()
                if (device, inode) != (current.device, current.inode):
                    raise MaintenanceCoordinatorError(
                        "DATABASE_IDENTITY_MISMATCH",
                        "activating database identity changed",
                    )
                return current
            if current.state != "PREPARING" or current.operation_id != operation_id:
                raise MaintenanceCoordinatorError("OPERATION_MISMATCH", "activation does not own the preparing barrier")
            if current.original_generation is None or generation <= current.original_generation:
                raise MaintenanceCoordinatorError("INVALID_GENERATION", "activated generation must advance")
            device, inode = self._database_identity()
            activating = BarrierSnapshot(
                state="ACTIVATING",
                operation_id=operation_id,
                generation=generation,
                original_generation=current.original_generation,
                intent_revision=current.intent_revision,
                database_path=current.database_path,
                device=device,
                inode=inode,
            )
            self._write_locked(activating)
            return activating

    def mark_recovery_required(self, operation_id: str) -> BarrierSnapshot:
        operation_id = _canonical_uuid(operation_id, "operation_id")
        with self.guard():
            current = self.read_locked()
            if current.operation_id != operation_id or current.state not in {
                "PREPARING",
                "ABORTING",
                "ACTIVATING",
                "RECOVERY_REQUIRED",
            }:
                raise MaintenanceCoordinatorError("OPERATION_MISMATCH", "recovery publication does not own the maintenance barrier")
            if current.state == "RECOVERY_REQUIRED":
                return current
            recovery = BarrierSnapshot(
                state="RECOVERY_REQUIRED",
                operation_id=operation_id,
                generation=current.generation,
                original_generation=current.original_generation,
                intent_revision=current.intent_revision,
                database_path=current.database_path,
                device=current.device,
                inode=current.inode,
            )
            self._write_locked(recovery)
            return recovery

    def rearm_recovery(
        self,
        operation_id: str,
        *,
        original_generation: int,
        intent_revision: int,
    ) -> BarrierSnapshot:
        operation_id = _canonical_uuid(operation_id, "operation_id")
        if isinstance(original_generation, bool) or not isinstance(original_generation, int) or original_generation < 0:
            raise MaintenanceCoordinatorError("INVALID_GENERATION", "original generation must be non-negative")
        if isinstance(intent_revision, bool) or not isinstance(intent_revision, int) or intent_revision < 0:
            raise MaintenanceCoordinatorError("INVALID_BARRIER_STATE", "maintenance intent revision is invalid")
        with self.guard():
            current = self.read_locked()
            if current.state == "PREPARING" and current.operation_id == operation_id:
                if current.original_generation != original_generation or current.intent_revision != intent_revision:
                    raise MaintenanceCoordinatorError(
                        "ACTIVATION_IDENTITY_MISMATCH",
                        "rearmed maintenance identity does not match prepared state",
                    )
                return current
            if current.state != "RECOVERY_REQUIRED" or current.operation_id != operation_id:
                raise MaintenanceCoordinatorError(
                    "INVALID_BARRIER_TRANSITION",
                    "only the exact recovery-required operation may re-enter activation",
                )
            if current.original_generation != original_generation or current.intent_revision != intent_revision:
                raise MaintenanceCoordinatorError(
                    "ACTIVATION_IDENTITY_MISMATCH",
                    "recovery-required barrier does not match prepared maintenance identity",
                )
            device, inode = self._database_identity()
            preparing = BarrierSnapshot(
                state="PREPARING",
                operation_id=operation_id,
                generation=current.generation,
                original_generation=current.original_generation,
                intent_revision=current.intent_revision,
                database_path=current.database_path,
                device=device,
                inode=inode,
            )
            self._write_locked(preparing)
            return preparing


@dataclass(frozen=True)
class PreparedMaintenance:
    operation_id: str
    original_generation: int
    intent_revision: int
    initially_open_instance_ids: tuple[str, ...]
    registrations: tuple[RuntimeIdentity, ...]

    def initially_open(self) -> tuple[RuntimeIdentity, ...]:
        wanted = set(self.initially_open_instance_ids)
        return tuple(identity for identity in self.registrations if identity.instance_id in wanted)


@dataclass(frozen=True)
class ResumeReport:
    ready_instance_ids: tuple[str, ...]
    failures: dict[str, str]
    participant_failures: dict[str, str] = field(default_factory=dict)


RuntimeCaller = Callable[[RuntimeIdentity, RuntimeControlRequest], Awaitable[dict[str, object]]]


class MaintenanceCoordinator:
    """Same-host process coordination up to, but not including, database mutation."""

    def __init__(
        self,
        *,
        barrier: MaintenanceBarrierStore,
        registry: RuntimeRegistry,
        call_runtime: RuntimeCaller,
        exclusion_check: Callable[[str], bool],
        max_concurrency: int = 8,
    ) -> None:
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        self.barrier = barrier
        self.registry = registry
        self._call_runtime = call_runtime
        self._exclusion_check = exclusion_check
        self._max_concurrency = max_concurrency

    @staticmethod
    def _request_id(operation_id: str, instance_id: str, command: str) -> str:
        return str(uuid.uuid5(uuid.UUID(operation_id), f"{instance_id}:{command}"))

    def _request(
        self,
        identity: RuntimeIdentity,
        *,
        operation_id: str,
        command: str,
        generation: int,
        drain_deadline: float | None = None,
    ) -> RuntimeControlRequest:
        payload: dict[str, object] = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": self._request_id(operation_id, identity.instance_id, command),
            "operation_id": operation_id,
            "instance_id": identity.instance_id,
            "expected_generation": generation,
            "command": command,
        }
        if command == "PREPARE_CLOSE":
            payload["drain_deadline"] = drain_deadline
        return RuntimeControlRequest.from_mapping(payload)

    async def _bounded_calls(
        self,
        calls: list[tuple[RuntimeIdentity, RuntimeControlRequest]],
        *,
        deadline: float | None = None,
    ) -> dict[str, dict[str, object] | BaseException]:
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def one(identity: RuntimeIdentity, request: RuntimeControlRequest) -> tuple[str, dict[str, object] | BaseException]:
            async with semaphore:
                try:
                    response = await self._call_runtime(identity, request)
                    return identity.instance_id, validate_response(response, request)
                except Exception as exc:  # preserve ordinary per-runtime failure without swallowing cancellation
                    return identity.instance_id, exc

        work = asyncio.gather(*(one(identity, request) for identity, request in calls))
        if deadline is not None:
            remaining = deadline - time.time()
            if remaining <= 0:
                work.cancel()
                raise MaintenanceCoordinatorError("CLIENT_DRAIN_TIMEOUT", "runtime-control drain deadline expired")
            try:
                pairs = await asyncio.wait_for(work, timeout=remaining)
            except TimeoutError as exc:
                raise MaintenanceCoordinatorError("CLIENT_DRAIN_TIMEOUT", "runtime-control drain deadline expired") from exc
        else:
            pairs = await work
        return dict(pairs)

    async def _ordered_barrier_calls(
        self,
        calls: list[tuple[RuntimeIdentity, RuntimeControlRequest]],
        *,
        deadline: float | None = None,
        deadline_error_code: str = "CLIENT_DRAIN_TIMEOUT",
        deadline_error_message: str = "runtime-control drain deadline expired",
    ) -> dict[str, dict[str, object] | BaseException]:
        """Dispatch shared-barrier lifecycle commands without self-contention.

        Runtime lifecycle handlers deliberately use a non-blocking filesystem
        barrier guard.  Concurrent PREPARE_CLOSE/ABORT/RESUME calls from one
        coordinator would therefore contend with each other even though they
        belong to the same operation.  Keep those calls ordered while leaving
        read-only INSPECT fan-out bounded/concurrent.  A different coordinator
        still observes the non-blocking guard and fails closed.

        ``deadline`` is one absolute wall-clock deadline for the whole ordered
        sequence; it is never reset per runtime.
        """

        results: dict[str, dict[str, object] | BaseException] = {}
        for identity, request in calls:
            try:
                if deadline is None:
                    response = await self._call_runtime(identity, request)
                else:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise MaintenanceCoordinatorError(
                            deadline_error_code,
                            deadline_error_message,
                        )
                    try:
                        response = await asyncio.wait_for(
                            self._call_runtime(identity, request),
                            timeout=remaining,
                        )
                    except TimeoutError as exc:
                        raise MaintenanceCoordinatorError(
                            deadline_error_code,
                            deadline_error_message,
                        ) from exc
                results[identity.instance_id] = validate_response(response, request)
            except MaintenanceCoordinatorError as exc:
                if exc.code == deadline_error_code:
                    raise
                results[identity.instance_id] = exc
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                results[identity.instance_id] = exc
        return results

    def _live_registrations(self) -> tuple[RuntimeIdentity, ...]:
        try:
            registrations = self.registry.registrations(database_path=self.barrier.database_path, require_live=True)
        except RuntimeControlError as exc:
            raise MaintenanceCoordinatorError("REGISTRY_IDENTITY_INVALID", exc.safe_message) from exc
        current = self.barrier.read()
        for identity in registrations:
            if identity.generation != current.generation:
                raise MaintenanceCoordinatorError("REGISTRY_IDENTITY_INVALID", "registered runtime generation is stale")
            if identity.device != current.device or identity.inode != current.inode:
                raise MaintenanceCoordinatorError("REGISTRY_IDENTITY_INVALID", "registered runtime database inode is stale")
        return tuple(registrations)

    async def prepare_close(self, operation_id: str, *, drain_deadline: float) -> PreparedMaintenance:
        operation_id = _canonical_uuid(operation_id, "operation_id")
        registrations = self._live_registrations()
        running = self.barrier.read()
        prepared_barrier = self.barrier.begin_prepare(operation_id, expected_generation=running.generation)

        inspect_calls = [
            (
                identity,
                self._request(
                    identity,
                    operation_id=operation_id,
                    command="INSPECT",
                    generation=prepared_barrier.original_generation or prepared_barrier.generation,
                ),
            )
            for identity in registrations
        ]
        fallback_open = tuple(
            identity
            for identity in registrations
            if identity.lifecycle_state in {"OPEN", "OPENING", "PREPARING", "CLOSE_FAILED", "REOPENING"}
        )
        fallback = PreparedMaintenance(
            operation_id=operation_id,
            original_generation=prepared_barrier.original_generation or prepared_barrier.generation,
            intent_revision=prepared_barrier.intent_revision,
            initially_open_instance_ids=tuple(identity.instance_id for identity in fallback_open),
            registrations=registrations,
        )
        try:
            inspect_results = await self._bounded_calls(inspect_calls, deadline=drain_deadline)
        except MaintenanceCoordinatorError as exc:
            if exc.code == "CLIENT_DRAIN_TIMEOUT":
                await self._abort_after_prepare_failure(fallback, deadline=drain_deadline)
            raise
        inspect_failures = {
            instance_id: "UNREACHABLE" if isinstance(result, BaseException) else str(result["status_code"])
            for instance_id, result in inspect_results.items()
            if isinstance(result, BaseException) or result["status_code"] != "OK"
        }
        if inspect_failures:
            await self._abort_after_prepare_failure(fallback)
            raise MaintenanceCoordinatorError("CLIENT_INSPECTION_FAILED", "one or more registered runtimes could not be inspected")

        initially_open = tuple(
            identity
            for identity in registrations
            if bool(inspect_results[identity.instance_id]["lease_open"])
            or str(inspect_results[identity.instance_id]["state"]) in {"OPEN", "OPENING", "PREPARING"}
        )
        prepared = PreparedMaintenance(
            operation_id=operation_id,
            original_generation=prepared_barrier.original_generation or prepared_barrier.generation,
            intent_revision=prepared_barrier.intent_revision,
            initially_open_instance_ids=tuple(identity.instance_id for identity in initially_open),
            registrations=registrations,
        )
        close_calls = [
            (
                identity,
                self._request(
                    identity,
                    operation_id=operation_id,
                    command="PREPARE_CLOSE",
                    generation=prepared.original_generation,
                    drain_deadline=drain_deadline,
                ),
            )
            for identity in initially_open
        ]
        try:
            close_results = (
                await self._ordered_barrier_calls(close_calls, deadline=drain_deadline)
                if close_calls
                else {}
            )
        except MaintenanceCoordinatorError as exc:
            if exc.code == "CLIENT_DRAIN_TIMEOUT":
                await self._abort_after_prepare_failure(prepared, deadline=drain_deadline)
            raise
        close_failures = {
            instance_id: "UNREACHABLE" if isinstance(result, BaseException) else str(result["status_code"])
            for instance_id, result in close_results.items()
            if isinstance(result, BaseException)
            or result["status_code"] != "OK"
            or result["state"] != "CLOSED"
            or bool(result["lease_open"])
        }
        if close_failures:
            await self._abort_after_prepare_failure(prepared)
            raise MaintenanceCoordinatorError("CLIENT_CLOSE_FAILED", "one or more runtimes did not close cleanly")

        try:
            excluded = bool(self._exclusion_check(self.barrier.database_path))
        except Exception as exc:
            await self._abort_after_prepare_failure(prepared)
            raise MaintenanceCoordinatorError("EXCLUSION_UNPROVEN", "database-handle exclusion could not be proven") from exc
        if not excluded:
            await self._abort_after_prepare_failure(prepared)
            raise MaintenanceCoordinatorError("EXCLUSION_UNPROVEN", "database-handle exclusion could not be proven")
        return prepared

    async def _abort_after_prepare_failure(
        self,
        prepared: PreparedMaintenance,
        *,
        deadline: float | None = None,
    ) -> None:
        calls = [
            (
                identity,
                self._request(
                    identity,
                    operation_id=prepared.operation_id,
                    command="ABORT",
                    generation=prepared.original_generation,
                ),
            )
            for identity in prepared.initially_open()
        ]
        if deadline is not None and calls and deadline - time.time() <= 0:
            self.barrier.mark_recovery_required(prepared.operation_id)
            raise MaintenanceCoordinatorError(
                "CLIENT_DRAIN_TIMEOUT",
                "runtime-control drain deadline expired before abort reconciliation",
            )
        self.barrier.publish_abort(prepared.operation_id)
        try:
            results = await self._ordered_barrier_calls(calls, deadline=deadline) if calls else {}
        except MaintenanceCoordinatorError as exc:
            if exc.code == "CLIENT_DRAIN_TIMEOUT":
                self.barrier.mark_recovery_required(prepared.operation_id)
            raise
        failures = {
            instance_id: result
            for instance_id, result in results.items()
            if isinstance(result, BaseException)
            or result["status_code"] != "OK"
            or result["state"] not in {"OPEN", "READY"}
            or not bool(result["lease_open"])
        }
        if failures:
            raise MaintenanceCoordinatorError(
                "ABORT_RECONCILIATION_FAILED",
                "pre-mutation maintenance abort could not reconcile every previously open runtime",
            )
        self.barrier.publish_running(prepared.operation_id, generation=prepared.original_generation)

    async def abort_pre_mutation(self, prepared: PreparedMaintenance) -> None:
        await self._abort_after_prepare_failure(prepared)

    async def recover_aborted_pre_mutation(self, prepared: PreparedMaintenance) -> BarrierSnapshot:
        """Validate an already-aborted pre-mutation operation without re-entering maintenance.

        This path is intentionally recovery-only: it does not close runtimes, prove
        exclusion, or mutate the barrier.  It accepts only the exact RUNNING state
        published by the prior abort and fresh live identities for every runtime that
        had been open when maintenance began.
        """

        snapshot = self.barrier.read()
        if (
            snapshot.state != "RUNNING"
            or snapshot.operation_id != prepared.operation_id
            or snapshot.generation != prepared.original_generation
            or snapshot.original_generation != prepared.original_generation
            or snapshot.intent_revision != prepared.intent_revision
        ):
            raise MaintenanceCoordinatorError(
                "PREMUTATION_RECOVERY_MISMATCH",
                "aborted pre-mutation maintenance identity no longer matches the exact RUNNING barrier",
            )
        try:
            database_info = os.stat(self.barrier.database_path)
        except OSError as exc:
            raise MaintenanceCoordinatorError(
                "PREMUTATION_RECOVERY_MISMATCH",
                "aborted pre-mutation database identity is unavailable",
            ) from exc
        if (database_info.st_dev, database_info.st_ino) != (snapshot.device, snapshot.inode):
            raise MaintenanceCoordinatorError(
                "PREMUTATION_RECOVERY_MISMATCH",
                "aborted pre-mutation database identity changed after RUNNING publication",
            )

        live = {identity.instance_id: identity for identity in self._live_registrations()}
        immutable_fields = (
            "protocol_version",
            "owner_uid",
            "pid",
            "process_start_ticks",
            "boot_id",
            "database_path",
            "device",
            "inode",
            "generation",
            "control_socket_path",
        )
        for expected in prepared.initially_open():
            current = live.get(expected.instance_id)
            if (
                current is None
                or current.lifecycle_state not in {"OPEN", "READY"}
                or any(getattr(current, field) != getattr(expected, field) for field in immutable_fields)
            ):
                raise MaintenanceCoordinatorError(
                    "PREMUTATION_RECOVERY_MISMATCH",
                    "a previously open runtime no longer has the exact fresh live identity required for recovery",
                )
        return snapshot

    async def publish_activation_and_resume(self, prepared: PreparedMaintenance, *, new_generation: int) -> ResumeReport:
        self.begin_activation(prepared, new_generation=new_generation)
        self.publish_running(prepared, new_generation=new_generation)
        return await self.resume_running(prepared, new_generation=new_generation)

    def begin_activation(self, prepared: PreparedMaintenance, *, new_generation: int) -> BarrierSnapshot:
        if new_generation <= prepared.original_generation:
            raise MaintenanceCoordinatorError("INVALID_GENERATION", "activated generation must advance")
        snapshot = self.barrier.begin_activation(prepared.operation_id, generation=new_generation)
        if snapshot.original_generation != prepared.original_generation:
            raise MaintenanceCoordinatorError("STALE_GENERATION", "activation original generation does not match prepared maintenance")
        return snapshot

    def rearm_recovery(self, prepared: PreparedMaintenance) -> BarrierSnapshot:
        snapshot = self.barrier.rearm_recovery(
            prepared.operation_id,
            original_generation=prepared.original_generation,
            intent_revision=prepared.intent_revision,
        )
        if snapshot.original_generation != prepared.original_generation or snapshot.intent_revision != prepared.intent_revision:
            raise MaintenanceCoordinatorError(
                "ACTIVATION_IDENTITY_MISMATCH",
                "rearmed barrier does not match prepared maintenance identity",
            )
        return snapshot

    async def resume_activating(self, prepared: PreparedMaintenance, *, new_generation: int) -> ResumeReport:
        snapshot = self.activation_state(
            prepared.operation_id,
            new_generation=new_generation,
        )
        if snapshot.state != "ACTIVATING":
            raise MaintenanceCoordinatorError("INVALID_BARRIER_TRANSITION", "runtimes may resume only while ACTIVATING")
        return await self._resume_prepared(prepared, new_generation=new_generation)

    async def resume_running(self, prepared: PreparedMaintenance, *, new_generation: int) -> ResumeReport:
        snapshot = self.activation_state(
            prepared.operation_id,
            new_generation=new_generation,
        )
        if snapshot.state != "RUNNING":
            raise MaintenanceCoordinatorError("INVALID_BARRIER_TRANSITION", "runtimes may resume after publication only while RUNNING")
        return await self._resume_prepared(prepared, new_generation=new_generation)

    async def _resume_prepared(self, prepared: PreparedMaintenance, *, new_generation: int) -> ResumeReport:
        calls = [
            (
                identity,
                self._request(
                    identity,
                    operation_id=prepared.operation_id,
                    command="RESUME",
                    generation=new_generation,
                ),
            )
            for identity in prepared.initially_open()
        ]
        resume_deadline = time.time() + _RUNTIME_RESUME_TIMEOUT_SECONDS
        results = (
            await self._ordered_barrier_calls(
                calls,
                deadline=resume_deadline,
                deadline_error_code="RUNTIME_RESUME_TIMEOUT",
                deadline_error_message="runtime resume exceeded its absolute deadline",
            )
            if calls
            else {}
        )
        ready: list[str] = []
        failures: dict[str, str] = {}
        for instance_id, result in results.items():
            if isinstance(result, BaseException):
                failures[instance_id] = "UNREACHABLE"
                continue
            if (
                result["status_code"] == "OK"
                and result["state"] == "READY"
                and result["actual_generation"] == new_generation
                and bool(result["lease_open"])
            ):
                ready.append(instance_id)
            else:
                failures[instance_id] = str(result["status_code"])
        return ResumeReport(tuple(sorted(ready)), failures)

    def publish_running(self, prepared: PreparedMaintenance, *, new_generation: int) -> BarrierSnapshot:
        snapshot = self.activation_state(prepared.operation_id, new_generation=new_generation)
        if snapshot.state != "ACTIVATING":
            if snapshot.state == "RUNNING":
                return snapshot
            raise MaintenanceCoordinatorError("INVALID_BARRIER_TRANSITION", "activation is not ready for RUNNING")
        return self.barrier.publish_running(prepared.operation_id, generation=new_generation)

    def activation_state(
        self,
        operation_id: str,
        *,
        new_generation: int,
        device: int | None = None,
        inode: int | None = None,
    ) -> BarrierSnapshot:
        operation_id = _canonical_uuid(operation_id, "operation_id")
        snapshot = self.barrier.read()
        if snapshot.operation_id != operation_id or snapshot.generation != new_generation:
            raise MaintenanceCoordinatorError(
                "ACTIVATION_IDENTITY_MISMATCH",
                "activation operation or generation does not match durable barrier state",
            )
        if snapshot.state not in {"ACTIVATING", "RUNNING"}:
            raise MaintenanceCoordinatorError(
                "INVALID_BARRIER_TRANSITION",
                "durable barrier is not in an activation-finalizable state",
            )
        if device is not None and snapshot.device != device:
            raise MaintenanceCoordinatorError("ACTIVATION_IDENTITY_MISMATCH", "activation device identity does not match")
        if inode is not None and snapshot.inode != inode:
            raise MaintenanceCoordinatorError("ACTIVATION_IDENTITY_MISMATCH", "activation inode identity does not match")
        return snapshot

    def reconcile_activation_ready(
        self,
        operation_id: str,
        *,
        new_generation: int,
        device: int,
        inode: int,
    ) -> BarrierSnapshot:
        snapshot = self.activation_state(
            operation_id,
            new_generation=new_generation,
            device=device,
            inode=inode,
        )
        if snapshot.state == "ACTIVATING":
            return self.barrier.publish_running(operation_id, generation=new_generation)
        return snapshot

    def mark_recovery_required(self, operation_id: str) -> BarrierSnapshot:
        return self.barrier.mark_recovery_required(operation_id)
