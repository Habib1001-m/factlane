from __future__ import annotations

import html
import importlib
import json
import math
import os
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote, urlsplit

from .owner_auth import PendingEnrollment
from .webui_install import (
    EndpointState,
    InstallEnvironment,
    InstallerStepStatus,
    ManagedServiceActivationResult,
    ProductionLinuxInstallerBackend,
    SetupApplyResult,
    SetupConsent,
    SetupReceipt,
    SetupRequest,
    WEBUI_PORT_MIN,
    WEBUI_SERVICE_NAME,
    apply_webui_setup,
    consent_requirements,
)
from .webui_runtime import (
    BackendFactory,
    ProcessFactory,
    RuntimeConfigError,
    RuntimeOutcomeState,
    WebUIRuntimeConfig,
    WebUIRuntimeRunner,
    default_runtime_config_path,
    load_runtime_config,
)
from .webui_service import ServiceStatus, WebUIServiceState


_PRODUCTION_COMPOSITION_MODULE = "factlane.webui_app"
_PRODUCTION_BACKEND_FACTORY = "create_owner_runtime_backend"
_PRODUCTION_PROCESS_FACTORY = "create_caddy_process"
_PRODUCTION_BROWSER_LAUNCHER = "open_owner_browser"
_CONTROL_SOCKET_NAME = "owner-control.sock"
_BACKEND_SOCKET_NAME = "owner-http.sock"
_SERVICE_STATE_NAME = "webui-service.json"
_CONTROL_MAX_FRAME_BYTES = 64 * 1024
_CONTROL_TIMEOUT_SECONDS = 10.0
_SENSITIVE_CONTROL_RESPONSE_TIMEOUT_SECONDS = 125.0
_LOCAL_ENROLLMENT_DEVICE_LABEL = "FactLane local browser"
_LOCAL_ENROLLMENT_TIMEOUT_SECONDS = 5 * 60.0
_LOCAL_ENROLLMENT_POLL_INTERVAL_SECONDS = 0.1


class WebUIPlatformError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class OwnerPaths:
    owner_config_home: str
    service_config_path: str
    caddy_config_path: str
    user_unit_path: str
    runtime_dir: str
    backend_socket: str
    native_control_socket: str
    state_dir: str
    caddy_state_dir: str
    browser_profile_dir: str


class LauncherControlClient(Protocol):
    def status(self) -> Mapping[str, Any]: ...

    def start(self, *, request_id: str) -> Mapping[str, Any]: ...

    def stop(self, *, request_id: str) -> Mapping[str, Any]: ...

    def restart(self, *, request_id: str) -> Mapping[str, Any]: ...

    def begin_local_enrollment(self, *, device_label: str) -> PendingEnrollment: ...

    def approve_pending(self, *, locator: str) -> None: ...

    def approve_pending_sensitive(self) -> None: ...


ControlClientFactory = Callable[[str, int], LauncherControlClient]
BrowserLauncher = Callable[[str], bool]
RunnerFactory = Callable[..., Any]
ServiceStateFactory = Callable[[str], Any]
RequestIdFactory = Callable[[], str]
ZenityCommandRunner = Callable[[tuple[str, ...]], int]
ReadOnlyCommandRunner = Callable[[tuple[str, ...]], tuple[int, str]]
LingeringDetector = Callable[[str], bool]
SetupApplyExecutor = Callable[[SetupReceipt, SetupConsent], SetupApplyResult]
SystemdCommandRunner = Callable[[tuple[str, ...]], tuple[int, str]]


@dataclass(frozen=True)
class SystemdUnitObservation:
    active: bool
    enabled: bool
    active_state: str
    enabled_state: str


class SystemdUnitManager(Protocol):
    def observe(self) -> SystemdUnitObservation: ...

    def start(self) -> None: ...


class SystemdUserUnitManager:
    """Fixed-argv user-systemd observer/start boundary for the exact WebUI unit."""

    def __init__(
        self,
        systemctl_path: str,
        *,
        command_runner: SystemdCommandRunner | None = None,
    ) -> None:
        if not isinstance(systemctl_path, str) or not os.path.isabs(systemctl_path):
            raise ValueError("systemctl_path must be an absolute path")
        self._systemctl_path = systemctl_path
        self._command_runner = command_runner or _run_readonly_argv

    def observe(self) -> SystemdUnitObservation:
        active_rc, active_out = self._command_runner(
            (
                self._systemctl_path,
                "--user",
                "is-active",
                WEBUI_SERVICE_NAME,
            )
        )
        enabled_rc, enabled_out = self._command_runner(
            (
                self._systemctl_path,
                "--user",
                "is-enabled",
                WEBUI_SERVICE_NAME,
            )
        )
        active_state = active_out.strip().casefold()
        enabled_state = enabled_out.strip().casefold()
        if active_state not in {
            "active",
            "inactive",
            "failed",
            "activating",
            "deactivating",
            "reloading",
        }:
            raise WebUIPlatformError(
                "SYSTEMD_STATUS_UNAVAILABLE",
                "WebUI user unit active state could not be determined safely",
            )
        if active_state == "active" and active_rc != 0:
            raise WebUIPlatformError(
                "SYSTEMD_STATUS_UNAVAILABLE",
                "WebUI user unit active state was contradictory",
            )
        if enabled_state not in {
            "enabled",
            "enabled-runtime",
            "disabled",
            "static",
            "indirect",
            "masked",
            "masked-runtime",
            "generated",
            "transient",
            "linked",
            "linked-runtime",
            "alias",
        }:
            raise WebUIPlatformError(
                "SYSTEMD_STATUS_UNAVAILABLE",
                "WebUI user unit enablement state could not be determined safely",
            )
        if enabled_state in {"enabled", "enabled-runtime"} and enabled_rc != 0:
            raise WebUIPlatformError(
                "SYSTEMD_STATUS_UNAVAILABLE",
                "WebUI user unit enablement state was contradictory",
            )
        return SystemdUnitObservation(
            active=active_state == "active",
            enabled=enabled_state in {"enabled", "enabled-runtime"},
            active_state=active_state,
            enabled_state=enabled_state,
        )

    def start(self) -> None:
        returncode, _stdout = self._command_runner(
            (
                self._systemctl_path,
                "--user",
                "start",
                WEBUI_SERVICE_NAME,
            )
        )
        if returncode != 0:
            raise WebUIPlatformError(
                "SYSTEMD_START_FAILED",
                "WebUI user unit failed to start",
            )


class SetupConsentProvider(Protocol):
    def acquire(
        self,
        request: SetupRequest,
        environment: InstallEnvironment,
    ) -> SetupConsent: ...


class ZenitySetupConsentProvider:
    """One explicit fixed-argv setup review; denial/failure never implies consent."""

    def __init__(
        self,
        zenity_path: str,
        *,
        command_runner: ZenityCommandRunner | None = None,
    ) -> None:
        if not isinstance(zenity_path, str) or not os.path.isabs(zenity_path):
            raise ValueError("zenity_path must be an absolute path")
        self._zenity_path = zenity_path
        self._command_runner = command_runner or _run_zenity_argv

    def acquire(
        self,
        request: SetupRequest,
        environment: InstallEnvironment,
    ) -> SetupConsent:
        denied = SetupConsent(
            hostname_mapping=False,
            browser_profile_trust=False,
            user_lingering=False,
        )
        if environment.zenity_path != self._zenity_path:
            return denied
        requirements = consent_requirements(
            lingering_required=request.lingering_required
        )
        review = (
            "FactLane WebUI requires explicit consent for these installation changes:\n\n"
            + "\n".join(f"• {item.explanation}" for item in requirements)
        )
        argv = (
            self._zenity_path,
            "--question",
            "--title=FactLane WebUI setup",
            f"--text={html.escape(review, quote=False)}",
            "--ok-label=Apply reviewed changes",
            "--cancel-label=Cancel",
        )
        try:
            approved = self._command_runner(argv) == 0
        except Exception:
            return denied
        if not approved:
            return denied
        return SetupConsent(
            hostname_mapping=True,
            browser_profile_trust=True,
            user_lingering=request.lingering_required,
        )


def acquire_default_setup_consent(
    request: SetupRequest,
    environment: InstallEnvironment,
) -> SetupConsent:
    if environment.zenity_path is None:
        return SetupConsent(
            hostname_mapping=False,
            browser_profile_trust=False,
            user_lingering=False,
        )
    return ZenitySetupConsentProvider(environment.zenity_path).acquire(
        request,
        environment,
    )


def detect_lingering_required(
    installing_user: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
    command_runner: ReadOnlyCommandRunner | None = None,
) -> bool:
    """Return False only on positive read-only proof that lingering is enabled."""

    loginctl = which("loginctl")
    if not loginctl or not os.path.isabs(loginctl):
        return True
    runner = command_runner or _run_readonly_argv
    argv = (
        loginctl,
        "show-user",
        "--property=Linger",
        "--value",
        "--",
        installing_user,
    )
    try:
        returncode, stdout = runner(argv)
    except Exception:
        return True
    return not (returncode == 0 and stdout.strip().casefold() == "yes")


def default_owner_paths(
    *,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    owner_uid: int | None = None,
) -> OwnerPaths:
    environment = os.environ if environ is None else environ
    uid = os.getuid() if owner_uid is None else owner_uid
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise WebUIPlatformError("OWNER_IDENTITY_INVALID", "owner UID must be a non-negative integer")
    owner_home = Path.home() if home is None else Path(home)
    if not owner_home.is_absolute():
        raise WebUIPlatformError("OWNER_HOME_INVALID", "owner home must be an absolute path")

    config_home = _xdg_path(
        environment,
        "XDG_CONFIG_HOME",
        fallback=owner_home / ".config",
    )
    state_home = _xdg_path(
        environment,
        "XDG_STATE_HOME",
        fallback=owner_home / ".local" / "state",
    )
    runtime_home = _xdg_path(
        environment,
        "XDG_RUNTIME_DIR",
        fallback=Path(f"/run/user/{uid}"),
    )

    config_dir = config_home / "factlane"
    runtime_dir = runtime_home / "factlane"
    state_dir = state_home / "factlane"
    return OwnerPaths(
        owner_config_home=str(config_home),
        service_config_path=str(config_dir / "webui.json"),
        caddy_config_path=str(config_dir / "Caddyfile"),
        user_unit_path=str(config_home / "systemd" / "user" / "factlane-webui.service"),
        runtime_dir=str(runtime_dir),
        backend_socket=str(runtime_dir / _BACKEND_SOCKET_NAME),
        native_control_socket=str(runtime_dir / _CONTROL_SOCKET_NAME),
        state_dir=str(state_dir),
        caddy_state_dir=str(state_dir / "caddy"),
        browser_profile_dir=str(state_dir / "browser"),
    )


def build_default_setup_request(
    *,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    owner_uid: int | None = None,
    installing_user: str | None = None,
    factlane_executable: str | None = None,
    installation_id_factory: Callable[[], str] = lambda: secrets.token_hex(8),
    lingering_detector: LingeringDetector = detect_lingering_required,
) -> SetupRequest:
    environment = os.environ if environ is None else environ
    uid = os.getuid() if owner_uid is None else owner_uid
    paths = default_owner_paths(environ=environment, home=home, owner_uid=uid)

    user = installing_user
    if user is None:
        try:
            pwd_module = importlib.import_module("pwd")
            user = pwd_module.getpwuid(uid).pw_name
        except (ImportError, KeyError, OSError) as exc:
            raise WebUIPlatformError(
                "OWNER_ACCOUNT_UNAVAILABLE",
                "installing Owner account could not be resolved",
            ) from exc
    executable = factlane_executable or shutil.which("factlane")
    if not executable:
        invoked = Path(sys.argv[0])
        if invoked.is_absolute():
            executable = str(invoked)
    if not executable or not os.path.isabs(executable):
        raise WebUIPlatformError(
            "FACTLANE_EXECUTABLE_UNAVAILABLE",
            "installed FactLane executable could not be resolved to an absolute path",
        )
    lingering_required = lingering_detector(user)

    existing = _load_existing_runtime_config(paths.service_config_path, owner_uid=uid)
    if existing is None:
        installation_id = installation_id_factory()
        saved_port = WEBUI_PORT_MIN
        db = profile = host_id = None
    else:
        parsed = urlsplit(existing.origin)
        hostname = parsed.hostname
        if hostname is None or not hostname.startswith("factlane-") or not hostname.endswith(".test"):
            raise WebUIPlatformError(
                "RUNTIME_CONFIG_INVALID",
                "existing runtime origin does not contain a canonical FactLane installation ID",
            )
        installation_id = hostname[len("factlane-") : -len(".test")]
        assert parsed.port is not None
        saved_port = parsed.port
        db, profile, host_id = existing.db, existing.profile, existing.host_id

    return SetupRequest(
        installation_id=installation_id,
        saved_port=saved_port,
        lingering_required=lingering_required,
        installing_user=user,
        factlane_executable=executable,
        owner_config_home=paths.owner_config_home,
        service_config_path=paths.service_config_path,
        caddy_config_path=paths.caddy_config_path,
        user_unit_path=paths.user_unit_path,
        backend_socket=paths.backend_socket,
        runtime_dir=paths.runtime_dir,
        state_dir=paths.state_dir,
        caddy_state_dir=paths.caddy_state_dir,
        browser_profile_dir=paths.browser_profile_dir,
        db=db,
        profile=profile,
        host_id=host_id,
    )


def build_default_setup_executor(
    request: SetupRequest,
    environment: InstallEnvironment,
    *,
    which: Callable[[str], str | None] = shutil.which,
    command_runner: Callable[[tuple[str, ...]], int] | None = None,
    service_state_reader: Callable[[], Any] | None = None,
    activation_probe: Callable[[str], ManagedServiceActivationResult] | None = None,
    service_state_factory: Callable[[str], Any] = WebUIServiceState,
) -> SetupApplyExecutor:
    """Compose only the already-defined owner-side setup backend.

    Privileged hostname/lingering changes and browser trust deliberately remain
    typed incomplete results from ProductionLinuxInstallerBackend.
    """

    systemctl_path = which("systemctl")
    if (
        environment.system != "Linux"
        or not environment.systemd
        or systemctl_path is None
        or not os.path.isabs(systemctl_path)
    ):
        def unavailable(
            _receipt: SetupReceipt,
            _consent: SetupConsent,
        ) -> SetupApplyResult:
            return SetupApplyResult(
                status=InstallerStepStatus.FAILED,
                endpoint_state=EndpointState.PROPOSED,
                detail="systemctl executable is unavailable for WebUI setup",
            )

        return unavailable

    state_path = str(PurePosixPath(request.state_dir) / _SERVICE_STATE_NAME)
    state_holder: list[Any] = []

    def default_state_reader() -> Any:
        if not state_holder:
            state_holder.append(service_state_factory(state_path))
        return state_holder[0].snapshot()

    backend_kwargs: dict[str, Any] = {
        "systemctl_path": systemctl_path,
        "service_state_reader": service_state_reader or default_state_reader,
    }
    if command_runner is not None:
        backend_kwargs["command_runner"] = command_runner
    if activation_probe is not None:
        backend_kwargs["activation_probe"] = activation_probe
    backend = ProductionLinuxInstallerBackend(**backend_kwargs)

    def execute(
        receipt: SetupReceipt,
        consent: SetupConsent,
    ) -> SetupApplyResult:
        return apply_webui_setup(receipt, consent, backend)

    return execute


class NativeLauncherClient:
    """Typed same-UID client for worker4's private native-control AF_UNIX server."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        owner_uid: int | None = None,
        timeout_seconds: float = _CONTROL_TIMEOUT_SECONDS,
        socket_factory: Callable[[int, int], socket.socket] = socket.socket,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        if isinstance(self.owner_uid, bool) or not isinstance(self.owner_uid, int) or self.owner_uid < 0:
            raise ValueError("owner_uid must be a non-negative integer")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or timeout_seconds > _CONTROL_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be greater than zero and at most 10 seconds")
        self.timeout_seconds = float(timeout_seconds)
        self._socket_factory = socket_factory

    def status(self) -> Mapping[str, Any]:
        return self._service_request({"op": "SERVICE_STATUS"})

    def start(self, *, request_id: str) -> Mapping[str, Any]:
        return self._service_request({"op": "SERVICE_START", "request_id": request_id})

    def stop(self, *, request_id: str) -> Mapping[str, Any]:
        return self._service_request({"op": "SERVICE_STOP", "request_id": request_id})

    def restart(self, *, request_id: str) -> Mapping[str, Any]:
        return self._service_request({"op": "SERVICE_RESTART", "request_id": request_id})

    def begin_local_enrollment(
        self,
        *,
        device_label: str,
    ) -> PendingEnrollment:
        if (
            not isinstance(device_label, str)
            or not device_label
            or len(device_label) > 256
        ):
            raise ValueError("device_label must be non-empty bounded text")
        response = self._exchange(
            {
                "op": "BEGIN_LOCAL_ENROLLMENT",
                "device_label": device_label,
            }
        )
        self._require_ok(response)
        if set(response) != {
            "status",
            "locator",
            "verification_phrase",
            "expires_at",
        }:
            raise WebUIPlatformError(
                "CONTROL_RESPONSE_INVALID",
                "native enrollment response fields are invalid",
            )
        locator = response.get("locator")
        phrase = response.get("verification_phrase")
        expires_at = response.get("expires_at")
        if (
            not isinstance(locator, str)
            or not locator
            or len(locator) > 512
            or not isinstance(phrase, str)
            or not phrase
            or len(phrase) > 512
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
        ):
            raise WebUIPlatformError(
                "CONTROL_RESPONSE_INVALID",
                "native enrollment response is invalid",
            )
        return PendingEnrollment(
            locator=locator,
            verification_phrase=phrase,
            expires_at=float(expires_at),
        )

    def approve_pending(self, *, locator: str) -> None:
        if not isinstance(locator, str) or not locator or len(locator) > 512:
            raise ValueError("locator must be non-empty bounded text")
        response = self._exchange(
            {
                "op": "APPROVE_PENDING",
                "locator": locator,
            }
        )
        self._require_ok(response)
        if set(response) != {"status", "locator"} or response.get("locator") != locator:
            raise WebUIPlatformError(
                "CONTROL_RESPONSE_INVALID",
                "native approval response is invalid",
            )

    def approve_pending_sensitive(self) -> None:
        response = self._exchange(
            {"op": "APPROVE_PENDING_SENSITIVE"},
            response_timeout_seconds=_SENSITIVE_CONTROL_RESPONSE_TIMEOUT_SECONDS,
        )
        self._require_ok(response)
        if response != {"status": "OK", "state": "SENSITIVE_APPROVED"}:
            raise WebUIPlatformError(
                "CONTROL_RESPONSE_INVALID",
                "native sensitive-approval response is invalid",
            )

    def _service_request(self, request: dict[str, Any]) -> Mapping[str, Any]:
        response = self._exchange(request)
        self._require_ok(response)
        service = response.get("service")
        if not isinstance(service, Mapping):
            raise WebUIPlatformError(
                "CONTROL_RESPONSE_INVALID",
                "native WebUI control response did not contain service state",
            )
        return dict(service)

    @staticmethod
    def _require_ok(response: Mapping[str, Any]) -> None:
        if response.get("status") == "OK":
            return
        code = response.get("code")
        safe_code = code if isinstance(code, str) and code else "CONTROL_OPERATION_FAILED"
        retryable = response.get("retryable") is True
        raise WebUIPlatformError(
            safe_code,
            f"native WebUI control request failed: {safe_code}",
            retryable=retryable,
        )

    def _exchange(
        self,
        request: dict[str, Any],
        *,
        response_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self._validate_socket_path()
        raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if not raw or len(raw) > _CONTROL_MAX_FRAME_BYTES:
            raise WebUIPlatformError("CONTROL_REQUEST_INVALID", "native control request is too large")
        client = self._socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(self.timeout_seconds)
            client.connect(str(self.socket_path))
            self._validate_server_peer(client)
            client.sendall(struct.pack("!I", len(raw)) + raw)
            if response_timeout_seconds is not None:
                client.settimeout(response_timeout_seconds)
            length = struct.unpack("!I", self._recv_exact(client, 4))[0]
            if length == 0 or length > _CONTROL_MAX_FRAME_BYTES:
                raise WebUIPlatformError(
                    "CONTROL_RESPONSE_INVALID",
                    "native control response frame is invalid",
                )
            payload = self._recv_exact(client, length)
        except WebUIPlatformError:
            raise
        except (OSError, TimeoutError, socket.timeout) as exc:
            raise WebUIPlatformError(
                "CONTROL_UNAVAILABLE",
                "native WebUI control socket is unavailable",
            ) from exc
        finally:
            client.close()
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebUIPlatformError(
                "CONTROL_RESPONSE_INVALID",
                "native control response is not valid JSON",
            ) from exc
        if not isinstance(decoded, dict):
            raise WebUIPlatformError(
                "CONTROL_RESPONSE_INVALID",
                "native control response must be a JSON object",
            )
        return decoded

    def _validate_socket_path(self) -> None:
        try:
            parent = self.socket_path.parent.lstat()
            endpoint = self.socket_path.lstat()
        except OSError as exc:
            raise WebUIPlatformError(
                "CONTROL_UNAVAILABLE",
                "native WebUI control socket is unavailable",
            ) from exc
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != self.owner_uid
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise WebUIPlatformError(
                "CONTROL_SOCKET_UNSAFE",
                "native WebUI control socket directory is not owner-private",
            )
        if (
            not stat.S_ISSOCK(endpoint.st_mode)
            or endpoint.st_uid != self.owner_uid
            or stat.S_IMODE(endpoint.st_mode) != 0o600
        ):
            raise WebUIPlatformError(
                "CONTROL_SOCKET_UNSAFE",
                "native WebUI control socket is not an owner-private socket",
            )

    def _validate_server_peer(self, client: socket.socket) -> None:
        option = getattr(socket, "SO_PEERCRED", None)
        if option is None:
            raise WebUIPlatformError(
                "CONTROL_PEER_IDENTITY_UNAVAILABLE",
                "SO_PEERCRED is required for native WebUI control",
            )
        try:
            raw = client.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
        except (OSError, struct.error) as exc:
            raise WebUIPlatformError(
                "CONTROL_PEER_IDENTITY_UNAVAILABLE",
                "native WebUI control peer identity could not be verified",
            ) from exc
        if uid != self.owner_uid:
            raise WebUIPlatformError(
                "CONTROL_PEER_UID_DENIED",
                "native WebUI control server is owned by a different user",
            )

    @staticmethod
    def _recv_exact(client: socket.socket, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            chunk = client.recv(remaining)
            if not chunk:
                raise WebUIPlatformError(
                    "CONTROL_RESPONSE_INCOMPLETE",
                    "native WebUI control response ended early",
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


class ProductionWebUIController:
    def __init__(
        self,
        *,
        paths: OwnerPaths | None = None,
        environ: Mapping[str, str] | None = None,
        home: str | os.PathLike[str] | None = None,
        owner_uid: int | None = None,
        runtime_runner_factory: RunnerFactory = WebUIRuntimeRunner,
        backend_factory: BackendFactory | None = None,
        process_factory: ProcessFactory | None = None,
        service_state_factory: ServiceStateFactory = WebUIServiceState,
        control_client_factory: ControlClientFactory | None = None,
        systemd_manager: SystemdUnitManager | None = None,
        browser_launcher: BrowserLauncher | None = None,
        request_id_factory: RequestIdFactory = lambda: secrets.token_urlsafe(18),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        startup_timeout: float = 10.0,
        startup_poll_interval: float = 0.1,
        enrollment_timeout: float = _LOCAL_ENROLLMENT_TIMEOUT_SECONDS,
        enrollment_poll_interval: float = _LOCAL_ENROLLMENT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.environ = os.environ if environ is None else environ
        self.home = Path.home() if home is None else Path(home)
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        self.paths = paths or default_owner_paths(
            environ=self.environ,
            home=self.home,
            owner_uid=self.owner_uid,
        )
        self._runtime_runner_factory = runtime_runner_factory
        self._backend_factory = backend_factory
        self._process_factory = process_factory
        self._service_state_factory = service_state_factory
        self._control_client_factory = control_client_factory or (
            lambda path, uid: NativeLauncherClient(path, owner_uid=uid)
        )
        self._systemd_manager = systemd_manager or _default_systemd_user_manager()
        self._browser_launcher = browser_launcher
        self._request_id_factory = request_id_factory
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive")
        if startup_poll_interval <= 0:
            raise ValueError("startup_poll_interval must be positive")
        if enrollment_timeout <= 0:
            raise ValueError("enrollment_timeout must be positive")
        if enrollment_poll_interval <= 0:
            raise ValueError("enrollment_poll_interval must be positive")
        self._clock = clock
        self._sleep = sleep
        self._startup_timeout = startup_timeout
        self._startup_poll_interval = startup_poll_interval
        self._enrollment_timeout = enrollment_timeout
        self._enrollment_poll_interval = enrollment_poll_interval
        self._persistent_state: Any | None = None

    def dispatch(self, action: str) -> Mapping[str, Any]:
        try:
            if action == "serve":
                return self._serve()
            if action == "open":
                return self._open()
            if action in {"status", "start", "stop", "restart"}:
                return self._control(action)
            raise WebUIPlatformError("UNKNOWN_ACTION", "unsupported WebUI platform action")
        except RuntimeConfigError as exc:
            return {
                "status": "ERROR",
                "error_code": "RUNTIME_CONFIG_ERROR",
                "message": str(exc),
            }
        except WebUIPlatformError as exc:
            return {
                "status": "ERROR",
                "error_code": exc.code,
                "message": str(exc),
            }
        except Exception:
            return {
                "status": "ERROR",
                "error_code": "PLATFORM_OPERATION_FAILED",
                "message": "FactLane WebUI platform operation failed safely",
            }

    def _serve(self) -> Mapping[str, Any]:
        config_path = default_runtime_config_path(environ=self.environ, home=self.home)
        config = load_runtime_config(config_path, owner_uid=self.owner_uid)
        backend_factory, process_factory = self._runtime_factories()
        service_state_path = str(PurePosixPath(config.state_dir) / _SERVICE_STATE_NAME)
        service_state = self._service_state_factory(service_state_path)
        runner = self._runtime_runner_factory(
            config,
            service_state,
            backend_factory=backend_factory,
            process_factory=process_factory,
        )
        outcome = runner.run()
        state = getattr(outcome.state, "value", outcome.state)
        if outcome.state is RuntimeOutcomeState.FAILED:
            return {
                "status": "ERROR",
                "error_code": "RUNTIME_FAILED",
                "state": state,
                "message": outcome.detail or "FactLane WebUI runtime failed",
            }
        result: dict[str, Any] = {"status": "OK", "state": state}
        if outcome.retry_at is not None:
            result["retry_at"] = outcome.retry_at
        if outcome.detail is not None:
            result["detail"] = outcome.detail
        return result

    def _runtime_factories(self) -> tuple[BackendFactory, ProcessFactory]:
        if self._backend_factory is not None and self._process_factory is not None:
            return self._backend_factory, self._process_factory
        try:
            module = importlib.import_module(_PRODUCTION_COMPOSITION_MODULE)
        except ImportError as exc:
            raise WebUIPlatformError(
                "RUNTIME_COMPOSITION_UNAVAILABLE",
                (
                    "production WebUI runtime composition is unavailable; expected "
                    f"{_PRODUCTION_COMPOSITION_MODULE}"
                ),
            ) from exc
        backend_factory = self._backend_factory or getattr(
            module, _PRODUCTION_BACKEND_FACTORY, None
        )
        process_factory = self._process_factory or getattr(
            module, _PRODUCTION_PROCESS_FACTORY, None
        )
        if not callable(backend_factory) or not callable(process_factory):
            raise WebUIPlatformError(
                "RUNTIME_COMPOSITION_UNAVAILABLE",
                (
                    "production WebUI runtime composition must provide "
                    f"{_PRODUCTION_COMPOSITION_MODULE}.{_PRODUCTION_BACKEND_FACTORY} "
                    f"and {_PRODUCTION_COMPOSITION_MODULE}.{_PRODUCTION_PROCESS_FACTORY}"
                ),
            )
        return backend_factory, process_factory

    def _control(self, action: str) -> Mapping[str, Any]:
        if action == "status":
            return self._offline_status()

        manager = self._require_systemd_manager()
        unit = manager.observe()
        request_id = self._request_id_factory()

        if action == "stop":
            if unit.active:
                return self._native_live_control("stop", request_id)
            service = self._persistent_service_state().request_stop(request_id)
            return {
                "status": "OK",
                "unit": _unit_payload(unit),
                "service": _service_snapshot_payload(service),
            }

        if action == "restart":
            if unit.active:
                return self._native_live_control("restart", request_id)
            return self._offline_start(request_id)

        if action == "start":
            if unit.active:
                return self._native_live_control("start", request_id)
            return self._offline_start(request_id)

        raise WebUIPlatformError("UNKNOWN_ACTION", "unsupported WebUI control action")

    def _offline_status(self) -> Mapping[str, Any]:
        unit = self._require_systemd_manager().observe()
        service = self._persistent_service_state().snapshot()
        return {
            "status": "OK",
            "unit": _unit_payload(unit),
            "service": _service_snapshot_payload(service),
        }

    def _offline_start(self, request_id: str) -> Mapping[str, Any]:
        manager = self._require_systemd_manager()
        service_state = self._persistent_service_state()
        prepared = service_state.request_start(request_id)
        if prepared.status not in {
            ServiceStatus.STARTING,
            ServiceStatus.RECOVERY_REQUIRED,
            ServiceStatus.READY,
        }:
            raise WebUIPlatformError(
                "SERVICE_START_BLOCKED",
                "persistent WebUI service state does not permit explicit start",
            )
        service_state.mark_starting()
        manager.start()
        deadline = self._clock() + self._startup_timeout
        last_unit = manager.observe()
        last_service = service_state.snapshot()
        while True:
            if last_service.status in {
                ServiceStatus.READY,
                ServiceStatus.RECOVERY_REQUIRED,
            } and last_unit.active:
                return {
                    "status": "OK",
                    "unit": _unit_payload(last_unit),
                    "service": _service_snapshot_payload(last_service),
                }
            if last_service.status is ServiceStatus.FAILED:
                return {
                    "status": "ERROR",
                    "error_code": "SERVICE_START_FAILED",
                    "message": "WebUI service entered FAILED during explicit start",
                    "unit": _unit_payload(last_unit),
                    "service": _service_snapshot_payload(last_service),
                }
            now = self._clock()
            if now >= deadline:
                return {
                    "status": "ERROR",
                    "error_code": "SERVICE_START_TIMEOUT",
                    "message": "WebUI service did not become ready within the startup deadline",
                    "unit": _unit_payload(last_unit),
                    "service": _service_snapshot_payload(last_service),
                }
            self._sleep(min(self._startup_poll_interval, deadline - now))
            last_unit = manager.observe()
            last_service = service_state.snapshot()

    def _native_live_control(
        self,
        action: str,
        request_id: str,
    ) -> Mapping[str, Any]:
        client = self._control_client_factory(
            self.paths.native_control_socket,
            self.owner_uid,
        )
        if action == "start":
            method = client.start
        elif action == "stop":
            method = client.stop
        elif action == "restart":
            method = client.restart
        else:
            raise WebUIPlatformError(
                "UNKNOWN_ACTION",
                "unsupported live WebUI control action",
            )
        service = method(request_id=request_id)
        return {"status": "OK", "service": dict(service)}

    def _persistent_service_state(self) -> Any:
        if self._persistent_state is None:
            state_path = str(PurePosixPath(self.paths.state_dir) / _SERVICE_STATE_NAME)
            self._persistent_state = self._service_state_factory(state_path)
        return self._persistent_state

    def _require_systemd_manager(self) -> SystemdUnitManager:
        if self._systemd_manager is None:
            raise WebUIPlatformError(
                "SYSTEMD_USER_MANAGER_UNAVAILABLE",
                "systemd user-manager control is unavailable",
            )
        return self._systemd_manager

    def _open(self) -> Mapping[str, Any]:
        client = self._control_client_factory(
            self.paths.native_control_socket,
            self.owner_uid,
        )
        service = client.status()
        origin = service.get("saved_endpoint")
        if not isinstance(origin, str) or not _is_safe_origin(origin):
            raise WebUIPlatformError(
                "WEBUI_ENDPOINT_UNAVAILABLE",
                "WebUI service has no valid saved HTTPS endpoint to open",
            )
        state = service.get("status", service.get("state"))
        if state not in {"READY", "RECOVERY_REQUIRED"}:
            raise WebUIPlatformError(
                "WEBUI_NOT_RUNNING",
                "WebUI service is not available to open",
            )
        try:
            client.approve_pending_sensitive()
        except WebUIPlatformError as exc:
            if exc.code != "SENSITIVE_CHALLENGE_NONE":
                raise
        else:
            return {
                "status": "OK",
                "state": "SENSITIVE_APPROVED",
                "origin": origin,
            }
        enrollment = client.begin_local_enrollment(
            device_label=_LOCAL_ENROLLMENT_DEVICE_LABEL
        )
        # Keep the non-credential enrollment locator entirely in the URL fragment.
        # Fragments are not sent in the HTTP request, so the private backend can
        # continue rejecting every query string while the SPA consumes and
        # immediately scrubs the one-time local handoff value.
        connect_url = f"{origin}#connect={quote(enrollment.locator, safe='')}"
        launcher = self._browser_launcher or _resolve_production_browser_launcher()
        if not launcher(connect_url):
            raise WebUIPlatformError(
                "BROWSER_LAUNCH_FAILED",
                "supported browser launcher did not open the WebUI",
            )
        deadline = min(
            enrollment.expires_at,
            self._clock() + self._enrollment_timeout,
        )
        while True:
            if self._clock() >= deadline:
                raise WebUIPlatformError(
                    "PENDING_CLAIM_TIMEOUT",
                    "browser did not claim the local enrollment before its deadline",
                )
            try:
                client.approve_pending(locator=enrollment.locator)
                break
            except WebUIPlatformError as exc:
                if exc.code != "PENDING_NOT_CLAIMED" or not exc.retryable:
                    raise
            now = self._clock()
            if now >= deadline:
                raise WebUIPlatformError(
                    "PENDING_CLAIM_TIMEOUT",
                    "browser did not claim the local enrollment before its deadline",
                )
            self._sleep(min(self._enrollment_poll_interval, deadline - now))
        return {"status": "OK", "state": "OPENED", "origin": origin}


def build_default_platform_controller() -> ProductionWebUIController:
    return ProductionWebUIController()


def _default_systemd_user_manager() -> SystemdUserUnitManager | None:
    systemctl = shutil.which("systemctl")
    if not systemctl or not os.path.isabs(systemctl):
        return None
    return SystemdUserUnitManager(systemctl)


def _unit_payload(unit: SystemdUnitObservation) -> dict[str, Any]:
    return {
        "active": unit.active,
        "enabled": unit.enabled,
        "active_state": unit.active_state,
        "enabled_state": unit.enabled_state,
    }


def _service_snapshot_payload(snapshot: Any) -> dict[str, Any]:
    status = getattr(snapshot.status, "value", snapshot.status)
    return {
        "status": status,
        "saved_endpoint": snapshot.saved_endpoint,
        "stopped_boot_id": snapshot.stopped_boot_id,
        "intent_revision": snapshot.intent_revision,
        "crash_attempts": snapshot.crash_attempts,
        "next_retry_at": snapshot.next_retry_at,
        "ready_since": snapshot.ready_since,
        "maintenance_operation_id": snapshot.maintenance_operation_id,
    }


def _load_existing_runtime_config(
    path: str,
    *,
    owner_uid: int,
) -> WebUIRuntimeConfig | None:
    candidate = Path(path)
    try:
        candidate.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WebUIPlatformError(
            "RUNTIME_CONFIG_ERROR",
            "existing runtime config could not be inspected safely",
        ) from exc
    try:
        return load_runtime_config(candidate, owner_uid=owner_uid)
    except RuntimeConfigError as exc:
        raise WebUIPlatformError("RUNTIME_CONFIG_ERROR", str(exc)) from exc


def _xdg_path(
    environment: Mapping[str, str],
    key: str,
    *,
    fallback: Path,
) -> Path:
    value = environment.get(key)
    if value:
        if not isinstance(value, str):
            raise WebUIPlatformError("XDG_PATH_INVALID", f"{key} must be text")
        if "\x00" in value or any(ch in value for ch in ("\n", "\r")):
            raise WebUIPlatformError("XDG_PATH_INVALID", f"{key} contains unsafe characters")
        candidate = Path(os.path.normpath(value))
        if candidate.is_absolute():
            return candidate
    return Path(os.path.normpath(os.fspath(fallback)))


def _run_zenity_argv(argv: tuple[str, ...]) -> int:
    completed = subprocess.run(
        argv,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return int(completed.returncode)


def _run_readonly_argv(argv: tuple[str, ...]) -> tuple[int, str]:
    completed = subprocess.run(
        argv,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return int(completed.returncode), completed.stdout


def _resolve_production_browser_launcher() -> BrowserLauncher:
    try:
        module = importlib.import_module(_PRODUCTION_COMPOSITION_MODULE)
    except ImportError as exc:
        raise WebUIPlatformError(
            "BROWSER_LAUNCH_UNAVAILABLE",
            (
                "isolated FactLane browser launcher is unavailable; expected "
                f"{_PRODUCTION_COMPOSITION_MODULE}.{_PRODUCTION_BROWSER_LAUNCHER}"
            ),
        ) from exc
    launcher = getattr(module, _PRODUCTION_BROWSER_LAUNCHER, None)
    if not callable(launcher):
        raise WebUIPlatformError(
            "BROWSER_LAUNCH_UNAVAILABLE",
            (
                "isolated FactLane browser launcher is unavailable; expected "
                f"{_PRODUCTION_COMPOSITION_MODULE}.{_PRODUCTION_BROWSER_LAUNCHER}"
            ),
        )
    return launcher


def _is_safe_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.startswith("factlane-")
        and parsed.hostname.endswith(".test")
        and port is not None
        and 8765 <= port <= 8774
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
        and origin == f"https://{parsed.hostname}:{port}"
    )
