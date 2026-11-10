from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Protocol
from urllib.parse import urlsplit

from factlane.webui_service import (
    EndpointUnavailable,
    ServiceStatus,
    WebUIServiceState,
    is_legacy_endpoint_canonicalization,
)


READINESS_DEADLINE_SECONDS = 10.0
STOP_DRAIN_DEADLINE_SECONDS = 30.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.1
MAX_CONFIG_BYTES = 64 * 1024
_PORT_MIN = 8765
_PORT_MAX = 8774
_HOST_RE = re.compile(r"factlane-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.test")
_LEGACY_LINUX_CONFIG_FIELDS = frozenset(
    {
        "db",
        "profile",
        "host_id",
        "origin",
        "backend_socket",
        "caddy_path",
        "caddyfile",
        "runtime_dir",
        "state_dir",
    }
)
_PLATFORM_CONFIG_FIELDS = frozenset({"platform_kind", "backend_transport"})
_EXPLICIT_CONFIG_FIELDS = _LEGACY_LINUX_CONFIG_FIELDS | _PLATFORM_CONFIG_FIELDS
_OPTIONAL_CONFIG_FIELDS = frozenset({"public_origins"})
_MEMORY_SELECTION_FIELDS = frozenset({"db", "profile", "host_id"})
_MAX_PUBLIC_ORIGINS = 8


class RuntimeConfigError(ValueError):
    pass


class RuntimeStateError(RuntimeError):
    pass


class BackendMode(str, Enum):
    NORMAL = "NORMAL"
    SETUP_SHELL = "SETUP_SHELL"
    RECOVERY_SHELL = "RECOVERY_SHELL"


class PlatformKind(str, Enum):
    LINUX = "LINUX"
    WINDOWS = "WINDOWS"


class BackendTransportKind(str, Enum):
    AF_UNIX = "AF_UNIX"
    WINDOWS_HTTPS = "WINDOWS_HTTPS"
    WINDOWS_HTTP = "WINDOWS_HTTP"


class RuntimeOutcomeState(str, Enum):
    READY = "READY"
    SHELL_READY = "SHELL_READY"
    RECOVERY_READY = "RECOVERY_READY"
    STOPPED = "STOPPED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class WebUIRuntimeConfig:
    db: str | None
    profile: str | None
    host_id: str | None
    origin: str
    backend_socket: str | None
    caddy_path: str | None
    caddyfile: str | None
    runtime_dir: str
    state_dir: str
    public_origins: tuple[str, ...] = ()
    platform_kind: PlatformKind | str = PlatformKind.LINUX
    backend_transport: BackendTransportKind | str = BackendTransportKind.AF_UNIX

    def __post_init__(self) -> None:
        try:
            platform_kind = PlatformKind(self.platform_kind)
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigError("platform_kind is unsupported") from exc
        try:
            backend_transport = BackendTransportKind(self.backend_transport)
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigError("backend_transport is unsupported") from exc
        object.__setattr__(self, "platform_kind", platform_kind)
        object.__setattr__(self, "backend_transport", backend_transport)

        configured_values = (self.db, self.profile, self.host_id)
        configured_count = sum(value is not None for value in configured_values)
        if configured_count not in {0, 3}:
            raise RuntimeConfigError(
                "db, profile, and host_id must be all configured or all null"
            )
        if self.memory_configured:
            assert self.db is not None
            assert self.profile is not None
            assert self.host_id is not None
            if not all(
                isinstance(value, str)
                for value in (self.db, self.profile, self.host_id)
            ):
                raise RuntimeConfigError(
                    "db, profile, and host_id must be strings when configured"
                )
            if not self.profile or any(ord(ch) < 32 for ch in self.profile):
                raise RuntimeConfigError("profile must be a non-empty text identifier")
            if (
                not self.host_id
                or self.host_id != self.host_id.strip()
                or any(ch.isspace() or ord(ch) < 32 for ch in self.host_id)
            ):
                raise RuntimeConfigError(
                    "host_id must be a non-empty stable identifier without whitespace"
                )
        _validate_origin(self.origin)
        if not isinstance(self.public_origins, (tuple, list)):
            raise RuntimeConfigError("public_origins must be a bounded list of exact HTTPS origins")
        public_origins = tuple(self.public_origins)
        if len(public_origins) > _MAX_PUBLIC_ORIGINS:
            raise RuntimeConfigError("public_origins exceeds the configured origin limit")
        if len(set(public_origins)) != len(public_origins):
            raise RuntimeConfigError("public_origins must not contain duplicates")
        for public_origin in public_origins:
            _validate_public_origin(public_origin)
            if public_origin == self.origin:
                raise RuntimeConfigError("public_origins must not repeat the saved local origin")
        object.__setattr__(self, "public_origins", public_origins)
        if platform_kind is PlatformKind.LINUX:
            if backend_transport is not BackendTransportKind.AF_UNIX:
                raise RuntimeConfigError("Linux runtime requires AF_UNIX backend transport")
            for value, label in (
                (self.backend_socket, "backend_socket"),
                (self.caddy_path, "caddy_path"),
                (self.caddyfile, "caddyfile"),
                (self.runtime_dir, "runtime_dir"),
                (self.state_dir, "state_dir"),
            ):
                if not isinstance(value, str):
                    raise RuntimeConfigError(f"{label} must be a string")
                _validate_absolute_posix_path(value, label)
            if self.memory_configured:
                assert self.db is not None
                _validate_absolute_posix_path(self.db, "db")
            assert self.backend_socket is not None
            runtime = PurePosixPath(self.runtime_dir)
            backend = PurePosixPath(self.backend_socket)
            try:
                backend.relative_to(runtime)
            except ValueError as exc:
                raise RuntimeConfigError("backend_socket must be inside runtime_dir") from exc
            if backend == runtime:
                raise RuntimeConfigError("backend_socket must name a socket path inside runtime_dir")
            return

        if backend_transport not in {
            BackendTransportKind.WINDOWS_HTTP,
            BackendTransportKind.WINDOWS_HTTPS,
        }:
            raise RuntimeConfigError("Windows runtime requires a managed loopback backend transport")
        parsed_origin = urlsplit(self.origin)
        if (
            backend_transport is BackendTransportKind.WINDOWS_HTTP
            and not (
                parsed_origin.scheme == "http"
                and parsed_origin.hostname == "127.0.0.1"
            )
        ):
            raise RuntimeConfigError(
                "WINDOWS_HTTP requires the canonical 127.0.0.1 HTTP origin"
            )
        if (
            backend_transport is BackendTransportKind.WINDOWS_HTTPS
            and parsed_origin.scheme != "https"
        ):
            raise RuntimeConfigError("WINDOWS_HTTPS requires a managed HTTPS origin")
        if self.backend_socket is not None or self.caddy_path is not None or self.caddyfile is not None:
            raise RuntimeConfigError(
                "Windows backend-owned HTTPS transport must not configure backend_socket or Caddy paths"
            )
        _validate_absolute_windows_local_path(self.runtime_dir, "runtime_dir")
        _validate_absolute_windows_local_path(self.state_dir, "state_dir")
        if self.memory_configured:
            assert self.db is not None
            _validate_absolute_windows_local_path(self.db, "db")

    @property
    def memory_configured(self) -> bool:
        return self.db is not None


@dataclass(frozen=True)
class BackendLaunchSpec:
    db: str | None
    profile: str | None
    host_id: str | None
    expected_origin: str
    socket_path: str | None
    runtime_dir: str
    state_dir: str
    mode: BackendMode
    normal_adapter_required: bool
    public_origins: tuple[str, ...] = ()
    platform_kind: PlatformKind = PlatformKind.LINUX
    backend_transport: BackendTransportKind = BackendTransportKind.AF_UNIX


@dataclass(frozen=True)
class RuntimeOutcome:
    state: RuntimeOutcomeState
    retry_at: float | None = None
    detail: str | None = None


class OwnerHTTPBackend(Protocol):
    def start(self) -> None: ...

    def is_ready(self) -> bool: ...

    def is_alive(self) -> bool: ...

    def begin_drain(self) -> None: ...

    def wait_stopped(self, timeout: float) -> bool: ...


class CaddyProcess(Protocol):
    def is_ready(self) -> bool: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait_stopped(self, timeout: float) -> bool: ...


BackendFactory = Callable[[BackendLaunchSpec, WebUIServiceState], OwnerHTTPBackend]
ProcessFactory = Callable[[WebUIRuntimeConfig, tuple[str, ...]], CaddyProcess]
Clock = Callable[[], float]
Sleep = Callable[[float], None]
StopPredicate = Callable[[], bool]


def default_runtime_config_path(
    *,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the canonical owner runtime-config path for exact `serve webui` startup.

    An absolute XDG_CONFIG_HOME wins. Missing, empty, or relative XDG_CONFIG_HOME
    follows the XDG fallback under the Owner's home directory. This helper only
    selects a path; `load_runtime_config` remains responsible for ownership,
    symlink, permission, and content validation.
    """

    environment = os.environ if environ is None else environ
    xdg_config_home = environment.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        if not isinstance(xdg_config_home, str):
            raise RuntimeConfigError("XDG_CONFIG_HOME must be text")
        if "\x00" in xdg_config_home or any(ch in xdg_config_home for ch in ("\n", "\r")):
            raise RuntimeConfigError("XDG_CONFIG_HOME contains unsafe characters")
        if os.path.isabs(xdg_config_home):
            base = Path(os.path.normpath(xdg_config_home))
            return base / "factlane" / "webui.json"

    owner_home = Path.home() if home is None else Path(home)
    owner_home_text = os.fspath(owner_home)
    if "\x00" in owner_home_text or any(ch in owner_home_text for ch in ("\n", "\r")):
        raise RuntimeConfigError("owner home contains unsafe characters")
    if not owner_home.is_absolute():
        raise RuntimeConfigError("owner home must be an absolute path")
    base = Path(os.path.normpath(owner_home_text)) / ".config"
    return base / "factlane" / "webui.json"


def load_runtime_config(
    path: str | os.PathLike[str],
    *,
    owner_uid: int | None = None,
) -> WebUIRuntimeConfig:
    """Load an exact owner-private runtime config without following symlinks."""

    config_path = Path(path)
    expected_uid = os.getuid() if owner_uid is None else owner_uid
    try:
        before = config_path.lstat()
    except OSError as exc:
        raise RuntimeConfigError("runtime config is unavailable") from exc
    if stat.S_ISLNK(before.st_mode):
        raise RuntimeConfigError("runtime config must not be a symlink")
    _validate_config_stat(before, expected_uid)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(config_path, flags)
    except OSError as exc:
        raise RuntimeConfigError("runtime config could not be opened safely") from exc
    try:
        opened = os.fstat(fd)
        _validate_config_stat(opened, expected_uid)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeConfigError("runtime config changed while being opened")
        if opened.st_size > MAX_CONFIG_BYTES:
            raise RuntimeConfigError("runtime config exceeds the 64 KiB limit")
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise RuntimeConfigError("runtime config exceeds the 64 KiB limit")
    finally:
        os.close(fd)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeConfigError("runtime config must be UTF-8 JSON") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_strict_json_object)
    except (json.JSONDecodeError, RuntimeConfigError) as exc:
        if isinstance(exc, RuntimeConfigError):
            raise
        raise RuntimeConfigError("runtime config must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeConfigError("runtime config must be one JSON object")
    received = set(payload)
    # Absence of the platform fields is the exact pre-portability serialized
    # Linux schema. This is a schema-version rule, never host-OS inference.
    explicit_platform = bool(received & _PLATFORM_CONFIG_FIELDS)
    expected_fields = _EXPLICIT_CONFIG_FIELDS if explicit_platform else _LEGACY_LINUX_CONFIG_FIELDS
    unknown = received - expected_fields - _OPTIONAL_CONFIG_FIELDS
    missing = expected_fields - received
    if unknown:
        raise RuntimeConfigError(f"runtime config contains unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise RuntimeConfigError(f"runtime config is missing required fields: {', '.join(sorted(missing))}")
    if any(
        payload[field] is not None and not isinstance(payload[field], str)
        for field in _MEMORY_SELECTION_FIELDS
    ):
        raise RuntimeConfigError(
            "db, profile, and host_id must be strings when configured or null when absent"
        )
    if "public_origins" in payload:
        raw_public_origins = payload["public_origins"]
        if (
            not isinstance(raw_public_origins, list)
            or len(raw_public_origins) > _MAX_PUBLIC_ORIGINS
            or any(not isinstance(item, str) for item in raw_public_origins)
        ):
            raise RuntimeConfigError("public_origins must be a bounded list of exact HTTPS origins")
        payload["public_origins"] = tuple(raw_public_origins)
    if not explicit_platform:
        transport_fields = _LEGACY_LINUX_CONFIG_FIELDS - _MEMORY_SELECTION_FIELDS
        if any(not isinstance(payload[field], str) for field in transport_fields):
            raise RuntimeConfigError("every transport runtime config field must be a string")
        return WebUIRuntimeConfig(**payload)

    if any(not isinstance(payload[field], str) for field in _PLATFORM_CONFIG_FIELDS):
        raise RuntimeConfigError("platform_kind and backend_transport must be strings")
    if not isinstance(payload["origin"], str) or not isinstance(payload["runtime_dir"], str) or not isinstance(payload["state_dir"], str):
        raise RuntimeConfigError("origin, runtime_dir, and state_dir must be strings")
    platform_kind = payload["platform_kind"]
    if platform_kind == PlatformKind.LINUX.value:
        for field in ("backend_socket", "caddy_path", "caddyfile"):
            if not isinstance(payload[field], str):
                raise RuntimeConfigError("Linux transport runtime config fields must be strings")
    elif platform_kind == PlatformKind.WINDOWS.value:
        for field in ("backend_socket", "caddy_path", "caddyfile"):
            if payload[field] is not None:
                raise RuntimeConfigError("Windows backend-owned transport fields must be null")
    else:
        raise RuntimeConfigError("platform_kind is unsupported")
    return WebUIRuntimeConfig(**payload)


def caddy_command(config: WebUIRuntimeConfig) -> tuple[str, ...]:
    """Return the exact argv used by a process factory; no shell string is produced."""

    if (
        config.platform_kind is not PlatformKind.LINUX
        or config.backend_transport is not BackendTransportKind.AF_UNIX
        or config.caddy_path is None
        or config.caddyfile is None
    ):
        raise RuntimeConfigError("Caddy command is available only for Linux AF_UNIX runtime config")
    return (
        config.caddy_path,
        "run",
        "--config",
        config.caddyfile,
        "--adapter",
        "caddyfile",
    )


class WebUIRuntimeRunner:
    """Supervise the Owner backend and, when configured, an external local gateway.

    Linux production continues to require the external Caddy gateway. A platform
    whose backend owns the local HTTPS transport may set external_gateway_required
    to False; this changes only lifecycle supervision and does not implement that
    platform's backend transport.
    """

    def __init__(
        self,
        config: WebUIRuntimeConfig,
        service_state: WebUIServiceState,
        *,
        backend_factory: BackendFactory,
        process_factory: ProcessFactory | None,
        external_gateway_required: bool = True,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.config = config
        self.service_state = service_state
        self._backend_factory = backend_factory
        self._process_factory = process_factory
        self._external_gateway_required = bool(external_gateway_required)
        if self._external_gateway_required and self._process_factory is None:
            raise ValueError("process_factory is required when the external gateway is enabled")
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._backend: OwnerHTTPBackend | None = None
        self._caddy: CaddyProcess | None = None
        self._mode: BackendMode | None = None

    def start(self, *, mode: BackendMode = BackendMode.NORMAL) -> RuntimeOutcome:
        if self._backend is not None or self._caddy is not None:
            raise RuntimeStateError("WebUI runtime components are already active")
        snapshot = self.service_state.snapshot()
        if snapshot.status is ServiceStatus.STOPPED_UNTIL_REBOOT:
            return RuntimeOutcome(RuntimeOutcomeState.STOPPED)
        if (
            snapshot.saved_endpoint is not None
            and snapshot.saved_endpoint != self.config.origin
            and not is_legacy_endpoint_canonicalization(
                snapshot.saved_endpoint,
                self.config.origin,
            )
        ):
            return RuntimeOutcome(
                RuntimeOutcomeState.FAILED,
                detail="runtime config origin differs from saved endpoint; explicit recovery is required",
            )

        if snapshot.status is ServiceStatus.RECOVERY_REQUIRED:
            effective_mode = BackendMode.RECOVERY_SHELL
        elif mode is BackendMode.NORMAL and not self.config.memory_configured:
            effective_mode = BackendMode.SETUP_SHELL
        else:
            effective_mode = mode
        if effective_mode is BackendMode.NORMAL:
            self.service_state.mark_starting()

        spec = BackendLaunchSpec(
            db=self.config.db,
            profile=self.config.profile,
            host_id=self.config.host_id,
            expected_origin=self.config.origin,
            socket_path=self.config.backend_socket,
            runtime_dir=self.config.runtime_dir,
            state_dir=self.config.state_dir,
            mode=effective_mode,
            normal_adapter_required=effective_mode is BackendMode.NORMAL,
            public_origins=self.config.public_origins,
            platform_kind=self.config.platform_kind,
            backend_transport=self.config.backend_transport,
        )
        self._mode = effective_mode
        try:
            self._backend = self._backend_factory(spec, self.service_state)
            self._backend.start()
            if self._external_gateway_required:
                assert self._process_factory is not None
                self._caddy = self._process_factory(self.config, caddy_command(self.config))
        except Exception as exc:
            return self._unexpected_failure(f"runtime launch failed: {type(exc).__name__}")

        deadline = self._clock() + READINESS_DEADLINE_SECONDS
        while True:
            if self.service_state.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT:
                self.stop()
                return RuntimeOutcome(RuntimeOutcomeState.STOPPED)
            if not self._components_alive():
                if (
                    effective_mode is BackendMode.SETUP_SHELL
                    and self._external_gateway_required
                    and self._caddy is not None
                    and self._caddy.poll() is not None
                    and self.service_state.snapshot().saved_endpoint is None
                ):
                    return self._unclaimed_setup_gateway_failure(
                        "setup gateway exited before endpoint claim"
                    )
                return self._unexpected_failure("runtime component exited during readiness")
            gateway_ready = (
                not self._external_gateway_required
                or (self._caddy is not None and self._caddy.is_ready())
            )
            if self._backend.is_ready() and gateway_ready:
                try:
                    self.service_state.claim_endpoint(self.config.origin)
                except EndpointUnavailable as exc:
                    return self._endpoint_claim_failure(str(exc))
                except Exception as exc:
                    return self._endpoint_claim_failure(
                        f"endpoint claim failed safely: {type(exc).__name__}"
                    )
                if effective_mode is BackendMode.NORMAL:
                    self.service_state.mark_ready()
                    return RuntimeOutcome(RuntimeOutcomeState.READY)
                if effective_mode is BackendMode.RECOVERY_SHELL:
                    return RuntimeOutcome(RuntimeOutcomeState.RECOVERY_READY)
                self.service_state.mark_ready()
                return RuntimeOutcome(RuntimeOutcomeState.SHELL_READY)
            now = self._clock()
            if now >= deadline:
                return self._unexpected_failure("runtime readiness exceeded 10 seconds")
            self._sleep(min(self._poll_interval, deadline - now))

    def supervise_once(self) -> RuntimeOutcome:
        if self._backend is None or self._mode is None:
            raise RuntimeStateError("WebUI runtime is not active")
        if self._external_gateway_required and self._caddy is None:
            raise RuntimeStateError("WebUI runtime is not active")
        if self.service_state.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT:
            self.stop()
            return RuntimeOutcome(RuntimeOutcomeState.STOPPED)
        if not self._components_alive():
            return self._unexpected_failure("runtime component exited unexpectedly")
        if self._mode is BackendMode.NORMAL:
            self.service_state.observe_ready()
            return RuntimeOutcome(RuntimeOutcomeState.READY)
        if self._mode is BackendMode.RECOVERY_SHELL:
            return RuntimeOutcome(RuntimeOutcomeState.RECOVERY_READY)
        self.service_state.observe_ready()
        return RuntimeOutcome(RuntimeOutcomeState.SHELL_READY)

    def stop(self) -> bool:
        """Request graceful stop using one 30-second budget and never force-kill."""

        if self._backend is None and self._caddy is None:
            self._mode = None
            return True
        deadline = self._clock() + STOP_DRAIN_DEADLINE_SECONDS
        backend = self._backend
        caddy = self._caddy

        if caddy is not None and caddy.poll() is None:
            caddy.terminate()
        if backend is not None:
            backend.begin_drain()

        caddy_stopped = True
        if caddy is not None and caddy.poll() is None:
            caddy_stopped = caddy.wait_stopped(max(0.0, deadline - self._clock()))
        backend_stopped = True
        if backend is not None:
            backend_stopped = backend.wait_stopped(max(0.0, deadline - self._clock()))

        stopped = bool(caddy_stopped and backend_stopped and self._clock() <= deadline)
        if stopped:
            self._backend = None
            self._caddy = None
            self._mode = None
        return stopped

    def run(
        self,
        *,
        mode: BackendMode = BackendMode.NORMAL,
        should_stop: StopPredicate = lambda: False,
    ) -> RuntimeOutcome:
        """Run supervision/retry policy synchronously until stopped or failed."""

        while True:
            outcome = self.start(mode=mode)
            if outcome.state is RuntimeOutcomeState.RETRY_SCHEDULED:
                if not self._wait_for_retry(outcome.retry_at, should_stop):
                    self.stop()
                    return RuntimeOutcome(RuntimeOutcomeState.STOPPED)
                continue
            if outcome.state in {RuntimeOutcomeState.STOPPED, RuntimeOutcomeState.FAILED}:
                return outcome

            while True:
                if should_stop():
                    if not self.stop():
                        return RuntimeOutcome(RuntimeOutcomeState.FAILED, detail="runtime drain exceeded 30 seconds")
                    return RuntimeOutcome(RuntimeOutcomeState.STOPPED)
                outcome = self.supervise_once()
                if outcome.state in {
                    RuntimeOutcomeState.READY,
                    RuntimeOutcomeState.SHELL_READY,
                    RuntimeOutcomeState.RECOVERY_READY,
                }:
                    self._sleep(self._poll_interval)
                    continue
                if outcome.state is RuntimeOutcomeState.RETRY_SCHEDULED:
                    if not self._wait_for_retry(outcome.retry_at, should_stop):
                        self.stop()
                        return RuntimeOutcome(RuntimeOutcomeState.STOPPED)
                    break
                return outcome

    def _unexpected_failure(self, detail: str) -> RuntimeOutcome:
        if not self.stop():
            if self.service_state.snapshot().status is not ServiceStatus.RECOVERY_REQUIRED:
                self.service_state.mark_failed()
            return RuntimeOutcome(
                RuntimeOutcomeState.FAILED,
                detail="runtime could not drain safely after failure",
            )
        retry = self.service_state.record_crash()
        if retry is None:
            return RuntimeOutcome(RuntimeOutcomeState.FAILED, detail=detail)
        return RuntimeOutcome(
            RuntimeOutcomeState.RETRY_SCHEDULED,
            retry_at=retry.retry_at,
            detail=detail,
        )

    def _unclaimed_setup_gateway_failure(self, detail: str) -> RuntimeOutcome:
        """Fail one provisional setup gateway without consuming crash budget.

        Linux first-install port selection is owned by the managed installer.
        Before an endpoint has been claimed, a Caddy child may exit because the
        candidate port belongs to another local process.  The installer's exact
        HTTPS probe classifies that candidate and decides whether to continue.
        Treating this expected pre-claim candidate rejection as a service crash
        would leak retry-budget residue into the next candidate.
        """

        if not self.stop():
            detail = f"{detail}; runtime could not drain safely"
        return RuntimeOutcome(RuntimeOutcomeState.FAILED, detail=detail)

    def _endpoint_claim_failure(self, detail: str) -> RuntimeOutcome:
        drained = self.stop()
        if self.service_state.snapshot().status is not ServiceStatus.RECOVERY_REQUIRED:
            self.service_state.mark_failed()
        if not drained:
            detail = f"{detail}; runtime could not drain safely"
        return RuntimeOutcome(RuntimeOutcomeState.FAILED, detail=detail)

    def _components_alive(self) -> bool:
        if self._backend is None or not self._backend.is_alive():
            return False
        if not self._external_gateway_required:
            return True
        return self._caddy is not None and self._caddy.poll() is None

    def _wait_for_retry(self, retry_at: float | None, should_stop: StopPredicate) -> bool:
        if retry_at is None:
            return False
        while self._clock() < retry_at:
            if should_stop() or self.service_state.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT:
                return False
            self._sleep(min(self._poll_interval, retry_at - self._clock()))
        return True


def _validate_config_stat(value: os.stat_result, owner_uid: int) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise RuntimeConfigError("runtime config must be a regular file")
    if value.st_uid != owner_uid:
        raise RuntimeConfigError("runtime config must be owned by the current owner")
    if stat.S_IMODE(value.st_mode) & 0o077:
        raise RuntimeConfigError("runtime config must be owner-private with no group or world access")


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeConfigError(f"runtime config contains duplicate field: {key}")
        result[key] = value
    return result


def _validate_absolute_posix_path(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise RuntimeConfigError(f"{label} must be an absolute POSIX path")
    if any(ch in value for ch in ("\n", "\r")):
        raise RuntimeConfigError(f"{label} must not contain line breaks")
    if ".." in PurePosixPath(value).parts:
        raise RuntimeConfigError(f"{label} must not contain parent traversal")


def _validate_absolute_windows_local_path(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RuntimeConfigError(f"{label} must be an absolute local-drive Windows path")
    if any(ch in value for ch in ("\n", "\r")):
        raise RuntimeConfigError(f"{label} must not contain line breaks")
    if "/" in value or value.startswith("\\\\") or value.startswith(("\\?\\", "\\.\\")):
        raise RuntimeConfigError(f"{label} must be an absolute local-drive Windows path")
    path = PureWindowsPath(value)
    drive = path.drive
    if (
        not path.is_absolute()
        or len(drive) != 2
        or drive[1] != ":"
        or not drive[0].isalpha()
        or ".." in path.parts
        or str(path) != value
    ):
        raise RuntimeConfigError(f"{label} must be a canonical absolute local-drive Windows path")
    for part in path.parts[1:]:
        if part in {".", ".."} or ":" in part or part.endswith((" ", ".")):
            raise RuntimeConfigError(f"{label} contains an unsafe Windows path component")


def _validate_origin(origin: str) -> None:
    if not isinstance(origin, str):
        raise RuntimeConfigError("origin must be a string")
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeConfigError("origin contains an invalid port") from exc
    local_http = (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None
        and _PORT_MIN <= port <= _PORT_MAX
        and origin == f"http://127.0.0.1:{port}"
    )
    legacy_https = (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and port is not None
        and _PORT_MIN <= port <= _PORT_MAX
        and _HOST_RE.fullmatch(parsed.hostname) is not None
        and origin == f"https://{parsed.hostname}:{port}"
    )
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not (local_http or legacy_https)
    ):
        raise RuntimeConfigError(
            "origin must be canonical FactLane loopback HTTP or legacy managed HTTPS"
        )


def _validate_public_origin(origin: str) -> None:
    if not isinstance(origin, str) or not origin or len(origin) > 2048:
        raise RuntimeConfigError("public origin must be a bounded HTTPS origin")
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeConfigError("public origin contains an invalid port") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.hostname != parsed.hostname.lower()
        or any(ord(ch) < 33 or ord(ch) > 126 for ch in parsed.netloc)
    ):
        raise RuntimeConfigError("public origin must be one exact canonical HTTPS origin")
    expected = f"https://{parsed.hostname}" + (f":{port}" if port is not None else "")
    if origin != expected:
        raise RuntimeConfigError("public origin must use canonical HTTPS origin form")
