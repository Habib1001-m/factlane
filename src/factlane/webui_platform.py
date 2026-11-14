from __future__ import annotations

import html
import importlib
import json
import math
import os
import platform
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import sysconfig
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote, urlsplit

from .adapter import PROFILE_DEFINITIONS
from .owner_auth import PendingEnrollment
from .windows_security import (
    WindowsProcessIdentity,
    WindowsSecurityError,
    current_user_sid,
    require_process_identity,
    validate_local_named_pipe_name,
    validate_owner_only_kernel_object,
)
from .windows_firefox_trust import (
    WindowsFirefoxTrustError,
    ensure_firefox_profile_network_policy,
    ensure_firefox_profile_trust,
)
from .windows_tls import (
    WindowsTLSError,
    ensure_windows_tls_material,
    load_windows_tls_material,
)
from .webui_install import (
    BrowserInstall,
    EndpointState,
    InstallEnvironment,
    InstallerStepStatus,
    LINUX_OWNER_PANEL_LAUNCHER_FILENAME,
    ManagedServiceActivationResult,
    ProductionLinuxInstallerBackend,
    ProductionWindowsInstallerBackend,
    SetupApplyResult,
    SetupConsent,
    SetupReceipt,
    SetupRequest,
    WindowsSetupPrecondition,
    WEBUI_PORT_MIN,
    WEBUI_SERVICE_NAME,
    WINDOWS_OWNER_PANEL_LAUNCHER_FILENAME,
    apply_webui_setup,
    consent_requirements,
    load_linux_provisional_installation_id,
    probe_linux_managed_https,
    validate_linux_firefox_managed_profile,
    webui_hostname,
    LinuxSetupRecoveryRequired,
)
from .webui_runtime import (
    BackendTransportKind,
    BackendFactory,
    PlatformKind,
    ProcessFactory,
    RuntimeConfigError,
    RuntimeOutcomeState,
    WebUIRuntimeConfig,
    WebUIRuntimeRunner,
    default_runtime_config_path,
    load_runtime_config,
)
from .webui_service import ServiceStatus, WebUIServiceState
from .webui_windows import (
    WindowsBrowserInstall,
    WindowsBrowserLauncher,
    WindowsOwnerPaths,
    WindowsSetupConsentProvider,
    WindowsTaskObservation,
    WindowsTaskSchedulerSupervisor,
    WindowsWebUIError,
    discover_windows_browser,
    load_windows_managed_config,
    windows_current_user_id,
    windows_managed_python_executables,
    windows_owner_paths,
)


_PRODUCTION_COMPOSITION_MODULE = "factlane.webui_app"
_PRODUCTION_BACKEND_FACTORY = "create_owner_runtime_backend"
_PRODUCTION_PROCESS_FACTORY = "create_caddy_process"
_PRODUCTION_BROWSER_LAUNCHER = "open_owner_browser"
_CONTROL_SOCKET_NAME = "owner-control.sock"
_BACKEND_SOCKET_NAME = "owner-http.sock"
_SERVICE_STATE_NAME = "webui-service.json"
_CONTROL_MAX_FRAME_BYTES = 64 * 1024
_CONTROL_TIMEOUT_SECONDS = 10.0
_HUMAN_CONFIRMATION_RESPONSE_TIMEOUT_SECONDS = 125.0
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
WindowsControlClientFactory = Callable[[str, WindowsProcessIdentity], LauncherControlClient]
BrowserLauncher = Callable[[str], bool]
OpenLinkPresenter = Callable[[str], None]
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


class ManagedServiceSupervisor(Protocol):
    def observe(self) -> Any: ...

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
        details = [
            (
                "Configure the Owner Panel on an exact 127.0.0.1 loopback endpoint, "
                "update its current-user managed service/configuration, and install the "
                "Owner launcher. No browser profile, browser trust store, DNS mapping, "
                "or system trust store is modified."
            ),
            *(item.explanation for item in requirements),
        ]
        review = (
            "FactLane WebUI requires explicit consent for these installation changes:\n\n"
            + "\n".join(f"• {item}" for item in details)
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
            hostname_mapping=False,
            browser_profile_trust=False,
            user_lingering=request.lingering_required,
        )


def acquire_default_setup_consent(
    request: SetupRequest,
    environment: InstallEnvironment,
) -> SetupConsent:
    if environment.system == "Windows" and os.name == "nt":
        approved = WindowsSetupConsentProvider().acquire()
        return SetupConsent(
            hostname_mapping=False,
            browser_profile_trust=False,
            user_lingering=False,
            windows_native_setup=approved,
        )
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
    if os.name == "nt":
        try:
            native = windows_owner_paths(environ=environment)
        except WindowsWebUIError as exc:
            raise WebUIPlatformError(exc.code, exc.safe_message) from exc
        root = Path(native.root_dir)
        return OwnerPaths(
            owner_config_home=native.owner_config_home,
            service_config_path=native.service_config_path,
            caddy_config_path=str(root / "config" / "transport.json"),
            user_unit_path=native.task_receipt_path,
            runtime_dir=native.runtime_dir,
            backend_socket=native.backend_endpoint,
            native_control_socket=native.native_control_socket,
            state_dir=native.state_dir,
            caddy_state_dir=str(root / "state" / "transport"),
            browser_profile_dir=native.browser_profile_dir,
        )
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
    webui_socket_dir = runtime_dir / "owner-webui"
    state_dir = state_home / "factlane"
    return OwnerPaths(
        owner_config_home=str(config_home),
        service_config_path=str(config_dir / "webui.json"),
        caddy_config_path=str(config_dir / "Caddyfile"),
        user_unit_path=str(config_home / "systemd" / "user" / "factlane-webui.service"),
        runtime_dir=str(runtime_dir),
        backend_socket=str(webui_socket_dir / _BACKEND_SOCKET_NAME),
        native_control_socket=str(webui_socket_dir / _CONTROL_SOCKET_NAME),
        state_dir=str(state_dir),
        caddy_state_dir=str(state_dir / "caddy"),
        browser_profile_dir=str(state_dir / "browser"),
    )


def build_webui_doctor_report(
    *,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    owner_uid: int | None = None,
    factlane_executable: str | None = None,
    python_executable: str | None = None,
    service_manager: ManagedServiceSupervisor | None = None,
) -> dict[str, Any]:
    """Return read-only onboarding facts without mutating setup or service state."""

    environment = os.environ if environ is None else environ
    executable = _doctor_executable_identity(factlane_executable)
    interpreter = _doctor_interpreter_identity(python_executable)

    try:
        paths = default_owner_paths(
            environ=environment,
            home=home,
            owner_uid=owner_uid,
        )
    except Exception:
        paths = None

    binding = _doctor_managed_binding(paths, owner_uid=owner_uid)
    launcher = _doctor_panel_launcher(environment, home=home)
    skill = _doctor_skill_surface()
    service = _doctor_service_surface(service_manager)
    mcp = _doctor_mcp_surface(executable, binding)

    next_actions: list[dict[str, Any]] = []
    if binding["state"] == "CONFIGURED":
        next_actions.append(
            {
                "id": "OPEN_OWNER_PANEL",
                "message": "Open the managed Owner Panel.",
                "command": ["factlane", "launch", "webui"],
            }
        )
    elif binding["state"] == "UNCONFIGURED":
        next_actions.append(
            {
                "id": "CONFIGURE_OWNER_PANEL_MEMORY",
                "message": "Choose a supported profile and bind the managed Owner Panel memory explicitly.",
                "command": ["factlane", "setup", "webui"],
            }
        )
    else:
        next_actions.append(
            {
                "id": "REVIEW_OWNER_PANEL_CONFIGURATION",
                "message": "Review the managed Owner Panel configuration before opening it.",
                "command": ["factlane", "status", "webui"],
            }
        )
    next_actions.append(
        {
            "id": "CONFIGURE_MCP_HOST",
            "message": (
                "Configure each stdio MCP host with its own explicit host_id; "
                "scope remains explicit in each MCP request."
            ),
            "recipe_ref": "mcp.launch_recipe",
        }
    )

    return {
        "status": "OK",
        "mode": "READ_ONLY",
        "platform": "WINDOWS" if os.name == "nt" else "LINUX",
        "factlane": {
            "executable": executable,
            "interpreter": interpreter,
        },
        "managed_binding": binding,
        "skill": skill,
        "panel_launcher": launcher,
        "service": service,
        "mcp": mcp,
        "next_actions": next_actions,
    }


def _doctor_executable_identity(explicit: str | None) -> dict[str, Any]:
    candidate = explicit
    if candidate is None:
        candidate = shutil.which("factlane")
    if candidate is None:
        invoked = sys.argv[0] if sys.argv else ""
        if invoked:
            if os.path.isabs(invoked):
                candidate = invoked
            else:
                invoked_path = Path(invoked)
                if invoked_path.is_file():
                    try:
                        candidate = str(invoked_path.resolve(strict=True))
                    except OSError:
                        candidate = None
                else:
                    candidate = shutil.which(invoked)
    if not candidate or not os.path.isabs(candidate):
        return {"state": "UNKNOWN", "path": None, "present": False}
    path = Path(candidate)
    if os.name == "nt" and not path.is_file() and path.suffix.casefold() != ".exe":
        executable_path = Path(f"{candidate}.exe")
        if executable_path.is_file():
            path = executable_path
    present = path.is_file()
    return {
        "state": "RESOLVED" if present else "UNKNOWN",
        "path": str(path),
        "present": present,
    }


def _doctor_interpreter_identity(explicit: str | None) -> dict[str, Any]:
    candidate = explicit or sys.executable
    if not candidate or not os.path.isabs(candidate):
        path = None
        present = False
    else:
        path = str(Path(candidate))
        present = Path(candidate).is_file()
    return {
        "state": "RESOLVED" if present else "UNKNOWN",
        "path": path,
        "present": present,
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
    }


def _doctor_managed_binding(
    paths: OwnerPaths | None,
    *,
    owner_uid: int | None,
) -> dict[str, Any]:
    profiles = sorted(PROFILE_DEFINITIONS)
    if paths is None:
        return {
            "state": "UNKNOWN",
            "db": None,
            "profile": None,
            "supported_profiles": profiles,
        }
    try:
        if os.name == "nt":
            existing = load_windows_managed_config(paths.service_config_path)
        else:
            uid = os.getuid() if owner_uid is None else owner_uid
            existing = _load_existing_runtime_config(
                paths.service_config_path,
                owner_uid=uid,
            )
    except (WebUIPlatformError, WindowsWebUIError) as exc:
        return {
            "state": "ERROR",
            "error_code": getattr(exc, "code", "CONFIG_UNAVAILABLE"),
            "db": None,
            "profile": None,
            "supported_profiles": profiles,
        }
    except Exception:
        return {
            "state": "ERROR",
            "error_code": "CONFIG_UNAVAILABLE",
            "db": None,
            "profile": None,
            "supported_profiles": profiles,
        }
    if existing is None or existing.db is None:
        return {
            "state": "UNCONFIGURED",
            "db": None,
            "profile": None,
            "supported_profiles": profiles,
        }
    return {
        "state": "CONFIGURED",
        "db": existing.db,
        "profile": existing.profile,
    }


def _doctor_panel_launcher(
    environment: Mapping[str, str],
    *,
    home: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    try:
        if os.name == "nt":
            appdata = environment.get("APPDATA")
            if not isinstance(appdata, str) or not appdata:
                raise ValueError("APPDATA unavailable")
            root = PureWindowsPath(appdata)
            if not root.is_absolute() or ".." in root.parts:
                raise ValueError("APPDATA invalid")
            launcher_path = str(
                root
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
                / WINDOWS_OWNER_PANEL_LAUNCHER_FILENAME
            )
        else:
            owner_home = Path.home() if home is None else Path(home)
            if not owner_home.is_absolute():
                raise ValueError("owner home invalid")
            data_home = _xdg_path(
                environment,
                "XDG_DATA_HOME",
                fallback=owner_home / ".local" / "share",
            )
            launcher_path = str(
                data_home / "applications" / LINUX_OWNER_PANEL_LAUNCHER_FILENAME
            )
    except Exception:
        return {"state": "UNKNOWN", "path": None, "present": False}
    present = Path(launcher_path).is_file()
    return {
        "state": "PRESENT" if present else "ABSENT",
        "path": launcher_path,
        "present": present,
    }


def _discover_packaged_skill_root() -> Path | None:
    candidates: list[Path] = []
    try:
        data_root = sysconfig.get_path("data")
    except Exception:
        data_root = None
    if isinstance(data_root, str) and data_root:
        candidates.append(
            Path(data_root) / "share" / "factlane" / "skills" / "using-factlane"
        )
    try:
        source_root = Path(__file__).resolve().parents[2]
    except (OSError, IndexError):
        source_root = None
    if source_root is not None and (source_root / "pyproject.toml").is_file():
        candidates.append(source_root / "skills" / "using-factlane")
    for candidate in candidates:
        if (candidate / "SKILL.md").is_file():
            return candidate
    return None


def _doctor_skill_surface() -> dict[str, Any]:
    root = _discover_packaged_skill_root()
    if root is None:
        return {"state": "UNKNOWN", "root": None, "present": False}
    return {"state": "PRESENT", "root": str(root), "present": True}


def _doctor_service_surface(
    service_manager: ManagedServiceSupervisor | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": "UNKNOWN",
        "unit": None,
        "status_command": ["factlane", "status", "webui"],
        "open_command": ["factlane", "open", "webui"],
        "launch_command": ["factlane", "launch", "webui"],
    }
    manager = service_manager
    if manager is None:
        try:
            manager = (
                _default_windows_task_supervisor()
                if os.name == "nt"
                else _default_systemd_user_manager()
            )
        except Exception:
            manager = None
    if manager is None:
        return result
    try:
        unit = manager.observe()
        unit_payload = _unit_payload(unit)
    except Exception:
        return result
    result["state"] = "ACTIVE" if unit_payload["active"] is True else "INACTIVE"
    result["unit"] = unit_payload
    return result


def _doctor_mcp_surface(
    executable: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    command = executable.get("path") if executable.get("state") == "RESOLVED" else None
    if not isinstance(command, str):
        command = "factlane"
    configured = binding.get("state") == "CONFIGURED"
    db = binding.get("db") if configured else None
    profile = binding.get("profile") if configured else None
    if not isinstance(db, str):
        db = "<DB_PATH>"
    if not isinstance(profile, str):
        profile = "<PROFILE>"
    return {
        "transport": "stdio",
        "host_id": {"state": "EXPLICIT_REQUIRED", "value": None},
        "scope": {"state": "EXPLICIT_PER_REQUEST", "value": None},
        "launch_recipe": {
            "kind": "argv",
            "argv": [
                command,
                "--db",
                db,
                "--profile",
                profile,
                "--host-id",
                "<MCP_HOST_ID>",
                "--write-profile",
                "read-only",
            ],
        },
    }


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
    if os.name == "nt":
        paths = default_owner_paths(environ=environment, home=home, owner_uid=owner_uid)
        user = installing_user or windows_current_user_id()
        executable = factlane_executable or sys.executable
        if not executable or not Path(executable).is_absolute():
            raise WebUIPlatformError(
                "FACTLANE_EXECUTABLE_UNAVAILABLE",
                "installed FactLane Python executable could not be resolved to an absolute Windows path",
            )
        if not Path(executable).is_file():
            raise WebUIPlatformError(
                "FACTLANE_EXECUTABLE_UNAVAILABLE",
                "installed FactLane Python executable is unavailable",
            )
        appdata = environment.get("APPDATA")
        if not isinstance(appdata, str) or not appdata:
            raise WebUIPlatformError(
                "WINDOWS_OWNER_LAUNCHER_PATH_UNAVAILABLE",
                "APPDATA is required for the current-user Owner Panel launcher",
            )
        appdata_path = PureWindowsPath(appdata)
        if not appdata_path.is_absolute() or ".." in appdata_path.parts:
            raise WebUIPlatformError(
                "WINDOWS_OWNER_LAUNCHER_PATH_INVALID",
                "APPDATA must resolve to one absolute current-user Windows path",
            )
        launcher_path = str(
            appdata_path
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "FactLane Owner Panel.lnk"
        )
        try:
            existing = load_windows_managed_config(paths.service_config_path)
        except WindowsWebUIError as exc:
            raise WebUIPlatformError(exc.code, exc.safe_message) from exc
        if existing is None:
            installation_id = installation_id_factory()
            saved_port = WEBUI_PORT_MIN
            db = profile = host_id = None
        else:
            installation_id = existing.installation_id
            parsed = urlsplit(existing.origin)
            if parsed.port is None:
                raise WebUIPlatformError("WINDOWS_CONFIG_INVALID", "saved Windows endpoint has no port")
            saved_port = parsed.port
            db, profile, host_id = existing.db, existing.profile, existing.host_id
        return SetupRequest(
            installation_id=installation_id,
            saved_port=saved_port,
            lingering_required=False,
            installing_user=user,
            factlane_executable=str(Path(executable)),
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
            native_control_socket=paths.native_control_socket,
            launcher_path=launcher_path,
        )
    uid = os.getuid() if owner_uid is None else owner_uid
    paths = default_owner_paths(environ=environment, home=home, owner_uid=uid)
    owner_home = Path.home() if home is None else Path(home)
    data_home = _xdg_path(
        environment,
        "XDG_DATA_HOME",
        fallback=owner_home / ".local" / "share",
    )

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
        try:
            provisional_id = load_linux_provisional_installation_id(
                paths.state_dir,
                owner_uid=uid,
            )
        except RuntimeError as exc:
            raise WebUIPlatformError(
                "SETUP_RECOVERY_REQUIRED",
                "provisional Linux setup state conflicts with the exact managed target",
            ) from exc
        installation_id = provisional_id or installation_id_factory()
        saved_port = WEBUI_PORT_MIN
        db = profile = host_id = None
    else:
        parsed = urlsplit(existing.origin)
        hostname = parsed.hostname
        assert parsed.port is not None
        saved_port = parsed.port
        if parsed.scheme == "https":
            if hostname is None or not hostname.startswith("factlane-") or not hostname.endswith(".test"):
                raise WebUIPlatformError(
                    "RUNTIME_CONFIG_INVALID",
                    "existing runtime origin does not contain a canonical FactLane installation ID",
                )
            installation_id = hostname[len("factlane-") : -len(".test")]
        else:
            # Browser-neutral managed configs intentionally use the fixed
            # 127.0.0.1 host, so the installation identity can no longer be
            # recovered from the origin.  The owner-private setup journal is
            # the durable binding created before the managed config is first
            # written and retained after CLAIMED; require that binding rather
            # than minting a new installation identity on a setup rerun.
            try:
                installation_id = load_linux_provisional_installation_id(
                    paths.state_dir,
                    owner_uid=uid,
                )
            except RuntimeError as exc:
                raise WebUIPlatformError(
                    "SETUP_RECOVERY_REQUIRED",
                    "provisional Linux setup state conflicts with the exact managed target",
                ) from exc
            if installation_id is None:
                raise WebUIPlatformError(
                    "RUNTIME_CONFIG_INVALID",
                    "browser-neutral runtime origin lacks its managed installation binding",
                )
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
        launcher_path=str(data_home / "applications" / "FactLane Owner Panel.desktop"),
    )


def build_default_setup_executor(
    request: SetupRequest,
    environment: InstallEnvironment,
    *,
    which: Callable[[str], str | None] = shutil.which,
    command_runner: Callable[[tuple[str, ...]], int] | None = None,
    read_command_runner: Callable[[tuple[str, ...]], tuple[int, str]] | None = None,
    caddy_bootstrap_process_factory: Callable[[tuple[str, ...]], object] | None = None,
    service_state_reader: Callable[[], Any] | None = None,
    activation_probe: Callable[[str], ManagedServiceActivationResult] | None = None,
    service_state_factory: Callable[[str], Any] = WebUIServiceState,
    windows_supervisor: WindowsTaskSchedulerSupervisor | None = None,
    windows_setup_precondition: WindowsSetupPrecondition | None = None,
) -> SetupApplyExecutor:
    """Compose the default shipped setup backend for the active platform."""

    if environment.system == "Windows" and os.name == "nt":
        backend = ProductionWindowsInstallerBackend(
            request=request,
            supervisor=windows_supervisor,
            expected_precondition=windows_setup_precondition,
        )

        def execute_windows(
            receipt: SetupReceipt,
            consent: SetupConsent,
        ) -> SetupApplyResult:
            return apply_webui_setup(receipt, consent, backend)

        return execute_windows

    systemctl_path = which("systemctl")
    loginctl_path = which("loginctl")
    caddy_path = environment.caddy_path
    if (
        environment.system != "Linux"
        or not environment.systemd
        or systemctl_path is None
        or not os.path.isabs(systemctl_path)
        or caddy_path is None
        or not os.path.isabs(caddy_path)
        or (request.lingering_required and (loginctl_path is None or not os.path.isabs(loginctl_path)))
    ):
        def unavailable(
            _receipt: SetupReceipt,
            _consent: SetupConsent,
        ) -> SetupApplyResult:
            return SetupApplyResult(
                status=InstallerStepStatus.FAILED,
                endpoint_state=EndpointState.PROPOSED,
                detail="required Linux managed-setup system tooling is unavailable",
            )

        return unavailable

    state_path = str(PurePosixPath(request.state_dir) / _SERVICE_STATE_NAME)

    def default_state_reader() -> Any:
        # The installer and the managed service are separate processes.  A
        # cached WebUIServiceState instance would keep the pre-start snapshot
        # in memory and never observe the service claiming READY on disk.
        return service_state_factory(state_path).snapshot()

    backend_kwargs: dict[str, Any] = {
        "systemctl_path": systemctl_path,
        "loginctl_path": loginctl_path,
        "certutil_path": None,
        "caddy_path": caddy_path,
        "owner_uid": os.getuid(),
        "service_state_reader": service_state_reader or default_state_reader,
    }
    if command_runner is not None:
        backend_kwargs["command_runner"] = command_runner
    if read_command_runner is not None:
        backend_kwargs["read_command_runner"] = read_command_runner
    if caddy_bootstrap_process_factory is not None:
        backend_kwargs["caddy_bootstrap_process_factory"] = caddy_bootstrap_process_factory
    root_path = str(PurePosixPath(request.caddy_state_dir) / "pki" / "authorities" / "local" / "root.crt")
    backend_kwargs["activation_probe"] = activation_probe or (
        lambda origin: probe_linux_managed_https(origin, root_cert_path=root_path)
    )
    backend = ProductionLinuxInstallerBackend(**backend_kwargs)

    def execute(
        receipt: SetupReceipt,
        consent: SetupConsent,
    ) -> SetupApplyResult:
        return apply_webui_setup(receipt, consent, backend)

    return execute


def capture_windows_setup_precondition(
    request: SetupRequest,
    *,
    windows_supervisor: WindowsTaskSchedulerSupervisor | None = None,
) -> WindowsSetupPrecondition:
    """Capture the exact native-Windows setup custody before human consent."""

    if os.name != "nt":
        raise WebUIPlatformError(
            "WINDOWS_SETUP_UNAVAILABLE",
            "native Windows setup precondition is only available on Windows",
        )
    backend = ProductionWindowsInstallerBackend(
        request=request,
        supervisor=windows_supervisor,
    )
    return backend.capture_precondition()


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
            },
            response_timeout_seconds=_HUMAN_CONFIRMATION_RESPONSE_TIMEOUT_SECONDS,
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
            response_timeout_seconds=_HUMAN_CONFIRMATION_RESPONSE_TIMEOUT_SECONDS,
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


class WindowsNativeLauncherClient(NativeLauncherClient):
    """Owner launcher/control client over an authenticated local Windows Named Pipe."""

    def __init__(
        self,
        pipe_name: str,
        *,
        expected_server: WindowsProcessIdentity,
        owner_sid: str | None = None,
        timeout_seconds: float = _CONTROL_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(expected_server, WindowsProcessIdentity):
            raise TypeError("expected_server must be a WindowsProcessIdentity")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or timeout_seconds > _CONTROL_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be greater than zero and at most 10 seconds")
        self.pipe_name = validate_local_named_pipe_name(pipe_name, prefix="factlane-owner-")
        self.owner_sid = current_user_sid() if owner_sid is None else owner_sid
        self.expected_server = expected_server
        self.timeout_seconds = float(timeout_seconds)
        if self.expected_server.sid.casefold() != self.owner_sid.casefold():
            raise WebUIPlatformError(
                "CONTROL_PEER_IDENTITY_DENIED",
                "expected native-control server belongs to a different Windows user",
            )
        if current_user_sid().casefold() != self.owner_sid.casefold():
            raise WebUIPlatformError(
                "CONTROL_OWNER_IDENTITY_DENIED",
                "native-control client is not running as the configured Windows Owner",
            )

    @staticmethod
    def _windows_pipe_modules() -> tuple[Any, Any, Any]:
        if os.name != "nt":
            raise WebUIPlatformError(
                "CONTROL_UNAVAILABLE",
                "Windows native-control Named Pipes are unavailable on this platform",
            )
        try:
            import pywintypes
            import win32file
            import win32pipe
        except ImportError as exc:  # pragma: no cover - native-Windows packaging guard
            raise WebUIPlatformError(
                "CONTROL_UNAVAILABLE",
                "pywin32 Named Pipe primitives are unavailable",
            ) from exc
        return pywintypes, win32file, win32pipe

    @classmethod
    def _read_pipe_exact(cls, handle: Any, length: int, *, deadline: float) -> bytes:
        pywintypes, win32file, _win32pipe = cls._windows_pipe_modules()
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            if time.monotonic() >= deadline:
                raise WebUIPlatformError(
                    "CONTROL_UNAVAILABLE",
                    "native control response timed out",
                )
            try:
                _result, chunk = win32file.ReadFile(handle, remaining)
            except pywintypes.error as exc:
                if getattr(exc, "winerror", None) in {232, 536}:
                    if time.monotonic() >= deadline:
                        raise WebUIPlatformError(
                            "CONTROL_UNAVAILABLE",
                            "native control response timed out",
                        ) from exc
                    time.sleep(0.01)
                    continue
                raise WebUIPlatformError(
                    "CONTROL_RESPONSE_INCOMPLETE",
                    "native control response ended early",
                ) from exc
            except Exception as exc:
                raise WebUIPlatformError(
                    "CONTROL_RESPONSE_INCOMPLETE",
                    "native control response ended early",
                ) from exc
            if time.monotonic() >= deadline:
                raise WebUIPlatformError(
                    "CONTROL_UNAVAILABLE",
                    "native control response timed out",
                )
            if not chunk:
                raise WebUIPlatformError(
                    "CONTROL_RESPONSE_INCOMPLETE",
                    "native control response ended early",
                )
            chunks.append(bytes(chunk))
            remaining -= len(chunk)
        return b"".join(chunks)

    def _validate_connected_server(self, handle: Any, win32pipe: Any) -> None:
        try:
            validate_owner_only_kernel_object(handle, self.owner_sid)
        except WindowsSecurityError as exc:
            raise WebUIPlatformError(
                "CONTROL_SOCKET_UNSAFE",
                "native WebUI Named Pipe is not owner-private",
            ) from exc
        try:
            server_pid = int(win32pipe.GetNamedPipeServerProcessId(handle))
            if server_pid != self.expected_server.pid:
                raise WebUIPlatformError(
                    "CONTROL_ENDPOINT_REPLACED",
                    "native WebUI control endpoint is owned by an unexpected process",
                )
            require_process_identity(
                server_pid,
                owner_sid=self.owner_sid,
                creation_time_us=self.expected_server.creation_time_us,
            )
        except WebUIPlatformError:
            raise
        except WindowsSecurityError as exc:
            code = (
                "CONTROL_PEER_IDENTITY_DENIED"
                if exc.code == "WINDOWS_PROCESS_SID_MISMATCH"
                else "CONTROL_ENDPOINT_REPLACED"
                if exc.code == "WINDOWS_PROCESS_IDENTITY_MISMATCH"
                else "CONTROL_PEER_IDENTITY_UNAVAILABLE"
            )
            raise WebUIPlatformError(code, "native WebUI control peer identity could not be verified") from exc
        except Exception as exc:
            raise WebUIPlatformError(
                "CONTROL_PEER_IDENTITY_UNAVAILABLE",
                "native WebUI control peer identity could not be verified",
            ) from exc

    def _open_pipe(self, pywintypes: Any, win32file: Any, win32pipe: Any) -> Any:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebUIPlatformError(
                    "CONTROL_UNAVAILABLE",
                    "native WebUI control Named Pipe did not become available before timeout",
                )
            try:
                win32pipe.WaitNamedPipe(self.pipe_name, max(1, int(remaining * 1000)))
                return win32file.CreateFile(
                    self.pipe_name,
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

    def _exchange(
        self,
        request: dict[str, Any],
        *,
        response_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if not raw or len(raw) > _CONTROL_MAX_FRAME_BYTES:
            raise WebUIPlatformError("CONTROL_REQUEST_INVALID", "native control request is too large")
        _pywintypes, win32file, win32pipe = self._windows_pipe_modules()
        handle: Any | None = None
        try:
            handle = self._open_pipe(_pywintypes, win32file, win32pipe)
            self._validate_connected_server(handle, win32pipe)
            win32pipe.SetNamedPipeHandleState(
                handle,
                win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_NOWAIT,
                None,
                None,
            )
            win32file.WriteFile(handle, struct.pack("!I", len(raw)) + raw)
            response_timeout = self.timeout_seconds if response_timeout_seconds is None else float(response_timeout_seconds)
            deadline = time.monotonic() + response_timeout
            length = struct.unpack("!I", self._read_pipe_exact(handle, 4, deadline=deadline))[0]
            if length == 0 or length > _CONTROL_MAX_FRAME_BYTES:
                raise WebUIPlatformError(
                    "CONTROL_RESPONSE_INVALID",
                    "native control response frame is invalid",
                )
            payload = self._read_pipe_exact(handle, length, deadline=deadline)
        except WebUIPlatformError:
            raise
        except Exception as exc:
            raise WebUIPlatformError(
                "CONTROL_UNAVAILABLE",
                "native WebUI control Named Pipe is unavailable",
            ) from exc
        finally:
            if handle is not None:
                close = getattr(handle, "Close", None)
                if callable(close):
                    close()
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
        service_manager: ManagedServiceSupervisor | None = None,
        windows_control_client_factory: WindowsControlClientFactory | None = None,
        browser_launcher: BrowserLauncher | None = None,
        open_link_presenter: OpenLinkPresenter | None = None,
        platform_system: str | None = None,
        request_id_factory: RequestIdFactory = lambda: secrets.token_urlsafe(18),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        startup_timeout: float = 30.0,
        startup_poll_interval: float = 0.1,
        enrollment_timeout: float = _LOCAL_ENROLLMENT_TIMEOUT_SECONDS,
        enrollment_poll_interval: float = _LOCAL_ENROLLMENT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.environ = os.environ if environ is None else environ
        self.home = Path.home() if home is None else Path(home)
        self.platform_system = platform.system() if platform_system is None else platform_system
        default_uid = os.getuid() if hasattr(os, "getuid") else 0
        self.owner_uid = default_uid if owner_uid is None else owner_uid
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
        self._windows_control_client_factory = windows_control_client_factory or (
            lambda path, identity: WindowsNativeLauncherClient(
                path,
                expected_server=identity,
                owner_sid=current_user_sid(),
            )
        )
        if service_manager is not None:
            self._service_manager: ManagedServiceSupervisor | None = service_manager
        elif self.platform_system == "Windows" and os.name == "nt":
            self._service_manager = _default_windows_task_supervisor()
        else:
            self._service_manager = systemd_manager or _default_systemd_user_manager()
        self._systemd_manager = self._service_manager if isinstance(self._service_manager, SystemdUserUnitManager) else systemd_manager
        self._browser_launcher = browser_launcher
        self._open_link_presenter = open_link_presenter
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
        except WindowsWebUIError as exc:
            return {
                "status": "ERROR",
                "error_code": exc.code,
                "message": exc.safe_message,
            }
        except Exception:
            return {
                "status": "ERROR",
                "error_code": "PLATFORM_OPERATION_FAILED",
                "message": "FactLane WebUI platform operation failed safely",
            }

    def _serve(self) -> Mapping[str, Any]:
        if self.platform_system == "Windows" and os.name == "nt":
            return self._serve_windows()
        config_path = default_runtime_config_path(environ=self.environ, home=self.home)
        config = load_runtime_config(config_path, owner_uid=self.owner_uid)
        legacy_endpoint_hostname: str | None = None
        if config.origin.startswith("http://127.0.0.1:"):
            try:
                installation_id = load_linux_provisional_installation_id(
                    config.state_dir,
                    owner_uid=self.owner_uid,
                )
            except RuntimeError as exc:
                raise RuntimeConfigError(
                    "browser-neutral runtime installation binding is invalid"
                ) from exc
            if installation_id is None:
                raise RuntimeConfigError(
                    "browser-neutral runtime installation binding is unavailable"
                )
            legacy_endpoint_hostname = webui_hostname(installation_id)
        backend_factory, process_factory = self._runtime_factories()
        service_state_path = str(PurePosixPath(config.state_dir) / _SERVICE_STATE_NAME)
        service_state = self._service_state_factory(service_state_path)
        runner = self._runtime_runner_factory(
            config,
            service_state,
            backend_factory=backend_factory,
            process_factory=process_factory,
            **(
                {"legacy_endpoint_hostname": legacy_endpoint_hostname}
                if legacy_endpoint_hostname is not None
                else {}
            ),
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

    def _serve_windows(self) -> Mapping[str, Any]:
        try:
            managed_config = load_windows_managed_config(self.paths.service_config_path)
        except WindowsWebUIError as exc:
            raise WebUIPlatformError(exc.code, exc.safe_message) from exc
        if managed_config is None:
            raise WebUIPlatformError(
                "WINDOWS_MANAGED_CONFIG_UNAVAILABLE",
                "native Windows managed WebUI setup has not been completed",
            )

        runtime_config = WebUIRuntimeConfig(
            db=managed_config.db,
            profile=managed_config.profile,
            host_id=managed_config.host_id,
            origin=managed_config.origin,
            backend_socket=None,
            caddy_path=None,
            caddyfile=None,
            runtime_dir=managed_config.runtime_dir,
            state_dir=managed_config.state_dir,
            platform_kind=PlatformKind.WINDOWS,
            backend_transport=(
                BackendTransportKind.WINDOWS_HTTP
                if managed_config.origin.startswith("http://")
                else BackendTransportKind.WINDOWS_HTTPS
            ),
        )
        parsed_origin = urlsplit(runtime_config.origin)
        hostname = parsed_origin.hostname
        if hostname is None:
            raise WebUIPlatformError(
                "WINDOWS_CONFIG_INVALID",
                "managed Windows origin has no hostname",
            )
        if runtime_config.backend_transport is BackendTransportKind.WINDOWS_HTTPS:
            try:
                ensure_windows_tls_material(Path(runtime_config.state_dir) / "tls", hostname)
            except WindowsTLSError as exc:
                raise WebUIPlatformError(exc.code, exc.safe_message) from exc

        service_state = self._persistent_service_state()
        runner = self._runtime_runner_factory(
            runtime_config,
            service_state,
            backend_factory=self._runtime_backend_factory(),
            process_factory=None,
            external_gateway_required=False,
            **(
                {
                    "legacy_endpoint_hostname": webui_hostname(
                        managed_config.installation_id
                    )
                }
                if runtime_config.origin.startswith("http://127.0.0.1:")
                else {}
            ),
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

    def _runtime_backend_factory(self) -> BackendFactory:
        if self._backend_factory is not None:
            return self._backend_factory
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
        backend_factory = getattr(module, _PRODUCTION_BACKEND_FACTORY, None)
        if not callable(backend_factory):
            raise WebUIPlatformError(
                "RUNTIME_COMPOSITION_UNAVAILABLE",
                (
                    "production WebUI runtime composition must provide "
                    f"{_PRODUCTION_COMPOSITION_MODULE}.{_PRODUCTION_BACKEND_FACTORY}"
                ),
            )
        return backend_factory

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

        manager = self._require_service_manager()
        unit = manager.observe()
        request_id = self._request_id_factory()

        if action == "stop":
            if unit.active:
                return self._native_live_control("stop", request_id, unit)
            service = self._persistent_service_state().request_stop(request_id)
            return {
                "status": "OK",
                "request_id": request_id,
                "unit": _unit_payload(unit),
                "service": _service_snapshot_payload(service),
            }

        if action == "restart":
            if unit.active:
                if self.platform_system == "Windows" and os.name == "nt":
                    return self._windows_restart(unit)
                return self._native_live_control("restart", request_id, unit)
            return self._offline_start(request_id)

        if action == "start":
            if unit.active:
                return self._native_live_control("start", request_id, unit)
            return self._offline_start(request_id)

        raise WebUIPlatformError("UNKNOWN_ACTION", "unsupported WebUI control action")

    def _windows_restart(self, unit: Any) -> Mapping[str, Any]:
        stop_request_id = self._request_id_factory()
        start_request_id = self._request_id_factory()
        self._native_live_control("stop", stop_request_id, unit)
        manager = self._require_service_manager()
        deadline = self._clock() + self._startup_timeout
        while True:
            observed = manager.observe()
            if not observed.active:
                break
            now = self._clock()
            if now >= deadline:
                return {
                    "status": "ERROR",
                    "error_code": "SERVICE_RESTART_DRAIN_TIMEOUT",
                    "message": "Windows WebUI service did not exit after graceful stop within the restart deadline",
                    "unit": _unit_payload(observed),
                }
            self._sleep(min(self._startup_poll_interval, deadline - now))
        return self._offline_start(start_request_id)

    def _offline_status(self) -> Mapping[str, Any]:
        unit = self._require_service_manager().observe()
        service = self._persistent_service_state().snapshot()
        return {
            "status": "OK",
            "unit": _unit_payload(unit),
            "service": _service_snapshot_payload(service),
        }

    def _offline_start(self, request_id: str) -> Mapping[str, Any]:
        manager = self._require_service_manager()
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
                    "request_id": request_id,
                    "unit": _unit_payload(last_unit),
                    "service": _service_snapshot_payload(last_service),
                }
            if last_service.status is ServiceStatus.FAILED:
                return {
                    "status": "ERROR",
                    "request_id": request_id,
                    "error_code": "SERVICE_START_FAILED",
                    "message": "WebUI service entered FAILED during explicit start",
                    "unit": _unit_payload(last_unit),
                    "service": _service_snapshot_payload(last_service),
                }
            now = self._clock()
            if now >= deadline:
                return {
                    "status": "ERROR",
                    "request_id": request_id,
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
        unit: Any,
    ) -> Mapping[str, Any]:
        client = self._live_control_client(unit)
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
        return {"status": "OK", "request_id": request_id, "service": dict(service)}

    def _live_control_client(self, unit: Any) -> LauncherControlClient:
        if self.platform_system == "Windows" and os.name == "nt":
            identity = getattr(unit, "process_identity", None)
            if not isinstance(identity, WindowsProcessIdentity):
                raise WebUIPlatformError(
                    "WINDOWS_TASK_PROCESS_UNAVAILABLE",
                    "running Windows WebUI task has no verified process identity",
                )
            return self._windows_control_client_factory(
                self.paths.native_control_socket,
                identity,
            )
        return self._control_client_factory(
            self._linux_native_control_socket(),
            self.owner_uid,
        )

    def _linux_native_control_socket(self) -> str:
        """Resolve native control from the persisted Linux backend binding when present.

        Managed configs created before the private `owner-webui` socket directory
        placed the HTTP socket directly beneath the shared runtime root.  The
        running backend derives native control as a sibling of that persisted HTTP
        socket, so launcher/control clients must follow the same binding until a
        governed setup rerun migrates the config to the current canonical path.
        """

        config_path = Path(self.paths.service_config_path)
        try:
            config_path.lstat()
        except FileNotFoundError:
            return self.paths.native_control_socket
        except OSError as exc:
            raise WebUIPlatformError(
                "RUNTIME_CONFIG_ERROR",
                "existing runtime config could not be inspected safely",
            ) from exc
        try:
            config = load_runtime_config(config_path, owner_uid=self.owner_uid)
        except RuntimeConfigError as exc:
            raise WebUIPlatformError("RUNTIME_CONFIG_ERROR", str(exc)) from exc
        if config.platform_kind is not PlatformKind.LINUX or config.backend_socket is None:
            raise WebUIPlatformError(
                "RUNTIME_CONFIG_ERROR",
                "Linux native control requires a persisted AF_UNIX backend socket",
            )
        return str(PurePosixPath(config.backend_socket).parent / _CONTROL_SOCKET_NAME)

    def _persistent_service_state(self) -> Any:
        if self._persistent_state is None:
            if self.platform_system == "Windows" and os.name == "nt":
                state_path = str(Path(self.paths.state_dir) / _SERVICE_STATE_NAME)
            else:
                state_path = str(PurePosixPath(self.paths.state_dir) / _SERVICE_STATE_NAME)
            self._persistent_state = self._service_state_factory(state_path)
        return self._persistent_state

    def _require_service_manager(self) -> ManagedServiceSupervisor:
        if self._service_manager is None:
            if self.platform_system == "Windows" and os.name == "nt":
                raise WebUIPlatformError(
                    "SERVICE_MANAGER_UNAVAILABLE",
                    "managed WebUI service supervisor is unavailable",
                )
            raise WebUIPlatformError(
                "SYSTEMD_USER_MANAGER_UNAVAILABLE",
                "systemd user-manager control is unavailable",
            )
        return self._service_manager

    def _require_systemd_manager(self) -> SystemdUnitManager:
        manager = self._require_service_manager()
        if not isinstance(manager, SystemdUserUnitManager):
            raise WebUIPlatformError(
                "SYSTEMD_USER_MANAGER_UNAVAILABLE",
                "systemd user-manager control is unavailable",
            )
        return manager

    def _open(self) -> Mapping[str, Any]:
        if self.platform_system == "Windows" and os.name == "nt":
            unit = self._require_service_manager().observe()
            if not unit.active:
                raise WebUIPlatformError(
                    "WEBUI_NOT_RUNNING",
                    "WebUI managed service is not active",
                )
            client = self._live_control_client(unit)
        else:
            client = self._control_client_factory(
                self._linux_native_control_socket(),
                self.owner_uid,
            )
        service = client.status()
        origin = service.get("saved_endpoint")
        if not isinstance(origin, str) or not _is_safe_origin(origin):
            raise WebUIPlatformError(
                "WEBUI_ENDPOINT_UNAVAILABLE",
                "WebUI service has no valid saved managed endpoint to open",
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
        # Keep the one-time local enrollment capability entirely in the URL
        # fragment. Fragments are not sent in the HTTP request; the SPA consumes
        # and immediately scrubs the handoff value before claiming it.  Browser
        # choice is deliberately left to the Owner.
        connect_url = f"{origin}#connect={quote(enrollment.locator, safe='')}"
        if self._open_link_presenter is None:
            raise WebUIPlatformError(
                "OPEN_LINK_PRESENTATION_UNAVAILABLE",
                "Owner Panel link could not be presented safely",
            )
        try:
            self._open_link_presenter(connect_url)
        except Exception as exc:
            raise WebUIPlatformError(
                "OPEN_LINK_PRESENTATION_FAILED",
                "Owner Panel link could not be presented safely",
            ) from exc

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


def _present_open_link_to_stderr(url: str) -> None:
    sys.stderr.write(
        "Open this URL in any browser:\n"
        f"{url}\n"
        "Waiting for browser connection and local approval...\n"
    )
    sys.stderr.flush()


def build_default_platform_controller() -> ProductionWebUIController:
    return ProductionWebUIController(open_link_presenter=_present_open_link_to_stderr)


def _default_systemd_user_manager() -> SystemdUserUnitManager | None:
    systemctl = shutil.which("systemctl")
    if not systemctl or not os.path.isabs(systemctl):
        return None
    return SystemdUserUnitManager(systemctl)


def _default_windows_task_supervisor() -> WindowsTaskSchedulerSupervisor:
    base, environment = windows_managed_python_executables()
    return WindowsTaskSchedulerSupervisor(
        expected_executable=base,
        environment_executable=environment,
    )


class LinuxFirefoxBrowserLauncher:
    """Shell-free launcher for the one isolated managed Linux Firefox profile."""

    def __init__(
        self,
        browser: BrowserInstall,
        profile_dir: str,
        *,
        process_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        if browser.family != "firefox":
            raise ValueError("Linux managed browser launcher requires Firefox")
        if not os.path.isabs(profile_dir):
            raise ValueError("isolated Firefox profile path must be absolute")
        self.browser = browser
        self.profile_dir = profile_dir
        self._process_factory = process_factory

    def __call__(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError):
            return False
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or not parsed.hostname.startswith("factlane-")
            or not parsed.hostname.endswith(".test")
            or port is None
            or not WEBUI_PORT_MIN <= port <= 8774
            or parsed.path
            or parsed.query
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.fragment.startswith("connect=")
            or parsed.fragment.count("connect=") != 1
            or "&" in parsed.fragment
            or len(parsed.fragment) <= len("connect=")
        ):
            return False
        argv = (
            self.browser.executable,
            "-no-remote",
            "-profile",
            self.profile_dir,
            url,
        )
        try:
            self._process_factory(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError):
            return False
        return True


def _unit_payload(unit: Any) -> dict[str, Any]:
    payload = {
        "active": unit.active,
        "enabled": unit.enabled,
        "active_state": unit.active_state,
        "enabled_state": unit.enabled_state,
    }
    engine_pid = getattr(unit, "engine_pid", None)
    identity = getattr(unit, "process_identity", None)
    if engine_pid is not None:
        payload["engine_pid"] = engine_pid
    if isinstance(identity, WindowsProcessIdentity):
        payload["process_start_identity"] = identity.creation_time_us
    return payload


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


def _resolve_production_browser_launcher(
    *,
    origin: str | None = None,
    state_dir: str | None = None,
    profile_dir: str | None = None,
) -> BrowserLauncher:
    if os.name == "nt":
        if origin is None or not _is_safe_origin(origin):
            raise WebUIPlatformError(
                "BROWSER_LAUNCH_UNAVAILABLE",
                "exact managed Windows HTTPS origin is unavailable",
            )
        parsed = urlsplit(origin)
        hostname = parsed.hostname
        if hostname is None:
            raise WebUIPlatformError(
                "BROWSER_LAUNCH_UNAVAILABLE",
                "managed Windows HTTPS origin has no hostname",
            )
        if state_dir is None:
            raise WebUIPlatformError(
                "BROWSER_LAUNCH_UNAVAILABLE",
                "managed Windows state path is unavailable",
            )
        browser = discover_windows_browser(environ=os.environ)
        if browser is None or browser.family != "firefox":
            raise WebUIPlatformError(
                "BROWSER_LAUNCH_UNAVAILABLE",
                "Mozilla Firefox is required for managed Windows WebUI",
            )
        if profile_dir is None:
            raise WebUIPlatformError(
                "BROWSER_LAUNCH_UNAVAILABLE",
                "isolated Windows browser profile path is unavailable",
            )
        try:
            material = load_windows_tls_material(Path(state_dir) / "tls", hostname)
            ensure_firefox_profile_network_policy(
                profile_dir=profile_dir,
                hostname=hostname,
            )
            ensure_firefox_profile_trust(
                firefox_executable=browser.executable,
                profile_dir=profile_dir,
                tls_material=material,
            )
        except (WindowsFirefoxTrustError, WindowsTLSError) as exc:
            raise WebUIPlatformError(exc.code, exc.safe_message) from exc
        return WindowsBrowserLauncher(
            WindowsBrowserInstall(family="firefox", executable=browser.executable),
            profile_dir,
        )
    if origin is None or not _is_safe_origin(origin):
        raise WebUIPlatformError(
            "BROWSER_LAUNCH_UNAVAILABLE",
            "exact managed Linux HTTPS origin is unavailable",
        )
    if state_dir is None or profile_dir is None:
        raise WebUIPlatformError(
            "BROWSER_LAUNCH_UNAVAILABLE",
            "isolated Linux browser profile state is unavailable",
        )
    firefox = shutil.which("firefox") or shutil.which("firefox-esr")
    certutil = shutil.which("certutil")
    if (
        firefox is None
        or certutil is None
        or not os.path.isabs(firefox)
        or not os.path.isabs(certutil)
    ):
        raise WebUIPlatformError(
            "BROWSER_LAUNCH_UNAVAILABLE",
            "Mozilla Firefox/Firefox ESR and certutil are required for managed Linux WebUI",
        )
    parsed = urlsplit(origin)
    hostname = parsed.hostname
    assert hostname is not None
    root_path = str(
        PurePosixPath(state_dir)
        / "caddy"
        / "pki"
        / "authorities"
        / "local"
        / "root.crt"
    )
    try:
        validate_linux_firefox_managed_profile(
            certutil_path=certutil,
            firefox_executable=firefox,
            profile_dir=profile_dir,
            root_cert_path=root_path,
            hostname=hostname,
            owner_uid=os.getuid(),
        )
    except (LinuxSetupRecoveryRequired, OSError, ValueError) as exc:
        raise WebUIPlatformError(
            "BROWSER_LAUNCH_UNAVAILABLE",
            "isolated Linux Firefox profile trust is missing, foreign, or drifted",
        ) from exc
    return LinuxFirefoxBrowserLauncher(
        BrowserInstall(family="firefox", executable=firefox),
        profile_dir,
    )


def _is_safe_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return False
    common = bool(
        parsed.hostname is not None
        and port is not None
        and 8765 <= port <= 8774
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )
    if not common:
        return False
    if parsed.scheme == "http":
        return parsed.hostname == "127.0.0.1" and origin == f"http://127.0.0.1:{port}"
    return bool(
        parsed.scheme == "https"
        and parsed.hostname.startswith("factlane-")
        and parsed.hostname.endswith(".test")
        and origin == f"https://{parsed.hostname}:{port}"
    )
