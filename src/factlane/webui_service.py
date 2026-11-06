from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


PORT_MIN = 8765
PORT_MAX = 8774
CRASH_RETRY_SECONDS = 5.0
MAX_CRASH_RETRIES = 3
READY_RESET_SECONDS = 600.0
STATE_VERSION = 1
_CLAIM_HOST_RE = re.compile(r"factlane-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.test")


class ServiceStatus(str, Enum):
    READY = "READY"
    STARTING = "STARTING"
    STOPPED_UNTIL_REBOOT = "STOPPED_UNTIL_REBOOT"
    MAINTENANCE = "MAINTENANCE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    FAILED = "FAILED"


class ServiceStateError(RuntimeError):
    pass


class EndpointUnavailable(ServiceStateError):
    pass


@dataclass(frozen=True)
class RetrySchedule:
    attempt: int
    retry_at: float


@dataclass(frozen=True)
class ServiceSnapshot:
    status: ServiceStatus
    saved_endpoint: str | None
    stopped_boot_id: str | None
    intent_revision: int
    crash_attempts: int
    next_retry_at: float | None
    ready_since: float | None
    maintenance_operation_id: str | None


@dataclass
class _PersistentState:
    version: int = STATE_VERSION
    status: str = ServiceStatus.STARTING.value
    saved_endpoint: str | None = None
    stopped_boot_id: str | None = None
    intent_revision: int = 0
    last_intent_id: str | None = None
    last_intent_action: str | None = None
    crash_attempts: int = 0
    next_retry_at: float | None = None
    observed_boot_id: str | None = None
    ready_since: float | None = None
    ready_boot_id: str | None = None
    maintenance_operation_id: str | None = None
    maintenance_initial_status: str | None = None
    maintenance_initial_stopped: bool = False
    maintenance_intent_revision: int | None = None


Controller = Callable[[str], None]
Clock = Callable[[], float]
BootIdProvider = Callable[[], str]
Listener = Callable[[int], bool]


def _noop_controller(action: str) -> None:
    del action


def _linux_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise ServiceStateError("Linux boot identity is unavailable") from exc
    if not value:
        raise ServiceStateError("Linux boot identity is empty")
    return value


class WebUIServiceState:
    """Persistent service-state primitives with injected host-side actions.

    This type records Owner intent and bounded service state. It never invokes
    systemd or mutates host configuration itself; the caller supplies any controller
    and listener operations used by a higher-level installer/service adapter.
    """

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        controller: Controller = _noop_controller,
        clock: Clock = time.monotonic,
        boot_id: BootIdProvider = _linux_boot_id,
        listener: Listener | None = None,
    ) -> None:
        self._path = Path(state_path)
        self._controller = controller
        self._clock = clock
        self._boot_id = boot_id
        self._listener = listener
        self._lock = threading.RLock()
        self._ensure_private_parent()
        self._state = self._load_state()
        if self._reconcile_boot():
            self._write_state()

    def snapshot(self) -> ServiceSnapshot:
        with self._lock:
            if self._reconcile_boot():
                self._write_state()
            return self._snapshot()

    def ensure_endpoint(self, hostname: str) -> str:
        with self._lock:
            if self._listener is None:
                raise EndpointUnavailable("listener binding function is required")
            self._validate_hostname(hostname)
            saved = self._state.saved_endpoint
            if saved is not None:
                saved_host, saved_port = self._parse_saved_endpoint(saved)
                if saved_host != hostname:
                    raise EndpointUnavailable("saved endpoint hostname does not match this installation")
                if not self._attempt_bind(saved_port):
                    raise EndpointUnavailable("saved endpoint is unavailable; explicit recovery is required")
                return saved

            for port in range(PORT_MIN, PORT_MAX + 1):
                if not self._attempt_bind(port):
                    continue
                endpoint = f"https://{hostname}:{port}"
                self._state.saved_endpoint = endpoint
                self._write_state()
                return endpoint
            raise EndpointUnavailable(f"no endpoint from {PORT_MIN} through {PORT_MAX} could be bound")

    def claim_endpoint(self, origin: str) -> ServiceSnapshot:
        """Persist the exact HTTPS endpoint only after the managed listener is ready."""

        with self._lock:
            endpoint = self._canonical_claim_endpoint(origin)
            saved = self._state.saved_endpoint
            if saved is None:
                self._state.saved_endpoint = endpoint
                self._write_state()
                return self._snapshot()
            if saved == endpoint:
                return self._snapshot()
            raise EndpointUnavailable(
                "saved endpoint differs from the ready managed endpoint; explicit recovery is required"
            )

    def request_stop(self, request_id: str) -> ServiceSnapshot:
        with self._lock:
            if self._is_duplicate_intent("stop", request_id):
                return self._snapshot()
            current_boot = self._current_boot_id()
            if self._state.stopped_boot_id == current_boot:
                return self._snapshot()

            self._record_intent("stop", request_id)
            self._state.stopped_boot_id = current_boot
            self._state.ready_since = None
            self._state.ready_boot_id = None
            self._state.next_retry_at = None
            if self._state.status not in {
                ServiceStatus.MAINTENANCE.value,
                ServiceStatus.RECOVERY_REQUIRED.value,
            }:
                self._state.status = ServiceStatus.STOPPED_UNTIL_REBOOT.value
            self._write_state()
            self._controller("stop")
            return self._snapshot()

    def request_start(self, request_id: str) -> ServiceSnapshot:
        with self._lock:
            if self._is_duplicate_intent("start", request_id):
                return self._snapshot()
            self._reconcile_boot()
            blocked = self._state.status in {
                ServiceStatus.MAINTENANCE.value,
                ServiceStatus.RECOVERY_REQUIRED.value,
            }
            has_stop = self._state.stopped_boot_id == self._current_boot_id()
            if not has_stop and self._state.status in {
                ServiceStatus.STARTING.value,
                ServiceStatus.READY.value,
            }:
                self._record_intent("start", request_id)
                self._state.crash_attempts = 0
                self._state.next_retry_at = None
                self._write_state()
                return self._snapshot()

            self._record_intent("start", request_id)
            self._state.stopped_boot_id = None
            self._state.ready_since = None
            self._state.ready_boot_id = None
            self._state.next_retry_at = None
            if not blocked:
                self._state.crash_attempts = 0
                self._state.status = ServiceStatus.STARTING.value
            self._write_state()
            if not blocked:
                self._controller("start")
            return self._snapshot()

    def request_restart(self, request_id: str) -> ServiceSnapshot:
        with self._lock:
            if self._is_duplicate_intent("restart", request_id):
                return self._snapshot()
            blocked = self._state.status in {
                ServiceStatus.MAINTENANCE.value,
                ServiceStatus.RECOVERY_REQUIRED.value,
                ServiceStatus.STOPPED_UNTIL_REBOOT.value,
            }
            self._record_intent("restart", request_id)
            self._state.ready_since = None
            self._state.ready_boot_id = None
            self._state.next_retry_at = None
            if not blocked:
                self._state.crash_attempts = 0
                self._state.status = ServiceStatus.STARTING.value
            self._write_state()
            if not blocked:
                self._controller("restart")
            return self._snapshot()

    def begin_maintenance(self, operation_id: str) -> ServiceSnapshot:
        with self._lock:
            if not operation_id:
                raise ValueError("operation_id is required")
            if self._state.maintenance_operation_id == operation_id:
                return self._snapshot()
            if self._state.maintenance_operation_id is not None:
                raise ServiceStateError("another maintenance operation already owns service state")
            if self._state.status == ServiceStatus.RECOVERY_REQUIRED.value:
                raise ServiceStateError("recovery must be completed before new maintenance")

            current_boot = self._current_boot_id()
            self._state.maintenance_operation_id = operation_id
            self._state.maintenance_initial_status = self._state.status
            self._state.maintenance_initial_stopped = self._state.stopped_boot_id == current_boot
            self._state.maintenance_intent_revision = self._state.intent_revision
            self._state.status = ServiceStatus.MAINTENANCE.value
            self._state.ready_since = None
            self._state.ready_boot_id = None
            self._state.next_retry_at = None
            self._write_state()
            return self._snapshot()

    def finish_maintenance(self, operation_id: str) -> ServiceSnapshot:
        with self._lock:
            self._require_maintenance_operation(operation_id)
            current_boot = self._current_boot_id()
            initial_status = self._state.maintenance_initial_status
            started_revision = self._state.maintenance_intent_revision
            newer_stop = (
                self._state.stopped_boot_id == current_boot
                and started_revision is not None
                and self._state.intent_revision > started_revision
            )
            restore_stopped = self._state.maintenance_initial_stopped or newer_stop

            self._clear_maintenance_fields()
            if restore_stopped:
                self._state.status = ServiceStatus.STOPPED_UNTIL_REBOOT.value
                self._write_state()
                return self._snapshot()

            if initial_status == ServiceStatus.FAILED.value:
                self._state.status = ServiceStatus.FAILED.value
                self._write_state()
                return self._snapshot()

            self._state.status = ServiceStatus.STARTING.value
            self._write_state()
            self._controller("start")
            return self._snapshot()

    def mark_recovery_required(self) -> ServiceSnapshot:
        with self._lock:
            self._clear_maintenance_fields()
            self._state.status = ServiceStatus.RECOVERY_REQUIRED.value
            self._state.ready_since = None
            self._state.ready_boot_id = None
            self._state.next_retry_at = None
            self._write_state()
            return self._snapshot()

    def clear_recovery_required(self) -> ServiceSnapshot:
        with self._lock:
            if self._state.status != ServiceStatus.RECOVERY_REQUIRED.value:
                return self._snapshot()
            if self._state.stopped_boot_id == self._current_boot_id():
                self._state.status = ServiceStatus.STOPPED_UNTIL_REBOOT.value
                self._write_state()
                return self._snapshot()
            self._state.status = ServiceStatus.STARTING.value
            self._write_state()
            self._controller("start")
            return self._snapshot()

    def mark_starting(self) -> ServiceSnapshot:
        with self._lock:
            if self._state.status == ServiceStatus.RECOVERY_REQUIRED.value:
                return self._snapshot()
            if self._state.stopped_boot_id == self._current_boot_id():
                self._state.status = ServiceStatus.STOPPED_UNTIL_REBOOT.value
            else:
                self._state.status = ServiceStatus.STARTING.value
            self._state.ready_since = None
            self._state.ready_boot_id = None
            self._state.next_retry_at = None
            self._write_state()
            return self._snapshot()

    def mark_ready(self) -> ServiceSnapshot:
        with self._lock:
            if self._state.status == ServiceStatus.RECOVERY_REQUIRED.value:
                raise ServiceStateError("recovery-required service cannot be marked ready")
            if self._state.stopped_boot_id == self._current_boot_id():
                raise ServiceStateError("stopped-until-reboot service cannot be marked ready")
            if self._state.status != ServiceStatus.READY.value or self._state.ready_since is None:
                self._state.ready_since = self._clock()
                self._state.ready_boot_id = self._current_boot_id()
            self._state.status = ServiceStatus.READY.value
            self._state.next_retry_at = None
            self._write_state()
            return self._snapshot()

    def observe_ready(self) -> ServiceSnapshot:
        with self._lock:
            if self._state.status != ServiceStatus.READY.value or self._state.ready_since is None:
                return self._snapshot()
            if self._state.ready_boot_id != self._current_boot_id():
                self._state.ready_since = None
                self._state.ready_boot_id = None
                self._state.status = ServiceStatus.STARTING.value
                self._write_state()
                return self._snapshot()
            if self._reset_crash_incident_after_stable_ready():
                self._write_state()
            return self._snapshot()

    def record_crash(self) -> RetrySchedule | None:
        with self._lock:
            if self._reconcile_boot():
                self._write_state()
            if self._state.stopped_boot_id == self._current_boot_id():
                self._state.status = ServiceStatus.STOPPED_UNTIL_REBOOT.value
                self._state.next_retry_at = None
                self._write_state()
                return None
            if self._state.status in {
                ServiceStatus.MAINTENANCE.value,
                ServiceStatus.RECOVERY_REQUIRED.value,
            }:
                self._state.next_retry_at = None
                self._write_state()
                return None
            self._reset_crash_incident_after_stable_ready()
            self._state.ready_since = None
            self._state.ready_boot_id = None
            if self._state.crash_attempts >= MAX_CRASH_RETRIES:
                self._state.status = ServiceStatus.FAILED.value
                self._state.next_retry_at = None
                self._write_state()
                return None

            self._state.crash_attempts += 1
            retry_at = self._clock() + CRASH_RETRY_SECONDS
            self._state.next_retry_at = retry_at
            self._state.status = ServiceStatus.STARTING.value
            self._write_state()
            return RetrySchedule(attempt=self._state.crash_attempts, retry_at=retry_at)

    def mark_failed(self) -> ServiceSnapshot:
        with self._lock:
            self._state.status = ServiceStatus.FAILED.value
            self._state.ready_since = None
            self._state.ready_boot_id = None
            self._state.next_retry_at = None
            self._write_state()
            return self._snapshot()

    def _snapshot(self) -> ServiceSnapshot:
        return ServiceSnapshot(
            status=ServiceStatus(self._state.status),
            saved_endpoint=self._state.saved_endpoint,
            stopped_boot_id=self._state.stopped_boot_id,
            intent_revision=self._state.intent_revision,
            crash_attempts=self._state.crash_attempts,
            next_retry_at=self._state.next_retry_at,
            ready_since=self._state.ready_since,
            maintenance_operation_id=self._state.maintenance_operation_id,
        )

    def _record_intent(self, action: str, request_id: str) -> None:
        if not request_id:
            raise ValueError("request_id is required")
        if self._state.last_intent_id == request_id and self._state.last_intent_action != action:
            raise ServiceStateError("request_id was already used for another service intent")
        self._state.intent_revision += 1
        self._state.last_intent_id = request_id
        self._state.last_intent_action = action

    def _is_duplicate_intent(self, action: str, request_id: str) -> bool:
        if not request_id:
            raise ValueError("request_id is required")
        if self._state.last_intent_id != request_id:
            return False
        if self._state.last_intent_action != action:
            raise ServiceStateError("request_id was already used for another service intent")
        return True

    def _require_maintenance_operation(self, operation_id: str) -> None:
        if self._state.maintenance_operation_id != operation_id:
            raise ServiceStateError("maintenance operation identity does not match current owner")

    def _clear_maintenance_fields(self) -> None:
        self._state.maintenance_operation_id = None
        self._state.maintenance_initial_status = None
        self._state.maintenance_initial_stopped = False
        self._state.maintenance_intent_revision = None

    def _attempt_bind(self, port: int) -> bool:
        assert self._listener is not None
        try:
            return bool(self._listener(port))
        except OSError:
            return False

    def _reset_crash_incident_after_stable_ready(self) -> bool:
        if (
            self._state.status == ServiceStatus.READY.value
            and self._state.ready_since is not None
            and self._state.ready_boot_id == self._current_boot_id()
            and self._clock() - self._state.ready_since >= READY_RESET_SECONDS
            and self._state.crash_attempts
        ):
            self._state.crash_attempts = 0
            self._state.next_retry_at = None
            return True
        return False

    @staticmethod
    def _validate_hostname(hostname: str) -> None:
        if not hostname or ":" in hostname or "/" in hostname or hostname != hostname.strip():
            raise ValueError("hostname must be one exact host name")

    @staticmethod
    def _parse_saved_endpoint(endpoint: str) -> tuple[str, int]:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or parsed.hostname is None or parsed.port is None:
            raise ServiceStateError("saved endpoint is invalid")
        if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ServiceStateError("saved endpoint is invalid")
        if not PORT_MIN <= parsed.port <= PORT_MAX:
            raise ServiceStateError("saved endpoint port is outside the supported range")
        return parsed.hostname, parsed.port

    @staticmethod
    def _canonical_claim_endpoint(origin: str) -> str:
        if not isinstance(origin, str):
            raise EndpointUnavailable("endpoint must be one canonical FactLane HTTPS origin")
        try:
            parsed = urlsplit(origin)
            port = parsed.port
        except ValueError as exc:
            raise EndpointUnavailable("endpoint must be one canonical FactLane HTTPS origin") from exc
        hostname = parsed.hostname
        if (
            parsed.scheme != "https"
            or hostname is None
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not PORT_MIN <= port <= PORT_MAX
            or _CLAIM_HOST_RE.fullmatch(hostname) is None
        ):
            raise EndpointUnavailable("endpoint must be one canonical FactLane HTTPS origin")
        canonical = f"https://{hostname}:{port}"
        if origin != canonical:
            raise EndpointUnavailable("endpoint must be one canonical FactLane HTTPS origin")
        return canonical

    def _current_boot_id(self) -> str:
        boot = self._boot_id()
        if not isinstance(boot, str) or not boot.strip():
            raise ServiceStateError("boot identity must be a non-empty string")
        return boot.strip()

    def _reconcile_boot(self) -> bool:
        current_boot = self._current_boot_id()
        changed = False
        if self._state.observed_boot_id != current_boot:
            if self._state.observed_boot_id is not None:
                self._state.next_retry_at = None
                self._state.ready_since = None
                self._state.ready_boot_id = None
                if self._state.status == ServiceStatus.FAILED.value:
                    self._state.crash_attempts = 0
                    self._state.status = ServiceStatus.STARTING.value
                elif self._state.status == ServiceStatus.READY.value:
                    self._state.status = ServiceStatus.STARTING.value
            self._state.observed_boot_id = current_boot
            changed = True
        if self._state.stopped_boot_id is not None and self._state.stopped_boot_id != current_boot:
            self._state.stopped_boot_id = None
            if self._state.status == ServiceStatus.STOPPED_UNTIL_REBOOT.value:
                self._state.status = ServiceStatus.STARTING.value
            changed = True
        if self._state.ready_boot_id is not None and self._state.ready_boot_id != current_boot:
            self._state.ready_since = None
            self._state.ready_boot_id = None
            self._state.next_retry_at = None
            if self._state.status == ServiceStatus.READY.value:
                self._state.status = ServiceStatus.STARTING.value
            changed = True
        return changed

    def _ensure_private_parent(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self._path.parent, 0o700)
        except OSError as exc:
            raise ServiceStateError("owner-private service state directory could not be secured") from exc

    def _load_state(self) -> _PersistentState:
        if not self._path.exists():
            return _PersistentState()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceStateError("service state could not be read") from exc
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            raise ServiceStateError("service state version is unsupported")
        allowed = set(_PersistentState.__dataclass_fields__)
        if set(raw) - allowed:
            raise ServiceStateError("service state contains unknown fields")
        try:
            state = _PersistentState(**raw)
            ServiceStatus(state.status)
        except (TypeError, ValueError) as exc:
            raise ServiceStateError("service state is invalid") from exc
        if not isinstance(state.intent_revision, int) or state.intent_revision < 0:
            raise ServiceStateError("service intent revision is invalid")
        if not isinstance(state.crash_attempts, int) or not 0 <= state.crash_attempts <= MAX_CRASH_RETRIES:
            raise ServiceStateError("service crash incident counter is invalid")
        if self._path.stat().st_mode & 0o077:
            os.chmod(self._path, 0o600)
        return state

    def _write_state(self) -> None:
        self._ensure_private_parent()
        payload = json.dumps(asdict(self._state), sort_keys=True, separators=(",", ":")) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{self._path.name}.", dir=self._path.parent)
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
            directory_fd = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            raise
