from __future__ import annotations

import json
import base64
import hashlib
import os
import platform
import re
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, replace
from enum import Enum
from http.client import HTTPException, HTTPResponse
from importlib import resources
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from . import webui_windows as windows
from .webui_runtime import WebUIRuntimeConfig
from .webui_service import (
    ServiceSnapshot,
    ServiceStatus,
    is_legacy_endpoint_canonicalization,
    service_state_file_guard,
)
from .webui_windows import (
    WINDOWS_TASK_NAME,
    WindowsBrowserInstall,
    WindowsManagedConfig,
    WindowsOwnerPaths,
    WindowsTaskSchedulerSupervisor,
    WindowsWebUIError,
    discover_windows_browser,
    ensure_windows_owner_paths,
    render_windows_managed_config,
    windows_managed_python_arguments,
    windows_managed_python_executables,
    windows_owner_paths,
    write_owner_private_text_atomic,
)
from .windows_security import validate_windows_path_no_reparse


WEBUI_SERVICE_NAME = "factlane-webui.service"
OWNER_PANEL_LAUNCHER_LABEL = "FactLane Owner Panel"
LINUX_OWNER_PANEL_LAUNCHER_FILENAME = f"{OWNER_PANEL_LAUNCHER_LABEL}.desktop"
WINDOWS_OWNER_PANEL_LAUNCHER_FILENAME = f"{OWNER_PANEL_LAUNCHER_LABEL}.lnk"
WEBUI_PORT_MIN = 8765
WEBUI_PORT_MAX = 8774
OWNER_PRIVATE_ATOMIC_WRITE_CONTRACT = "OWNER_PRIVATE_ATOMIC_REGULAR_FILE_0600"
WINDOWS_OWNER_PRIVATE_ATOMIC_WRITE_CONTRACT = "OWNER_PRIVATE_ATOMIC_REGULAR_FILE_SID_DACL"

_INSTALLATION_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_HOSTNAME_RE = re.compile(r"factlane-([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.test")
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:@+,-]+")
_SUPPORTED_BROWSERS = (
    ("firefox", "firefox"),
    ("firefox-esr", "firefox"),
)
LINUX_SETUP_JOURNAL_FILENAME = "webui-setup-provisional.json"
LINUX_FIREFOX_MANAGED_USER_JS = "user.js"
LINUX_FIREFOX_TRUST_MANIFEST = ".factlane-trust.json"
LINUX_FIREFOX_CA_NICKNAME = "FactLane Local CA"
_DEFAULT_GETEUID: Callable[[], int] = getattr(os, "geteuid", lambda: -1)


class EndpointState(str, Enum):
    PROPOSED = "PROPOSED"
    CLAIMED = "CLAIMED"


class InstallerStepStatus(str, Enum):
    APPLIED = "APPLIED"
    BLOCKED = "BLOCKED"
    NEEDS_PRIVILEGED_INSTALLER = "NEEDS_PRIVILEGED_INSTALLER"
    INCOMPLETE_BROWSER_TRUST = "INCOMPLETE_BROWSER_TRUST"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    FAILED = "FAILED"


class ManagedServiceActivationStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    PORT_CONFLICT = "PORT_CONFLICT"
    FAILED = "FAILED"


class EndpointClaimError(RuntimeError):
    pass


class LinuxSetupRecoveryRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallerStepResult:
    status: InstallerStepStatus
    operation: str
    detail: str | None = None


@dataclass(frozen=True)
class SetupApplyResult:
    status: InstallerStepStatus
    endpoint_state: EndpointState
    claimed_port: int | None = None
    origin: str | None = None
    completed_operations: tuple[str, ...] = ()
    transaction_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class WindowsSetupPrecondition:
    """Exact pre-consent Windows setup custody captured without mutation."""

    owner_sid: str
    service_config: dict[str, Any]
    scheduled_task: dict[str, Any]
    launcher: dict[str, Any]
    service_state: dict[str, Any]
    setup_journal: dict[str, Any]


@dataclass(frozen=True)
class ManagedServiceActivationResult:
    status: ManagedServiceActivationStatus
    saved_endpoint: str | None = None
    detail: str | None = None


PortReceiver = Callable[[socket.socket, int], bool]


class PortClaim:
    """A retained loopback TCP claim that is never check-then-released.

    The bound socket remains open until `handoff()` transfers a duplicate descriptor
    to a receiver that confirms ownership, or until `close()` abandons the claim.
    """

    def __init__(self, sock: socket.socket, port: int) -> None:
        self._socket: socket.socket | None = sock
        self.port = port

    @classmethod
    def acquire(cls) -> PortClaim:
        for port in range(WEBUI_PORT_MIN, WEBUI_PORT_MAX + 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("127.0.0.1", port))
                sock.listen(1)
            except OSError:
                sock.close()
                continue
            return cls(sock, port)
        raise EndpointClaimError(
            f"no loopback endpoint from {WEBUI_PORT_MIN} through {WEBUI_PORT_MAX} could be claimed"
        )

    @property
    def active(self) -> bool:
        return self._socket is not None

    def handoff(self, receiver: PortReceiver) -> bool:
        if self._socket is None:
            raise EndpointClaimError("port claim is no longer active")
        transferred = self._socket.dup()
        try:
            accepted = receiver(transferred, self.port)
        except BaseException:
            transferred.close()
            raise
        if not accepted or transferred.fileno() < 0:
            transferred.close()
            return False
        try:
            bound_host, bound_port = transferred.getsockname()[:2]
        except OSError:
            transferred.close()
            return False
        if bound_host != "127.0.0.1" or bound_port != self.port:
            transferred.close()
            return False
        self._socket.close()
        self._socket = None
        return True

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None


class LinuxInstallerBackend(Protocol):
    def apply_operation(
        self,
        operation: PlannedOperation,
        *,
        plan: SetupPlan,
    ) -> InstallerStepResult: ...

    def read_saved_endpoint(self) -> str | None: ...

    def activate_candidate(self, *, plan: SetupPlan) -> ManagedServiceActivationResult: ...

    def stop_candidate(self, *, plan: SetupPlan) -> InstallerStepResult: ...


@dataclass(frozen=True)
class BrowserInstall:
    family: str
    executable: str

    def __post_init__(self) -> None:
        if self.family not in {"chromium", "firefox"}:
            raise ValueError("browser family must be chromium or firefox")
        _validate_absolute_path(self.executable, "browser executable")


@dataclass(frozen=True)
class LinuxFirefoxNetworkPolicyReceipt:
    status: str
    profile_dir: str
    user_js_path: str
    profile_created: bool
    user_js_created: bool


@dataclass(frozen=True)
class LinuxFirefoxTrustReceipt:
    status: str
    profile_dir: str
    manifest_path: str
    root_fingerprint_sha256: str
    db_initialized: bool
    certificate_imported: bool
    manifest_created: bool


@dataclass(frozen=True)
class LinuxCaddyBootstrapReceipt:
    status: str
    root_cert_path: str
    root_fingerprint_sha256: str
    caddy_state_created: bool
    caddy_state_device: int
    caddy_state_inode: int


@dataclass(frozen=True)
class InstallEnvironment:
    system: str
    systemd: bool
    caddy_path: str | None
    zenity_path: str | None
    browser: BrowserInstall | WindowsBrowserInstall | None
    euid: int

    def __post_init__(self) -> None:
        if not isinstance(self.systemd, bool):
            raise ValueError("systemd availability must be a boolean")
        for value, label in (
            (self.caddy_path, "Caddy executable"),
            (self.zenity_path, "zenity executable"),
        ):
            if value is not None:
                _validate_absolute_path(value, label)


@dataclass(frozen=True)
class SetupConsent:
    hostname_mapping: bool
    browser_profile_trust: bool
    user_lingering: bool
    windows_native_setup: bool = False

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, bool)
            for value in (
                self.hostname_mapping,
                self.browser_profile_trust,
                self.user_lingering,
                self.windows_native_setup,
            )
        ):
            raise ValueError("setup consent values must be explicit booleans")


@dataclass(frozen=True)
class ConsentRequirement:
    key: str
    explanation: str


@dataclass(frozen=True)
class SetupRequest:
    installation_id: str
    saved_port: int
    lingering_required: bool
    installing_user: str
    factlane_executable: str
    owner_config_home: str
    service_config_path: str
    caddy_config_path: str
    user_unit_path: str
    backend_socket: str
    runtime_dir: str
    state_dir: str
    caddy_state_dir: str
    browser_profile_dir: str
    db: str | None = None
    profile: str | None = None
    host_id: str | None = None
    native_control_socket: str | None = None
    launcher_path: str | None = None

    def __post_init__(self) -> None:
        webui_hostname(self.installation_id)
        _validate_saved_port(self.saved_port)
        windows_request = (
            self.native_control_socket is not None
            and self.native_control_socket.startswith("\\\\.\\pipe\\factlane-owner-")
        )
        if not isinstance(self.lingering_required, bool):
            raise ValueError("lingering_required must be a boolean")
        if not self.installing_user or any(
            ch.isspace() or ord(ch) < 32 for ch in self.installing_user
        ):
            raise ValueError("installing_user must be a non-empty account name without whitespace")
        configured_count = sum(
            value is not None for value in (self.db, self.profile, self.host_id)
        )
        if configured_count not in {0, 3}:
            raise ValueError(
                "db, profile, and host_id must be all configured or all absent"
            )
        if configured_count == 3:
            assert self.db is not None
            assert self.profile is not None
            assert self.host_id is not None
            if not all(
                isinstance(value, str)
                for value in (self.db, self.profile, self.host_id)
            ):
                raise ValueError(
                    "db, profile, and host_id must be strings when configured"
                )
            if windows_request:
                _validate_windows_absolute_path(self.db, "database path")
            else:
                _validate_absolute_path(self.db, "database path")
            if not self.profile or any(
                ch.isspace() or ord(ch) < 32 for ch in self.profile
            ):
                raise ValueError("profile must be a non-empty identifier without whitespace")
            if not self.host_id or any(
                ch.isspace() or ord(ch) < 32 for ch in self.host_id
            ):
                raise ValueError("host_id must be a non-empty identifier without whitespace")
        if self.native_control_socket is not None:
            if os.name == "nt":
                if not self.native_control_socket.startswith("\\\\.\\pipe\\factlane-owner-"):
                    raise ValueError("native_control_socket must be a FactLane local Windows Named Pipe")
            else:
                _validate_absolute_path(self.native_control_socket, "native control socket")
        if self.launcher_path is not None:
            if windows_request:
                _validate_windows_absolute_path(self.launcher_path, "Owner Panel launcher path")
                if PureWindowsPath(self.launcher_path).name != WINDOWS_OWNER_PANEL_LAUNCHER_FILENAME:
                    raise ValueError("Windows Owner Panel launcher must use the canonical shortcut filename")
            else:
                _validate_absolute_path(self.launcher_path, "Owner Panel launcher path")
                if PurePosixPath(self.launcher_path).name != LINUX_OWNER_PANEL_LAUNCHER_FILENAME:
                    raise ValueError("Linux Owner Panel launcher must use the canonical desktop filename")
        for value, label in (
            (self.factlane_executable, "FactLane executable"),
            (self.owner_config_home, "Owner config home"),
            (self.service_config_path, "service config path"),
            (self.caddy_config_path, "Caddy config path"),
            (self.user_unit_path, "user unit path"),
            (self.backend_socket, "backend socket"),
            (self.runtime_dir, "runtime directory"),
            (self.state_dir, "state directory"),
            (self.caddy_state_dir, "Caddy state directory"),
            (self.browser_profile_dir, "browser profile directory"),
        ):
            if windows_request:
                _validate_windows_absolute_path(value, label)
            else:
                _validate_absolute_path(value, label)
        if windows_request:
            canonical_config_home = str(PureWindowsPath(self.owner_config_home))
            if canonical_config_home.casefold() != self.owner_config_home.casefold():
                raise ValueError("Owner config home must be a canonical absolute Windows path")
            expected_service_config = str(PureWindowsPath(self.owner_config_home) / "webui.json")
            service_matches = self.service_config_path.casefold() == expected_service_config.casefold()
        else:
            canonical_config_home = str(PurePosixPath(self.owner_config_home))
            if canonical_config_home != self.owner_config_home:
                raise ValueError("Owner config home must be a canonical absolute POSIX path")
            expected_service_config = str(
                PurePosixPath(self.owner_config_home) / "factlane" / "webui.json"
            )
            service_matches = self.service_config_path == expected_service_config
        if not service_matches:
            raise ValueError(
                "service_config_path must equal the canonical runtime config path "
                "for the active platform"
            )


@dataclass(frozen=True)
class PlannedOperation:
    kind: str
    target: str
    desired_state: str
    preserve_unrelated_state: bool = True
    consent_key: str | None = None
    write_contract: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "WRITE_SERVICE_CONFIG":
            if self.write_contract not in {
                OWNER_PRIVATE_ATOMIC_WRITE_CONTRACT,
                WINDOWS_OWNER_PRIVATE_ATOMIC_WRITE_CONTRACT,
            }:
                raise ValueError(
                    "WRITE_SERVICE_CONFIG requires a fixed owner-private atomic write contract"
                )
        elif self.write_contract is not None:
            raise ValueError("write_contract is only valid for WRITE_SERVICE_CONFIG")


@dataclass(frozen=True)
class SetupPlan:
    hostname: str
    origin: str
    saved_port: int
    browser: BrowserInstall | WindowsBrowserInstall | None
    backend_socket: str
    caddy_state_dir: str
    caddy_config: str
    user_unit: str
    operations: tuple[PlannedOperation, ...]
    endpoint_state: EndpointState = EndpointState.PROPOSED


@dataclass(frozen=True)
class SetupReceipt:
    status: str
    blocked_reasons: tuple[str, ...]
    plan: SetupPlan | None

    @property
    def operations(self) -> tuple[PlannedOperation, ...]:
        if self.plan is None:
            return ()
        return self.plan.operations


def detect_install_environment(
    *,
    system_name: Callable[[], str] = platform.system,
    path_exists: Callable[[Path], bool] = Path.exists,
    which: Callable[[str], str | None] = shutil.which,
    geteuid: Callable[[], int] = _DEFAULT_GETEUID,
) -> InstallEnvironment:
    """Return read-only receiving-host facts used by setup planning."""

    system = system_name()
    if system == "Windows" and os.name == "nt":
        discovered = discover_windows_browser(environ=os.environ, which=which)
        return InstallEnvironment(
            system="Windows",
            systemd=False,
            caddy_path=None,
            zenity_path=None,
            browser=discovered,
            euid=-1,
        )
    systemd = (
        system == "Linux"
        and which("systemctl") is not None
        and path_exists(Path("/run/systemd/system"))
    )
    browser: BrowserInstall | None = None
    for executable_name, family in _SUPPORTED_BROWSERS:
        executable = which(executable_name)
        if executable:
            browser = BrowserInstall(family=family, executable=executable)
            break
    return InstallEnvironment(
        system=system,
        systemd=systemd,
        caddy_path=which("caddy"),
        zenity_path=which("zenity"),
        browser=browser,
        euid=geteuid(),
    )


def consent_requirements(*, lingering_required: bool) -> tuple[ConsentRequirement, ...]:
    requirements: list[ConsentRequirement] = []
    if lingering_required:
        requirements.append(
            ConsentRequirement(
                key="user_lingering",
                explanation=(
                    "Allow user lingering so the user service can activate at device boot "
                    "before login and remain available after logout."
                ),
            )
        )
    return tuple(requirements)


def webui_hostname(installation_id: str) -> str:
    if (
        not isinstance(installation_id, str)
        or not 1 <= len(installation_id) <= 54
        or _INSTALLATION_ID_RE.fullmatch(installation_id) is None
    ):
        raise ValueError(
            "installation_id must be a 1-54 character lowercase DNS label fragment"
        )
    return f"factlane-{installation_id}.test"


def render_caddy_config(
    *,
    hostname: str,
    port: int,
    backend_socket: str,
    caddy_state_dir: str,
) -> str:
    _validate_hostname(hostname)
    _validate_saved_port(port)
    _validate_absolute_path(backend_socket, "backend socket")
    _validate_absolute_path(caddy_state_dir, "Caddy state directory")

    socket_upstream = f"unix/{backend_socket}"
    return (
        "{\n"
        "    admin off\n"
        "    persist_config off\n"
        "    auto_https off\n"
        "    storage_clean_interval off\n"
        "}\n"
        "\n"
        f"http://127.0.0.1:{port} {{\n"
        "    bind 127.0.0.1\n"
        f"    reverse_proxy {_caddy_token(socket_upstream)}\n"
        "}\n"
    )


def render_user_unit(*, factlane_executable: str, service_config_path: str) -> str:
    _validate_absolute_path(factlane_executable, "FactLane executable")
    _validate_absolute_path(service_config_path, "service config path")
    template = (
        resources.files("factlane")
        .joinpath("data/factlane-webui.service")
        .read_text(encoding="utf-8")
    )
    rendered = template.replace(
        "@FACTLANE_EXECUTABLE@", _systemd_token(factlane_executable)
    )
    if "@FACTLANE_" in rendered:
        raise ValueError("packaged FactLane WebUI unit contains an unknown placeholder")
    return rendered


def _desktop_exec_token(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ValueError("desktop launcher executable must be bounded text")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def render_linux_owner_panel_launcher(*, factlane_executable: str) -> str:
    """Render one shell-free launcher with a visible browser-neutral link surface."""

    _validate_absolute_path(factlane_executable, "FactLane executable")
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={OWNER_PANEL_LAUNCHER_LABEL}\n"
        "Comment=Open the local FactLane Owner Panel\n"
        f"Exec={_desktop_exec_token(factlane_executable)} launch webui\n"
        "Terminal=true\n"
        "Categories=Utility;\n"
        "StartupNotify=true\n"
    )


def render_windows_owner_panel_launcher(*, factlane_executable: str, owner_config_home: str) -> str:
    """Return the bounded shortcut specification consumed by the native installer backend."""

    _validate_windows_absolute_path(
        factlane_executable,
        "FactLane environment executable",
    )
    executable = PureWindowsPath(factlane_executable)
    python_console = executable.with_name("python.exe")
    _validate_windows_absolute_path(owner_config_home, "Owner config home")
    working_directory = PureWindowsPath(owner_config_home).parent
    return json.dumps(
        {
            "arguments": "-m factlane.webui_launcher",
            "description": "Open the local FactLane Owner Panel",
            "target": str(python_console),
            "working_directory": str(working_directory),
        },
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def plan_webui_setup(
    request: SetupRequest,
    environment: InstallEnvironment,
    consent: SetupConsent,
) -> SetupReceipt:
    """Build a deterministic setup plan without applying any host mutation."""

    if environment.system == "Windows":
        return _plan_windows_webui_setup(request, environment, consent)

    blocked_reasons = list(_environment_blockers(environment))
    if request.lingering_required and not consent.user_lingering:
        blocked_reasons.append(
            "explicit consent for user lingering is required for boot-before-login activation"
        )
    if blocked_reasons:
        return SetupReceipt(
            status="BLOCKED",
            blocked_reasons=tuple(blocked_reasons),
            plan=None,
        )

    browser: BrowserInstall | None = None
    hostname = webui_hostname(request.installation_id)
    origin = f"http://127.0.0.1:{request.saved_port}"
    caddy_path = environment.caddy_path
    if caddy_path is None:  # Defensive: dependency blockers above make this unreachable.
        return SetupReceipt(
            status="BLOCKED",
            blocked_reasons=("Caddy is required for the installation-private loopback gateway",),
            plan=None,
        )
    runtime_config = WebUIRuntimeConfig(
        db=request.db,
        profile=request.profile,
        host_id=request.host_id,
        origin=origin,
        backend_socket=request.backend_socket,
        caddy_path=caddy_path,
        caddyfile=request.caddy_config_path,
        runtime_dir=request.runtime_dir,
        state_dir=request.state_dir,
    )
    # Preserve the exact pre-portability Linux on-disk schema.  Windows
    # managed installs use the explicit platform/transport schema, but a
    # Linux setup must not rewrite its already-qualified nine-field contract
    # merely because WebUIRuntimeConfig now carries cross-platform defaults.
    runtime_config_json = json.dumps(
        {
            "db": runtime_config.db,
            "profile": runtime_config.profile,
            "host_id": runtime_config.host_id,
            "origin": runtime_config.origin,
            "backend_socket": runtime_config.backend_socket,
            "caddy_path": runtime_config.caddy_path,
            "caddyfile": runtime_config.caddyfile,
            "runtime_dir": runtime_config.runtime_dir,
            "state_dir": runtime_config.state_dir,
        },
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    caddy_config = render_caddy_config(
        hostname=hostname,
        port=request.saved_port,
        backend_socket=request.backend_socket,
        caddy_state_dir=request.caddy_state_dir,
    )
    user_unit = render_user_unit(
        factlane_executable=request.factlane_executable,
        service_config_path=request.service_config_path,
    )
    setup_journal = str(linux_setup_journal_path(request.state_dir))
    journal_state = render_linux_setup_journal(
        request=request,
        browser=browser,
        hostname=hostname,
    )
    operations: list[PlannedOperation] = [
        PlannedOperation(
            kind="PERSIST_PROVISIONAL_SETUP",
            target=setup_journal,
            desired_state=journal_state,
        ),
        PlannedOperation(
            kind="WRITE_SERVICE_CONFIG",
            target=request.service_config_path,
            desired_state=runtime_config_json,
            write_contract=OWNER_PRIVATE_ATOMIC_WRITE_CONTRACT,
        ),
        PlannedOperation(
            kind="WRITE_CADDY_CONFIG",
            target=request.caddy_config_path,
            desired_state=caddy_config,
        ),
        PlannedOperation(
            kind="INSTALL_USER_UNIT",
            target=request.user_unit_path,
            desired_state=user_unit,
        ),
    ]
    if request.lingering_required:
        operations.append(
            PlannedOperation(
                kind="ENABLE_USER_LINGERING",
                target=request.installing_user,
                desired_state=(
                    "Enable lingering for this installing user only so the user manager "
                    "can activate the service before login."
                ),
                consent_key="user_lingering",
            )
        )
    operations.append(
        PlannedOperation(
            kind="REGISTER_USER_SERVICE",
            target=WEBUI_SERVICE_NAME,
            desired_state=(
                f"Register and enable the rendered user unit at {request.user_unit_path}; "
                "activation readiness must be verified separately."
            ),
        )
    )
    if request.launcher_path is not None:
        operations.append(
            PlannedOperation(
                kind="INSTALL_OWNER_LAUNCHER",
                target=request.launcher_path,
                desired_state=render_linux_owner_panel_launcher(
                    factlane_executable=request.factlane_executable
                ),
            )
        )

    return SetupReceipt(
        status="READY_TO_APPLY",
        blocked_reasons=(),
        plan=SetupPlan(
            hostname=hostname,
            origin=origin,
            saved_port=request.saved_port,
            browser=browser,
            backend_socket=request.backend_socket,
            caddy_state_dir=request.caddy_state_dir,
            caddy_config=caddy_config,
            user_unit=user_unit,
            operations=tuple(operations),
        ),
    )


def _plan_windows_webui_setup(
    request: SetupRequest,
    environment: InstallEnvironment,
    consent: SetupConsent,
) -> SetupReceipt:
    blocked_reasons = list(_environment_blockers(environment))
    if not consent.windows_native_setup:
        blocked_reasons.append("explicit consent for owner-private Windows setup and current-user task registration is required")
    if blocked_reasons:
        return SetupReceipt(status="BLOCKED", blocked_reasons=tuple(blocked_reasons), plan=None)
    if request.native_control_socket is None:
        return SetupReceipt(
            status="BLOCKED",
            blocked_reasons=("native Windows control endpoint is missing",),
            plan=None,
        )
    browser: WindowsBrowserInstall | None = None
    hostname = webui_hostname(request.installation_id)
    origin = f"http://127.0.0.1:{request.saved_port}"
    managed = WindowsManagedConfig(
        installation_id=request.installation_id,
        origin=origin,
        native_control_pipe=request.native_control_socket,
        runtime_dir=request.runtime_dir,
        state_dir=request.state_dir,
        browser_profile_dir=request.browser_profile_dir,
        db=request.db,
        profile=request.profile,
        host_id=request.host_id,
    )
    operations: tuple[PlannedOperation, ...] = (
        PlannedOperation(
            kind="ENSURE_WINDOWS_OWNER_STATE",
            target=request.state_dir,
            desired_state="Protect FactLane managed state with the current-user SID and owner-only DACL custody.",
            consent_key="windows_native_setup",
        ),
        PlannedOperation(
            kind="WRITE_SERVICE_CONFIG",
            target=request.service_config_path,
            desired_state=render_windows_managed_config(managed),
            write_contract=WINDOWS_OWNER_PRIVATE_ATOMIC_WRITE_CONTRACT,
            consent_key="windows_native_setup",
        ),
        PlannedOperation(
            kind="REGISTER_WINDOWS_TASK",
            target=WINDOWS_TASK_NAME,
            desired_state=(
                f"Register or update one current-user Task Scheduler entry with the exact base Python executable "
                f"and an in-process bootstrap bound to FactLane environment {request.factlane_executable!r}."
            ),
            consent_key="windows_native_setup",
        ),
    )
    if request.launcher_path is not None:
        operations = (*operations, PlannedOperation(
            kind="INSTALL_OWNER_LAUNCHER",
            target=request.launcher_path,
            desired_state=render_windows_owner_panel_launcher(
                factlane_executable=request.factlane_executable,
                owner_config_home=request.owner_config_home,
            ),
            consent_key="windows_native_setup",
        ))
    return SetupReceipt(
        status="READY_TO_APPLY",
        blocked_reasons=(),
        plan=SetupPlan(
            hostname=hostname,
            origin=origin,
            saved_port=request.saved_port,
            browser=browser,
            backend_socket=request.backend_socket,
            caddy_state_dir=request.caddy_state_dir,
            caddy_config="",
            user_unit="",
            operations=operations,
        ),
    )


def apply_webui_setup(
    receipt: SetupReceipt,
    consent: SetupConsent,
    backend: LinuxInstallerBackend,
) -> SetupApplyResult:
    """Apply only a previously READY_TO_APPLY plan through an injected backend.

    The managed FactLane service/Caddy process is the production endpoint claimer.
    First install tries 8765..8774 in order. A previously saved endpoint gets exactly
    one activation attempt. Candidate config is provisional; only a READY activation
    whose service state reports the exact candidate origin is returned as CLAIMED.
    """

    if receipt.status != "READY_TO_APPLY" or receipt.plan is None:
        return SetupApplyResult(
            status=InstallerStepStatus.BLOCKED,
            endpoint_state=EndpointState.PROPOSED,
            detail="setup receipt is not READY_TO_APPLY",
        )
    plan = receipt.plan
    if _is_windows_setup_plan(plan):
        return _apply_windows_webui_setup(plan, consent, backend)
    if plan.endpoint_state is not EndpointState.PROPOSED:
        return SetupApplyResult(
            status=InstallerStepStatus.BLOCKED,
            endpoint_state=plan.endpoint_state,
            detail="setup plan has already crossed the proposed endpoint boundary",
        )

    for operation in plan.operations:
        if operation.consent_key is not None and not _consent_granted(
            consent, operation.consent_key
        ):
            return SetupApplyResult(
                status=InstallerStepStatus.BLOCKED,
                endpoint_state=EndpointState.PROPOSED,
                detail=f"explicit consent is missing for {operation.consent_key}",
            )
    completed: list[str] = []

    def failed(
        status: InstallerStepStatus,
        detail: str | None,
    ) -> SetupApplyResult:
        rollback = getattr(backend, "rollback_setup", None)
        if callable(rollback) and completed:
            rolled_back = rollback(plan=plan, completed_operations=tuple(completed))
            if rolled_back.status is not InstallerStepStatus.APPLIED:
                return SetupApplyResult(
                    status=InstallerStepStatus.RECOVERY_REQUIRED,
                    endpoint_state=EndpointState.PROPOSED,
                    completed_operations=tuple(completed),
                    detail=rolled_back.detail or "pre-claim rollback requires explicit recovery",
                )
        return SetupApplyResult(
            status=status,
            endpoint_state=EndpointState.PROPOSED,
            completed_operations=tuple(completed),
            detail=detail,
        )

    def recovery_without_rollback(detail: str) -> SetupApplyResult:
        seal_recovery = getattr(backend, "mark_recovery_required", None)
        if callable(seal_recovery):
            sealed = seal_recovery(plan=plan, detail=detail)
            if sealed.status is not InstallerStepStatus.APPLIED:
                detail = (
                    f"{detail}; durable recovery state could not be sealed: "
                    f"{sealed.detail or sealed.status.value}"
                )
        return SetupApplyResult(
            status=InstallerStepStatus.RECOVERY_REQUIRED,
            endpoint_state=EndpointState.PROPOSED,
            completed_operations=tuple(completed),
            detail=detail,
        )

    try:
        crossing_kinds = {
            "PERSIST_PROVISIONAL_SETUP",
            "BOOTSTRAP_CADDY_CA",
            "ENSURE_ISOLATED_PROFILE_NETWORK",
            "ENSURE_ISOLATED_PROFILE_TRUST",
            "ENABLE_USER_LINGERING",
        }
        endpoint_writes = {"WRITE_SERVICE_CONFIG", "WRITE_CADDY_CONFIG"}
        post_activation_kinds = {"INSTALL_OWNER_LAUNCHER"}
        ordered_prerequisites = (
            [op for op in plan.operations if op.kind in crossing_kinds]
            + [
                op
                for op in plan.operations
                if op.kind not in crossing_kinds
                and op.kind not in endpoint_writes
                and op.kind not in post_activation_kinds
            ]
        )
        for operation in ordered_prerequisites:
            result = backend.apply_operation(operation, plan=plan)
            if result.status is not InstallerStepStatus.APPLIED:
                return failed(result.status, result.detail)
            completed.append(operation.kind)
        saved_endpoint = backend.read_saved_endpoint()
        if saved_endpoint is not None:
            try:
                saved_port = _saved_endpoint_port_for_plan(saved_endpoint, plan)
            except ValueError as exc:
                return failed(InstallerStepStatus.RECOVERY_REQUIRED, str(exc))
            candidate_ports = (saved_port,)
        else:
            candidate_ports = tuple(range(WEBUI_PORT_MIN, WEBUI_PORT_MAX + 1))

        for port in candidate_ports:
            candidate_plan = _rebind_plan_endpoint(plan, port, EndpointState.PROPOSED)
            for endpoint_write_kind in ("WRITE_CADDY_CONFIG", "WRITE_SERVICE_CONFIG"):
                operation = next(
                    item
                    for item in candidate_plan.operations
                    if item.kind == endpoint_write_kind
                )
                written = backend.apply_operation(operation, plan=candidate_plan)
                if written.status is not InstallerStepStatus.APPLIED:
                    return failed(written.status, written.detail)
                completed.append(operation.kind)

            activation = backend.activate_candidate(plan=candidate_plan)
            if activation.status is ManagedServiceActivationStatus.READY:
                if activation.saved_endpoint != candidate_plan.origin:
                    stopped = backend.stop_candidate(plan=candidate_plan)
                    detail = "managed service reported READY without the exact claimed candidate endpoint"
                    if stopped.status is not InstallerStepStatus.APPLIED:
                        return recovery_without_rollback(
                            detail + "; managed service stop also failed; no rollback was attempted while liveness is ambiguous"
                        )
                    return failed(InstallerStepStatus.FAILED, detail)
                for operation in candidate_plan.operations:
                    if operation.kind not in post_activation_kinds:
                        continue
                    installed = backend.apply_operation(operation, plan=candidate_plan)
                    if installed.status is not InstallerStepStatus.APPLIED:
                        stopped = backend.stop_candidate(plan=candidate_plan)
                        if stopped.status is not InstallerStepStatus.APPLIED:
                            return recovery_without_rollback(
                                "Owner Panel launcher failed and the managed service could not be proven stopped"
                            )
                        return failed(
                            installed.status,
                            installed.detail or "Owner Panel launcher installation failed",
                        )
                    completed.append(operation.kind)
                finalize = getattr(backend, "finalize_setup", None)
                if callable(finalize):
                    finalized = finalize(plan=candidate_plan)
                    if finalized.status is not InstallerStepStatus.APPLIED:
                        stopped = backend.stop_candidate(plan=candidate_plan)
                        if stopped.status is not InstallerStepStatus.APPLIED:
                            return recovery_without_rollback(
                                "final setup publication failed and the managed service could not be proven stopped"
                            )
                        return failed(
                            InstallerStepStatus.RECOVERY_REQUIRED,
                            finalized.detail or "final setup publication could not be recorded durably",
                        )
                return SetupApplyResult(
                    status=InstallerStepStatus.APPLIED,
                    endpoint_state=EndpointState.CLAIMED,
                    claimed_port=port,
                    origin=candidate_plan.origin,
                    completed_operations=tuple(completed),
                )

            stopped = backend.stop_candidate(plan=candidate_plan)
            if stopped.status is not InstallerStepStatus.APPLIED:
                return recovery_without_rollback(
                    "candidate activation failed and managed service could not be stopped safely; no rollback was attempted while liveness is ambiguous"
                )
            if activation.status is ManagedServiceActivationStatus.PORT_CONFLICT:
                if saved_endpoint is not None:
                    return failed(
                        InstallerStepStatus.RECOVERY_REQUIRED,
                        "saved endpoint is unavailable; explicit endpoint recovery is required",
                    )
                continue
            return failed(
                InstallerStepStatus.FAILED,
                activation.detail or "managed service candidate activation failed",
            )

        return failed(
            InstallerStepStatus.FAILED,
            f"no managed endpoint from {WEBUI_PORT_MIN} through {WEBUI_PORT_MAX} could be activated",
        )
    except Exception as exc:
        return failed(
            InstallerStepStatus.FAILED,
            f"setup apply failed safely: {type(exc).__name__}",
        )


def _apply_windows_webui_setup(
    plan: SetupPlan,
    consent: SetupConsent,
    backend: LinuxInstallerBackend,
) -> SetupApplyResult:
    if plan.endpoint_state is not EndpointState.PROPOSED:
        return SetupApplyResult(
            status=InstallerStepStatus.BLOCKED,
            endpoint_state=plan.endpoint_state,
            detail="Windows setup plan already crossed the proposed endpoint boundary",
        )
    for operation in plan.operations:
        if operation.consent_key is not None and not _consent_granted(consent, operation.consent_key):
            return SetupApplyResult(
                status=InstallerStepStatus.BLOCKED,
                endpoint_state=EndpointState.PROPOSED,
                detail=f"explicit consent is missing for {operation.consent_key}",
            )
    completed: list[str] = []
    try:
        with backend.mutation_guard():
            try:
                backend.prepare_setup(plan=plan)
                for operation in plan.operations:
                    result = backend.apply_operation(operation, plan=plan)
                    if result.status is not InstallerStepStatus.APPLIED:
                        raise WindowsWebUIError("WINDOWS_SETUP_OPERATION_FAILED", result.detail or operation.kind)
                    completed.append(operation.kind)
                backend.commit_setup(plan=plan)
            except Exception as exc:
                primary = str(exc) if isinstance(exc, WindowsWebUIError) else type(exc).__name__
                try:
                    rollback = backend.rollback_setup(plan=plan, completed_operations=tuple(completed))
                    conflict = rollback.status is not InstallerStepStatus.APPLIED
                    recovery_detail = rollback.detail or rollback.status.value
                except Exception as rollback_exc:
                    conflict = True
                    recovery_detail = type(rollback_exc).__name__
                return SetupApplyResult(
                    status=(InstallerStepStatus.RECOVERY_REQUIRED
                            if conflict or (isinstance(exc, WindowsWebUIError) and exc.code == "WINDOWS_SETUP_RECOVERY_REQUIRED")
                            else InstallerStepStatus.FAILED),
                    endpoint_state=EndpointState.PROPOSED,
                    completed_operations=tuple(completed),
                    transaction_id=getattr(backend, "transaction_id", None),
                    detail=f"Windows setup failed: {primary}; rollback: {recovery_detail}",
                )
        return SetupApplyResult(
            status=InstallerStepStatus.APPLIED,
            endpoint_state=EndpointState.PROPOSED,
            origin=plan.origin,
            completed_operations=tuple(completed),
            transaction_id=getattr(backend, "transaction_id", None),
            detail="Windows lifecycle substrate configured; endpoint claim remains owned by the managed runtime",
        )
    except Exception as exc:
        return SetupApplyResult(
            status=InstallerStepStatus.BLOCKED,
            endpoint_state=EndpointState.PROPOSED,
            detail=f"Windows setup mutation guard unavailable: {type(exc).__name__}",
        )


CommandRunner = Callable[[tuple[str, ...]], int]
ReadCommandRunner = Callable[[tuple[str, ...]], tuple[int, str]]
ServiceStateReader = Callable[[], ServiceSnapshot]
ActivationProbe = Callable[[str], ManagedServiceActivationResult]
InstallClock = Callable[[], float]
InstallSleep = Callable[[float], None]
_ServiceGovernanceToken = tuple[int, int, str | None, str | None, str | None]


def _service_governance_token(raw: bytes | None) -> _ServiceGovernanceToken:
    if raw is None:
        return (0, 0, None, None, None)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinuxSetupRecoveryRequired("managed service state is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise LinuxSetupRecoveryRequired("managed service state is not an object")
    revision = payload.get("intent_revision")
    governance_revision = payload.get("governance_revision", 0)
    stopped_boot_id = payload.get("stopped_boot_id")
    maintenance_operation_id = payload.get("maintenance_operation_id")
    status = payload.get("status")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise LinuxSetupRecoveryRequired("managed service intent revision is invalid")
    if (
        isinstance(governance_revision, bool)
        or not isinstance(governance_revision, int)
        or governance_revision < 0
    ):
        raise LinuxSetupRecoveryRequired("managed service governance revision is invalid")
    if stopped_boot_id is not None and not isinstance(stopped_boot_id, str):
        raise LinuxSetupRecoveryRequired("managed service stop marker is invalid")
    if maintenance_operation_id is not None and not isinstance(maintenance_operation_id, str):
        raise LinuxSetupRecoveryRequired("managed service maintenance owner is invalid")
    if not isinstance(status, str):
        raise LinuxSetupRecoveryRequired("managed service status is invalid")
    protected_status = (
        status
        if status
        in {
            ServiceStatus.STOPPED_UNTIL_REBOOT.value,
            ServiceStatus.MAINTENANCE.value,
            ServiceStatus.RECOVERY_REQUIRED.value,
        }
        else None
    )
    return (
        revision,
        governance_revision,
        stopped_boot_id,
        maintenance_operation_id,
        protected_status,
    )


def _snapshot_governance_token(snapshot: ServiceSnapshot) -> _ServiceGovernanceToken:
    protected_status = (
        snapshot.status.value
        if snapshot.status
        in {
            ServiceStatus.STOPPED_UNTIL_REBOOT,
            ServiceStatus.MAINTENANCE,
            ServiceStatus.RECOVERY_REQUIRED,
        }
        else None
    )
    return (
        snapshot.intent_revision,
        snapshot.governance_revision,
        snapshot.stopped_boot_id,
        snapshot.maintenance_operation_id,
        protected_status,
    )


def _reconciled_service_governance_baseline(
    prepared: _ServiceGovernanceToken,
    reconciled: _ServiceGovernanceToken,
) -> _ServiceGovernanceToken:
    """Accept only an unchanged baseline or exact stale-boot stop reconciliation.

    Reconstructing WebUIServiceState may legitimately clear a STOPPED_UNTIL_REBOOT
    marker from a previous boot.  That transition advances governance exactly once
    without creating Owner intent.  Any intent revision change, maintenance/recovery
    transition, or other governance drift belongs to the Owner/runtime and must not
    be silently adopted as setup's candidate baseline.
    """

    if reconciled == prepared:
        return reconciled
    (
        prepared_intent,
        prepared_governance,
        prepared_stopped_boot,
        prepared_maintenance,
        prepared_status,
    ) = prepared
    (
        reconciled_intent,
        reconciled_governance,
        reconciled_stopped_boot,
        reconciled_maintenance,
        reconciled_status,
    ) = reconciled
    stale_boot_stop_reconciled = (
        prepared_stopped_boot is not None
        and prepared_maintenance is None
        and prepared_status == ServiceStatus.STOPPED_UNTIL_REBOOT.value
        and reconciled_intent == prepared_intent
        and reconciled_governance == prepared_governance + 1
        and reconciled_stopped_boot is None
        and reconciled_maintenance is None
        and reconciled_status is None
    )
    if stale_boot_stop_reconciled:
        return reconciled
    raise LinuxSetupRecoveryRequired(
        "governed service state changed before candidate activation baseline"
    )


class ProductionLinuxInstallerBackend:
    """Linux installer backend with fixed-argv, owner-scoped setup operations."""

    def __init__(
        self,
        *,
        systemctl_path: str,
        loginctl_path: str | None = None,
        certutil_path: str | None = None,
        caddy_path: str | None = None,
        owner_uid: int | None = None,
        command_runner: CommandRunner | None = None,
        read_command_runner: ReadCommandRunner | None = None,
        caddy_bootstrap_process_factory: Callable[[tuple[str, ...]], object] | None = None,
        service_state_reader: ServiceStateReader | None = None,
        activation_probe: ActivationProbe | None = None,
        clock: InstallClock = time.monotonic,
        sleep: InstallSleep = time.sleep,
        activation_timeout: float = 30.0,
        poll_interval: float = 0.1,
    ) -> None:
        _validate_absolute_path(systemctl_path, "systemctl executable")
        for value, label in (
            (loginctl_path, "loginctl executable"),
            (certutil_path, "certutil executable"),
            (caddy_path, "Caddy executable"),
        ):
            if value is not None:
                _validate_absolute_path(value, label)
        uid = _DEFAULT_GETEUID() if owner_uid is None else owner_uid
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            raise ValueError("owner_uid must be a non-negative integer")
        if activation_timeout <= 0:
            raise ValueError("activation_timeout must be positive")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._systemctl_path = systemctl_path
        self._loginctl_path = loginctl_path
        self._certutil_path = certutil_path
        self._caddy_path = caddy_path
        self._owner_uid = uid
        self._command_runner = command_runner or _run_argv
        self._read_command_runner = read_command_runner or _run_argv_capture
        self._caddy_bootstrap_process_factory = caddy_bootstrap_process_factory
        self._service_state_reader = service_state_reader
        self._activation_probe = activation_probe
        self._clock = clock
        self._sleep = sleep
        self._activation_timeout = activation_timeout
        self._poll_interval = poll_interval
        self._journal_path: Path | None = None
        self._attempt_file_targets: set[str] = set()
        self._attempt_endpoint_file_targets: set[str] = set()
        self._attempt_linger_changed = False
        self._attempt_service_enable_changed = False
        self._attempt_profile_created = False
        self._attempt_profile_created_files: set[str] = set()
        self._attempt_trust_imported = False
        self._attempt_caddy_state_created = False
        self._activation_state_path: Path | None = None
        self._activation_service_governance: _ServiceGovernanceToken | None = None
        self._activation_state_custody_ready = False

    def apply_operation(
        self,
        operation: PlannedOperation,
        *,
        plan: SetupPlan,
    ) -> InstallerStepResult:
        if operation.kind == "PERSIST_PROVISIONAL_SETUP":
            try:
                journal = _ensure_linux_setup_journal(
                    Path(operation.target),
                    operation.desired_state,
                    owner_uid=self._owner_uid,
                )
                self._journal_path = Path(operation.target)
                if journal.get("phase") in {"ACTIVATING", "RECOVERY_REQUIRED"}:
                    raise LinuxSetupRecoveryRequired(
                        "provisional setup journal requires explicit recovery before retry"
                    )
                if journal.get("phase") == "CLAIMED":
                    journal["phase"] = "PROVISIONAL"
                    journal["ownership"] = _new_linux_setup_ownership()
                    _atomic_owner_file_write(
                        self._journal_path,
                        json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n",
                        private_parent=True,
                    )
                ownership = journal.get("ownership")
                if not isinstance(ownership, dict):
                    raise LinuxSetupRecoveryRequired("provisional setup ownership state is invalid")
                caddy_state = Path(plan.caddy_state_dir)
                if ownership.get("caddy_state_preexisting") is None:
                    try:
                        caddy_info = _require_owner_private_directory(
                            caddy_state,
                            owner_uid=self._owner_uid,
                        )
                    except FileNotFoundError:
                        caddy_preexisting = False
                        caddy_dev = caddy_ino = None
                    else:
                        caddy_preexisting = True
                        caddy_dev = int(caddy_info.st_dev)
                        caddy_ino = int(caddy_info.st_ino)
                    ownership.update(
                        {
                            "caddy_state_preexisting": caddy_preexisting,
                            "caddy_state_device": caddy_dev,
                            "caddy_state_inode": caddy_ino,
                        }
                    )
                # SetupPlan carries browser identity; the exact profile path remains bound in
                # the durable journal rendered from SetupRequest.
                profile = Path(str(journal.get("browser_profile_dir")))
                if (
                    journal.get("browser_family") is not None
                    and ownership.get("profile_preexisting") is None
                ):
                    try:
                        profile_info = _require_owner_private_directory(
                            profile,
                            owner_uid=self._owner_uid,
                        )
                    except FileNotFoundError:
                        profile_preexisting = False
                        profile_dev = profile_ino = None
                    else:
                        profile_preexisting = True
                        profile_dev = int(profile_info.st_dev)
                        profile_ino = int(profile_info.st_ino)
                    user_js = profile / LINUX_FIREFOX_MANAGED_USER_JS
                    nss_files = tuple(profile / name for name in ("cert9.db", "key4.db", "pkcs11.txt"))
                    if profile_preexisting:
                        try:
                            _require_owner_private_regular_file(user_js, owner_uid=self._owner_uid)
                        except FileNotFoundError:
                            user_js_preexisting = False
                        else:
                            user_js_preexisting = True
                        nss_present = [item.exists() for item in nss_files]
                        if any(nss_present) and not all(nss_present):
                            raise LinuxSetupRecoveryRequired("pre-existing Firefox NSS state is partial")
                        if all(nss_present):
                            for item in nss_files:
                                _require_owner_private_regular_file(item, owner_uid=self._owner_uid)
                        nss_preexisting = all(nss_present)
                        manifest = profile / LINUX_FIREFOX_TRUST_MANIFEST
                        try:
                            _require_owner_private_regular_file(manifest, owner_uid=self._owner_uid)
                        except FileNotFoundError:
                            trust_preexisting = False
                        else:
                            trust_preexisting = True
                    else:
                        user_js_preexisting = False
                        nss_preexisting = False
                        trust_preexisting = False
                    ownership.update(
                        {
                            "profile_preexisting": profile_preexisting,
                            "profile_device": profile_dev,
                            "profile_inode": profile_ino,
                            "user_js_preexisting": user_js_preexisting,
                            "nss_preexisting": nss_preexisting,
                            "trust_preexisting": trust_preexisting,
                        }
                    )
                _update_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                    ownership_updates=ownership,
                )
                files = ownership.get("files")
                if isinstance(files, dict):
                    self._attempt_file_targets = set(files)
                self._attempt_linger_changed = ownership.get("linger_changed") is True
                self._attempt_service_enable_changed = ownership.get("service_enable_changed") is True
                self._attempt_profile_created = ownership.get("profile_created") is True
                profile_files = ownership.get("profile_created_files")
                if isinstance(profile_files, list):
                    self._attempt_profile_created_files = {
                        item for item in profile_files if isinstance(item, str)
                    }
                self._attempt_trust_imported = ownership.get("trust_imported") is True
                self._attempt_caddy_state_created = ownership.get("caddy_state_created") is True
            except LinuxSetupRecoveryRequired as exc:
                return InstallerStepResult(
                    status=InstallerStepStatus.RECOVERY_REQUIRED,
                    operation=operation.kind,
                    detail=str(exc),
                )
            except (OSError, ValueError) as exc:
                return InstallerStepResult(
                    status=InstallerStepStatus.FAILED,
                    operation=operation.kind,
                    detail=f"provisional setup journal failed safely: {type(exc).__name__}",
                )
            return InstallerStepResult(
                status=InstallerStepStatus.APPLIED,
                operation=operation.kind,
            )
        if operation.kind == "BOOTSTRAP_CADDY_CA":
            if self._journal_path is None:
                return InstallerStepResult(
                    status=InstallerStepStatus.RECOVERY_REQUIRED,
                    operation=operation.kind,
                    detail="provisional setup journal must be durable before Caddy CA bootstrap",
                )
            if self._caddy_path is None:
                return InstallerStepResult(
                    status=InstallerStepStatus.FAILED,
                    operation=operation.kind,
                    detail="Caddy executable is unavailable for private CA bootstrap",
                )
            try:
                journal = _parse_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                )
                ownership = journal["ownership"]
                assert isinstance(ownership, dict)
                caddy_state = Path(plan.caddy_state_dir)
                root_path = caddy_state / "pki" / "authorities" / "local" / "root.crt"
                caddy_preexisting = ownership.get("caddy_state_preexisting")
                caddy_created = ownership.get("caddy_state_created") is True
                caddy_create_intent = ownership.get("caddy_state_create_intent") is True
                if caddy_preexisting is False and not caddy_created:
                    if caddy_state.exists():
                        if not caddy_create_intent:
                            raise LinuxSetupRecoveryRequired(
                                "Caddy state appeared before a durable create intent"
                            )
                        info = _require_owner_private_directory(
                            caddy_state,
                            owner_uid=self._owner_uid,
                        )
                        try:
                            next(caddy_state.iterdir())
                        except StopIteration:
                            pass
                        else:
                            raise LinuxSetupRecoveryRequired(
                                "Caddy state appeared after create intent with unreceipted contents"
                            )
                        _update_linux_setup_journal(
                            self._journal_path,
                            owner_uid=self._owner_uid,
                            ownership_updates={
                                "caddy_state_created": True,
                                "caddy_state_device": int(info.st_dev),
                                "caddy_state_inode": int(info.st_ino),
                            },
                        )
                        ownership["caddy_state_created"] = True
                        ownership["caddy_state_device"] = int(info.st_dev)
                        ownership["caddy_state_inode"] = int(info.st_ino)
                        caddy_created = True
                    else:
                        if not caddy_create_intent:
                            _update_linux_setup_journal(
                                self._journal_path,
                                owner_uid=self._owner_uid,
                                ownership_updates={"caddy_state_create_intent": True},
                            )
                            ownership["caddy_state_create_intent"] = True
                            caddy_create_intent = True
                        _ensure_real_directory_chain(caddy_state.parent)
                        caddy_state.mkdir(mode=0o700)
                        os.chmod(caddy_state, 0o700)
                        info = _require_owner_private_directory(
                            caddy_state,
                            owner_uid=self._owner_uid,
                        )
                        _update_linux_setup_journal(
                            self._journal_path,
                            owner_uid=self._owner_uid,
                            ownership_updates={
                                "caddy_state_created": True,
                                "caddy_state_device": int(info.st_dev),
                                "caddy_state_inode": int(info.st_ino),
                            },
                        )
                        ownership["caddy_state_created"] = True
                        ownership["caddy_state_device"] = int(info.st_dev)
                        ownership["caddy_state_inode"] = int(info.st_ino)
                        caddy_created = True
                elif caddy_preexisting is False and caddy_created:
                    info = _require_owner_private_directory(
                        caddy_state,
                        owner_uid=self._owner_uid,
                    )
                    if (
                        info.st_dev != ownership.get("caddy_state_device")
                        or info.st_ino != ownership.get("caddy_state_inode")
                    ):
                        raise LinuxSetupRecoveryRequired(
                            "owned Caddy state identity changed after the durable receipt"
                        )
                if caddy_preexisting is True:
                    info = _require_owner_private_directory(caddy_state, owner_uid=self._owner_uid)
                    if (
                        info.st_dev != ownership.get("caddy_state_device")
                        or info.st_ino != ownership.get("caddy_state_inode")
                    ):
                        raise LinuxSetupRecoveryRequired("pre-existing Caddy state identity changed before bootstrap")
                    if not root_path.exists():
                        raise LinuxSetupRecoveryRequired(
                            "pre-existing Caddy state has no exact managed public root and cannot be mutated implicitly"
                        )
                bootstrap_config = caddy_state / ".factlane-ca-bootstrap.Caddyfile"
                if bootstrap_config.exists() or bootstrap_config.is_symlink():
                    expected_bootstrap = _render_caddy_ca_bootstrap_config(
                        hostname=plan.hostname,
                        caddy_state_dir=plan.caddy_state_dir,
                    )
                    _require_owner_private_regular_file(
                        bootstrap_config,
                        owner_uid=self._owner_uid,
                    )
                    if bootstrap_config.read_text(encoding="utf-8") != expected_bootstrap:
                        raise LinuxSetupRecoveryRequired(
                            "Caddy bootstrap file is foreign or drifted"
                        )
                    if caddy_preexisting is not False or not caddy_create_intent:
                        raise LinuxSetupRecoveryRequired(
                            "Caddy bootstrap file is not owned by the current setup attempt"
                        )
                    bootstrap_config.unlink()
                bootstrap = bootstrap_linux_caddy_root(
                    caddy_path=self._caddy_path,
                    caddy_state_dir=plan.caddy_state_dir,
                    hostname=plan.hostname,
                    owner_uid=self._owner_uid,
                    process_factory=self._caddy_bootstrap_process_factory,
                    clock=self._clock,
                    sleep=self._sleep,
                    timeout=self._activation_timeout,
                    require_complete_existing_state=(caddy_preexisting is False),
                )
                _update_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                    root_fingerprint_sha256=bootstrap.root_fingerprint_sha256,
                    ownership_updates={
                        "caddy_state_create_intent": bool(
                            ownership.get("caddy_state_create_intent")
                            or bootstrap.caddy_state_created
                        ),
                        "caddy_state_created": bool(
                            ownership.get("caddy_state_created")
                            or bootstrap.caddy_state_created
                        ),
                        "caddy_state_device": bootstrap.caddy_state_device,
                        "caddy_state_inode": bootstrap.caddy_state_inode,
                    },
                )
                self._attempt_caddy_state_created = (
                    self._attempt_caddy_state_created
                    or bool(ownership.get("caddy_state_created"))
                    or bootstrap.caddy_state_created
                )
            except LinuxSetupRecoveryRequired as exc:
                return InstallerStepResult(
                    status=InstallerStepStatus.RECOVERY_REQUIRED,
                    operation=operation.kind,
                    detail=str(exc),
                )
            except (OSError, ValueError) as exc:
                return InstallerStepResult(
                    status=InstallerStepStatus.FAILED,
                    operation=operation.kind,
                    detail=f"Caddy CA bootstrap failed safely: {type(exc).__name__}",
                )
            return InstallerStepResult(
                status=InstallerStepStatus.APPLIED,
                operation=operation.kind,
            )
        if operation.kind == "ENSURE_ISOLATED_PROFILE_NETWORK":
            try:
                if self._journal_path is None:
                    raise LinuxSetupRecoveryRequired(
                        "provisional setup journal is required before Firefox profile mutation"
                    )
                before = _parse_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                )
                before_ownership = before["ownership"]
                assert isinstance(before_ownership, dict)
                profile = Path(operation.target)
                profile_preexisting = before_ownership.get("profile_preexisting")
                profile_created_receipt = before_ownership.get("profile_created") is True
                profile_create_intent = before_ownership.get("profile_create_intent") is True
                if profile_preexisting is False and not profile_created_receipt:
                    if profile.exists():
                        if not profile_create_intent:
                            raise LinuxSetupRecoveryRequired(
                                "Firefox profile appeared before a durable create intent"
                            )
                        info = _require_owner_private_directory(
                            profile,
                            owner_uid=self._owner_uid,
                        )
                        try:
                            next(profile.iterdir())
                        except StopIteration:
                            pass
                        else:
                            raise LinuxSetupRecoveryRequired(
                                "Firefox profile appeared after create intent with unreceipted contents"
                            )
                        _update_linux_setup_journal(
                            self._journal_path,
                            owner_uid=self._owner_uid,
                            ownership_updates={
                                "profile_created": True,
                                "profile_device": int(info.st_dev),
                                "profile_inode": int(info.st_ino),
                            },
                        )
                        before_ownership["profile_created"] = True
                        before_ownership["profile_device"] = int(info.st_dev)
                        before_ownership["profile_inode"] = int(info.st_ino)
                        profile_created_receipt = True
                    else:
                        if not profile_create_intent:
                            _update_linux_setup_journal(
                                self._journal_path,
                                owner_uid=self._owner_uid,
                                ownership_updates={"profile_create_intent": True},
                            )
                            before_ownership["profile_create_intent"] = True
                            profile_create_intent = True
                        _ensure_real_directory_chain(profile.parent)
                        profile.mkdir(mode=0o700)
                        os.chmod(profile, 0o700)
                        info = _require_owner_private_directory(
                            profile,
                            owner_uid=self._owner_uid,
                        )
                        _update_linux_setup_journal(
                            self._journal_path,
                            owner_uid=self._owner_uid,
                            ownership_updates={
                                "profile_created": True,
                                "profile_device": int(info.st_dev),
                                "profile_inode": int(info.st_ino),
                            },
                        )
                        before_ownership["profile_created"] = True
                        before_ownership["profile_device"] = int(info.st_dev)
                        before_ownership["profile_inode"] = int(info.st_ino)
                        profile_created_receipt = True
                if profile_preexisting is True or profile_created_receipt:
                    info = _require_owner_private_directory(profile, owner_uid=self._owner_uid)
                    expected_dev = before_ownership.get("profile_device")
                    expected_ino = before_ownership.get("profile_inode")
                    if expected_dev is not None and (
                        info.st_dev != expected_dev or info.st_ino != expected_ino
                    ):
                        raise LinuxSetupRecoveryRequired("Firefox profile identity changed outside setup")
                user_js = profile / LINUX_FIREFOX_MANAGED_USER_JS
                user_js_preexisting = before_ownership.get("user_js_preexisting") is True
                user_js_create_intent = before_ownership.get("user_js_create_intent") is True
                created_before = before_ownership.get("profile_created_files")
                if not isinstance(created_before, list):
                    raise LinuxSetupRecoveryRequired("Firefox profile ownership receipt is invalid")
                expected_user_js = render_linux_firefox_network_policy(plan.hostname)
                if user_js_preexisting:
                    try:
                        _require_owner_private_regular_file(
                            user_js,
                            owner_uid=self._owner_uid,
                        )
                    except FileNotFoundError as exc:
                        raise LinuxSetupRecoveryRequired(
                            "pre-existing Firefox user.js disappeared before managed setup"
                        ) from exc
                    if user_js.read_text(encoding="utf-8") != expected_user_js:
                        raise LinuxSetupRecoveryRequired(
                            "pre-existing Firefox user.js conflicts with the exact managed network policy"
                        )
                elif user_js.exists() and LINUX_FIREFOX_MANAGED_USER_JS not in created_before:
                    if not user_js_create_intent:
                        raise LinuxSetupRecoveryRequired(
                            "Firefox user.js appeared before a durable create intent"
                        )
                    _require_owner_private_regular_file(
                        user_js,
                        owner_uid=self._owner_uid,
                    )
                    if user_js.read_text(encoding="utf-8") != expected_user_js:
                        raise LinuxSetupRecoveryRequired(
                            "Firefox user.js created after intent has foreign bytes"
                        )
                    created_before.append(LINUX_FIREFOX_MANAGED_USER_JS)
                    _update_linux_setup_journal(
                        self._journal_path,
                        owner_uid=self._owner_uid,
                        ownership_updates={"profile_created_files": created_before},
                    )
                elif not user_js.exists() and LINUX_FIREFOX_MANAGED_USER_JS not in created_before:
                    if not user_js_create_intent:
                        _update_linux_setup_journal(
                            self._journal_path,
                            owner_uid=self._owner_uid,
                            ownership_updates={"user_js_create_intent": True},
                        )
                        before_ownership["user_js_create_intent"] = True
                        user_js_create_intent = True
                network = ensure_linux_firefox_profile_network_policy(
                    profile_dir=profile,
                    hostname=plan.hostname,
                    owner_uid=self._owner_uid,
                )
                journal = _parse_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                )
                ownership = journal["ownership"]
                assert isinstance(ownership, dict)
                created_files = list(ownership.get("profile_created_files", []))
                if network.user_js_created and LINUX_FIREFOX_MANAGED_USER_JS not in created_files:
                    created_files.append(LINUX_FIREFOX_MANAGED_USER_JS)
                profile_info = _require_owner_private_directory(profile, owner_uid=self._owner_uid)
                _update_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                    ownership_updates={
                        "profile_created": bool(
                            ownership.get("profile_created") or network.profile_created
                        ),
                        "profile_create_intent": bool(
                            ownership.get("profile_create_intent")
                            or network.profile_created
                        ),
                        "user_js_create_intent": bool(
                            ownership.get("user_js_create_intent")
                            or network.user_js_created
                        ),
                        "profile_device": int(profile_info.st_dev),
                        "profile_inode": int(profile_info.st_ino),
                        "profile_created_files": created_files,
                    },
                )
                self._attempt_profile_created = (
                    self._attempt_profile_created or network.profile_created
                )
                if network.user_js_created:
                    self._attempt_profile_created_files.add(LINUX_FIREFOX_MANAGED_USER_JS)
            except (OSError, ValueError, RuntimeError) as exc:
                return InstallerStepResult(
                    status=InstallerStepStatus.RECOVERY_REQUIRED,
                    operation=operation.kind,
                    detail=f"isolated Firefox profile network state is unsafe: {exc}",
                )
            return InstallerStepResult(
                status=InstallerStepStatus.APPLIED,
                operation=operation.kind,
            )
        if operation.kind in {
            "WRITE_SERVICE_CONFIG",
            "WRITE_CADDY_CONFIG",
            "INSTALL_USER_UNIT",
            "INSTALL_OWNER_LAUNCHER",
        }:
            try:
                if self._journal_path is not None:
                    should_write = self._prepare_managed_file_write(
                        Path(operation.target),
                        desired_state=operation.desired_state,
                        allow_same_attempt_endpoint_rebind=(
                            operation.kind in {"WRITE_SERVICE_CONFIG", "WRITE_CADDY_CONFIG"}
                            and operation.target in self._attempt_endpoint_file_targets
                        ),
                    )
                    if not should_write:
                        return InstallerStepResult(
                            status=InstallerStepStatus.APPLIED,
                            operation=operation.kind,
                        )
                _atomic_owner_file_write(
                    Path(operation.target),
                    operation.desired_state,
                    private_parent=operation.kind not in {"INSTALL_USER_UNIT", "INSTALL_OWNER_LAUNCHER"},
                )
                if self._journal_path is not None:
                    self._mark_managed_file_written(
                        Path(operation.target),
                        desired_state=operation.desired_state,
                    )
                    if operation.kind in {"WRITE_SERVICE_CONFIG", "WRITE_CADDY_CONFIG"}:
                        self._attempt_endpoint_file_targets.add(operation.target)
            except LinuxSetupRecoveryRequired as exc:
                return InstallerStepResult(
                    status=InstallerStepStatus.RECOVERY_REQUIRED,
                    operation=operation.kind,
                    detail=str(exc),
                )
            except (OSError, ValueError) as exc:
                return InstallerStepResult(
                    status=InstallerStepStatus.FAILED,
                    operation=operation.kind,
                    detail=f"owner file write failed safely: {type(exc).__name__}",
                )
            return InstallerStepResult(
                status=InstallerStepStatus.APPLIED,
                operation=operation.kind,
            )

        if operation.kind == "ENABLE_USER_LINGERING":
            return self._ensure_user_lingering(operation)
        if operation.kind == "ENSURE_ISOLATED_PROFILE_TRUST":
            if self._journal_path is None:
                return InstallerStepResult(
                    status=InstallerStepStatus.RECOVERY_REQUIRED,
                    operation=operation.kind,
                    detail="provisional setup journal is required before Firefox trust mutation",
                )
            if self._certutil_path is None:
                return InstallerStepResult(
                    status=InstallerStepStatus.FAILED,
                    operation=operation.kind,
                    detail="certutil is required for isolated Firefox NSS trust",
                )
            if not isinstance(plan.browser, BrowserInstall) or plan.browser.family != "firefox":
                return InstallerStepResult(
                    status=InstallerStepStatus.FAILED,
                    operation=operation.kind,
                    detail="managed Linux browser must be Firefox",
                )
            root_path = Path(plan.caddy_state_dir) / "pki" / "authorities" / "local" / "root.crt"
            try:
                before = _parse_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                )
                before_ownership = before["ownership"]
                assert isinstance(before_ownership, dict)
                nss_preexisting = before_ownership.get("nss_preexisting") is True
                trust_preexisting = before_ownership.get("trust_preexisting") is True
                nss_intent = before_ownership.get("nss_create_intent") is True
                trust_intent = before_ownership.get("trust_import_intent") is True
                manifest_intent = before_ownership.get("trust_manifest_intent") is True
                profile = Path(operation.target)
                db_files = tuple(
                    profile / filename
                    for filename in ("cert9.db", "key4.db", "pkcs11.txt")
                )
                present = [path.exists() for path in db_files]
                if nss_preexisting:
                    if not all(present):
                        raise LinuxSetupRecoveryRequired(
                            "pre-existing Firefox NSS state disappeared or became partial"
                        )
                    for path in db_files:
                        _require_owner_private_regular_file(
                            path,
                            owner_uid=self._owner_uid,
                        )
                elif any(present) and not all(present):
                    if not nss_intent:
                        raise LinuxSetupRecoveryRequired(
                            "Firefox NSS state appeared before a durable create intent"
                        )
                    for path, exists in zip(db_files, present, strict=True):
                        if not exists:
                            continue
                        _require_owner_private_regular_file(
                            path,
                            owner_uid=self._owner_uid,
                        )
                        path.unlink()
                    present = [False, False, False]
                elif all(present) and not nss_preexisting:
                    if not nss_intent:
                        raise LinuxSetupRecoveryRequired(
                            "Firefox NSS state appeared before a durable create intent"
                        )
                    for path in db_files:
                        _require_owner_private_regular_file(
                            path,
                            owner_uid=self._owner_uid,
                        )
                    list_code, list_stdout = self._read_command_runner(
                        (self._certutil_path, "-L", "-d", f"sql:{profile}")
                    )
                    if list_code != 0:
                        raise LinuxSetupRecoveryRequired(
                            "interrupted Firefox NSS state cannot be inspected safely"
                        )
                    entries = _certutil_entries(list_stdout)
                    if any(
                        nickname != LINUX_FIREFOX_CA_NICKNAME
                        for nickname, _trust in entries
                    ):
                        raise LinuxSetupRecoveryRequired(
                            "interrupted Firefox NSS state contains foreign certificate entries"
                        )

                root_fingerprint = _root_certificate_fingerprint(
                    root_path,
                    owner_uid=self._owner_uid,
                )
                manifest_path = profile / LINUX_FIREFOX_TRUST_MANIFEST
                if manifest_path.exists() or manifest_path.is_symlink():
                    if trust_preexisting:
                        _require_owner_private_regular_file(
                            manifest_path,
                            owner_uid=self._owner_uid,
                        )
                    else:
                        if not manifest_intent:
                            raise LinuxSetupRecoveryRequired(
                                "Firefox trust manifest appeared before a durable create intent"
                            )
                        _require_owner_private_regular_file(
                            manifest_path,
                            owner_uid=self._owner_uid,
                        )
                    expected_manifest = _linux_firefox_manifest_text(
                        hostname=plan.hostname,
                        profile=profile,
                        firefox_executable=plan.browser.executable,
                        root_fingerprint_sha256=root_fingerprint,
                    )
                    if manifest_path.read_text(encoding="utf-8") != expected_manifest:
                        raise LinuxSetupRecoveryRequired(
                            "Firefox trust manifest has foreign or drifted bytes"
                        )

                def persist_trust_intent(kind: str) -> None:
                    field = {
                        "nss": "nss_create_intent",
                        "trust": "trust_import_intent",
                        "manifest": "trust_manifest_intent",
                    }.get(kind)
                    if field is None:
                        raise LinuxSetupRecoveryRequired(
                            "unknown Firefox setup mutation intent"
                        )
                    _update_linux_setup_journal(
                        self._journal_path,
                        owner_uid=self._owner_uid,
                        ownership_updates={field: True},
                    )

                trust = ensure_linux_firefox_profile_trust(
                    certutil_path=self._certutil_path,
                    firefox_executable=plan.browser.executable,
                    profile_dir=profile,
                    root_cert_path=root_path,
                    hostname=plan.hostname,
                    owner_uid=self._owner_uid,
                    command_runner=self._command_runner,
                    read_command_runner=self._read_command_runner,
                    mutation_intent_hook=persist_trust_intent,
                    recover_owned_missing_manifest=(
                        not trust_preexisting and trust_intent
                    ),
                )
                journal = _parse_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                )
                ownership = journal["ownership"]
                assert isinstance(ownership, dict)
                created_files = list(ownership.get("profile_created_files", []))
                owns_nss = bool(
                    not nss_preexisting
                    and ownership.get("nss_create_intent") is True
                )
                if owns_nss:
                    for filename in ("cert9.db", "key4.db", "pkcs11.txt"):
                        if filename not in created_files:
                            created_files.append(filename)
                owns_trust = bool(
                    not trust_preexisting
                    and ownership.get("trust_import_intent") is True
                )
                owns_manifest = bool(
                    not trust_preexisting
                    and ownership.get("trust_manifest_intent") is True
                )
                if owns_manifest and LINUX_FIREFOX_TRUST_MANIFEST not in created_files:
                    created_files.append(LINUX_FIREFOX_TRUST_MANIFEST)
                _update_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                    phase="TRUST_READY",
                    root_fingerprint_sha256=trust.root_fingerprint_sha256,
                    ownership_updates={
                        "trust_imported": bool(
                            ownership.get("trust_imported") or owns_trust
                        ),
                        "trust_import_intent": bool(
                            ownership.get("trust_import_intent") or owns_trust
                        ),
                        "trust_manifest_created": bool(
                            ownership.get("trust_manifest_created") or owns_manifest
                        ),
                        "trust_manifest_intent": bool(
                            ownership.get("trust_manifest_intent") or owns_manifest
                        ),
                        "profile_created_files": created_files,
                    },
                )
                self._attempt_trust_imported = (
                    self._attempt_trust_imported or owns_trust
                )
                if owns_nss:
                    self._attempt_profile_created_files.update(
                        {"cert9.db", "key4.db", "pkcs11.txt"}
                    )
                if owns_manifest:
                    self._attempt_profile_created_files.add(LINUX_FIREFOX_TRUST_MANIFEST)
            except LinuxSetupRecoveryRequired as exc:
                return InstallerStepResult(
                    status=InstallerStepStatus.RECOVERY_REQUIRED,
                    operation=operation.kind,
                    detail=str(exc),
                )
            except (OSError, ValueError) as exc:
                return InstallerStepResult(
                    status=InstallerStepStatus.FAILED,
                    operation=operation.kind,
                    detail=f"isolated Firefox trust failed safely: {type(exc).__name__}",
                )
            return InstallerStepResult(
                status=InstallerStepStatus.APPLIED,
                operation=operation.kind,
            )
        if operation.kind == "REGISTER_USER_SERVICE":
            if operation.target != WEBUI_SERVICE_NAME:
                return InstallerStepResult(
                    status=InstallerStepStatus.FAILED,
                    operation=operation.kind,
                    detail="user service target is not the exact FactLane service",
                )
            was_enabled = False
            ownership: dict[str, object] | None = None
            if self._journal_path is not None:
                try:
                    code, stdout = self._read_command_runner(
                        (self._systemctl_path, "--user", "is-enabled", WEBUI_SERVICE_NAME)
                    )
                except Exception:
                    return InstallerStepResult(
                        status=InstallerStepStatus.FAILED,
                        operation=operation.kind,
                        detail="user-service enablement state could not be inspected",
                    )
                state = stdout.strip()
                if code == 0 and state in {"enabled", "enabled-runtime"}:
                    was_enabled = True
                elif state not in {"disabled", "disabled-runtime", "not-found", ""}:
                    return InstallerStepResult(
                        status=InstallerStepStatus.RECOVERY_REQUIRED,
                        operation=operation.kind,
                        detail="user-service enablement state is ambiguous",
                    )
                try:
                    journal = _parse_linux_setup_journal(
                        self._journal_path,
                        owner_uid=self._owner_uid,
                    )
                    ownership = journal["ownership"]
                    assert isinstance(ownership, dict)
                    preexisting = ownership.get("service_enabled_preexisting")
                    intent = ownership.get("service_enable_intent") is True
                    changed = ownership.get("service_enable_changed") is True
                    if preexisting is None:
                        _update_linux_setup_journal(
                            self._journal_path,
                            owner_uid=self._owner_uid,
                            ownership_updates={"service_enabled_preexisting": was_enabled},
                        )
                        ownership["service_enabled_preexisting"] = was_enabled
                        preexisting = was_enabled
                    if preexisting is True and not was_enabled:
                        raise LinuxSetupRecoveryRequired(
                            "user service became disabled outside setup after the durable pre-mutation snapshot"
                        )
                    if preexisting is False and was_enabled and not (intent or changed):
                        raise LinuxSetupRecoveryRequired(
                            "user service became enabled outside the reviewed setup mutation"
                        )
                    if preexisting is False and was_enabled and intent and not changed:
                        _update_linux_setup_journal(
                            self._journal_path,
                            owner_uid=self._owner_uid,
                            ownership_updates={"service_enable_changed": True},
                        )
                        self._attempt_service_enable_changed = True
                except (OSError, ValueError, LinuxSetupRecoveryRequired) as exc:
                    return InstallerStepResult(
                        status=InstallerStepStatus.RECOVERY_REQUIRED,
                        operation=operation.kind,
                        detail=str(exc),
                    )
            commands = [(self._systemctl_path, "--user", "daemon-reload")]
            if not was_enabled:
                if self._journal_path is not None:
                    try:
                        _update_linux_setup_journal(
                            self._journal_path,
                            owner_uid=self._owner_uid,
                            ownership_updates={"service_enable_intent": True},
                        )
                    except (OSError, ValueError, LinuxSetupRecoveryRequired) as exc:
                        return InstallerStepResult(
                            status=InstallerStepStatus.RECOVERY_REQUIRED,
                            operation=operation.kind,
                            detail=f"service-enable intent could not be persisted before mutation: {exc}",
                        )
                    # From this point onward rollback must consult durable state even if
                    # systemctl reports failure after partially changing enablement.
                    self._attempt_service_enable_changed = True
                commands.append(
                    (
                        self._systemctl_path,
                        "--user",
                        "enable",
                        WEBUI_SERVICE_NAME,
                    )
                )
            for argv in commands:
                command_code = self._command_runner(argv)
                if command_code != 0:
                    if (
                        self._journal_path is not None
                        and len(argv) >= 2
                        and argv[-2:] == ("enable", WEBUI_SERVICE_NAME)
                    ):
                        try:
                            partial_code, partial_stdout = self._read_command_runner(
                                (self._systemctl_path, "--user", "is-enabled", WEBUI_SERVICE_NAME)
                            )
                        except Exception:
                            partial_code, partial_stdout = 1, ""
                        partial_state = partial_stdout.strip()
                        if partial_code == 0 and partial_state in {"enabled", "enabled-runtime"}:
                            _update_linux_setup_journal(
                                self._journal_path,
                                owner_uid=self._owner_uid,
                                ownership_updates={"service_enable_changed": True},
                            )
                            return InstallerStepResult(
                                status=InstallerStepStatus.RECOVERY_REQUIRED,
                                operation=operation.kind,
                                detail=(
                                    "systemctl enable returned nonzero after the service became enabled; "
                                    "durable rollback ownership is required"
                                ),
                            )
                        if partial_state not in {"disabled", "disabled-runtime", "not-found", ""}:
                            return InstallerStepResult(
                                status=InstallerStepStatus.RECOVERY_REQUIRED,
                                operation=operation.kind,
                                detail="systemctl enable failure left ambiguous user-service state",
                            )
                    return InstallerStepResult(
                        status=InstallerStepStatus.FAILED,
                        operation=operation.kind,
                        detail="exact user-service registration command failed",
                    )
            if self._journal_path is not None and not was_enabled:
                try:
                    verify_code, verify_stdout = self._read_command_runner(
                        (self._systemctl_path, "--user", "is-enabled", WEBUI_SERVICE_NAME)
                    )
                except Exception:
                    verify_code, verify_stdout = 1, ""
                if verify_code != 0 or verify_stdout.strip() not in {"enabled", "enabled-runtime"}:
                    return InstallerStepResult(
                        status=InstallerStepStatus.RECOVERY_REQUIRED,
                        operation=operation.kind,
                        detail="user-service enablement could not be positively verified",
                    )
                self._attempt_service_enable_changed = True
                _update_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                    ownership_updates={
                        "service_enable_intent": True,
                        "service_enable_changed": True,
                    },
                )
            return InstallerStepResult(
                status=InstallerStepStatus.APPLIED,
                operation=operation.kind,
            )
        return InstallerStepResult(
            status=InstallerStepStatus.FAILED,
            operation=operation.kind,
            detail="unsupported installer operation",
        )

    def read_saved_endpoint(self) -> str | None:
        if self._service_state_reader is None:
            return None
        return self._service_state_reader().saved_endpoint

    def _ensure_user_lingering(self, operation: PlannedOperation) -> InstallerStepResult:
        if self._loginctl_path is None:
            return InstallerStepResult(
                status=InstallerStepStatus.FAILED,
                operation=operation.kind,
                detail="loginctl executable is unavailable for the reviewed lingering change",
            )
        probe = (
            self._loginctl_path,
            "show-user",
            "--property=Linger",
            "--value",
            "--",
            operation.target,
        )
        try:
            returncode, stdout = self._read_command_runner(probe)
        except Exception:
            return InstallerStepResult(
                status=InstallerStepStatus.FAILED,
                operation=operation.kind,
                detail="loginctl could not inspect user lingering safely",
            )
        if returncode != 0 or stdout.strip().casefold() not in {"yes", "no"}:
            return InstallerStepResult(
                status=InstallerStepStatus.FAILED,
                operation=operation.kind,
                detail="loginctl returned an untrusted lingering state",
            )
        observed_yes = stdout.strip().casefold() == "yes"
        ownership: dict[str, object] | None = None
        if self._journal_path is not None:
            try:
                journal = _parse_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                )
                ownership = journal["ownership"]
                assert isinstance(ownership, dict)
                preexisting = ownership.get("linger_preexisting")
                intent = ownership.get("linger_change_intent") is True
                changed = ownership.get("linger_changed") is True
                if preexisting is None:
                    _update_linux_setup_journal(
                        self._journal_path,
                        owner_uid=self._owner_uid,
                        ownership_updates={"linger_preexisting": observed_yes},
                    )
                    ownership["linger_preexisting"] = observed_yes
                    preexisting = observed_yes
                if preexisting is True and not observed_yes:
                    raise LinuxSetupRecoveryRequired(
                        "user lingering changed outside setup after the durable pre-mutation snapshot"
                    )
                if preexisting is False and observed_yes and not (intent or changed):
                    raise LinuxSetupRecoveryRequired(
                        "user lingering became enabled outside the reviewed setup mutation"
                    )
                if preexisting is False and observed_yes and intent and not changed:
                    _update_linux_setup_journal(
                        self._journal_path,
                        owner_uid=self._owner_uid,
                        ownership_updates={"linger_changed": True},
                    )
                    self._attempt_linger_changed = True
                    changed = True
            except (OSError, ValueError, LinuxSetupRecoveryRequired) as exc:
                return InstallerStepResult(
                    status=InstallerStepStatus.RECOVERY_REQUIRED,
                    operation=operation.kind,
                    detail=str(exc),
                )
        if observed_yes:
            return InstallerStepResult(
                status=InstallerStepStatus.APPLIED,
                operation=operation.kind,
            )
        if self._journal_path is not None:
            try:
                _update_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                    ownership_updates={"linger_change_intent": True},
                )
            except (OSError, ValueError, LinuxSetupRecoveryRequired) as exc:
                return InstallerStepResult(
                    status=InstallerStepStatus.RECOVERY_REQUIRED,
                    operation=operation.kind,
                    detail=f"lingering intent could not be persisted before mutation: {exc}",
                )
            self._attempt_linger_changed = True
        argv = (
            self._loginctl_path,
            "enable-linger",
            "--",
            operation.target,
        )
        if self._command_runner(argv) != 0:
            return InstallerStepResult(
                status=(
                    InstallerStepStatus.RECOVERY_REQUIRED
                    if self._journal_path is not None
                    else InstallerStepStatus.FAILED
                ),
                operation=operation.kind,
                detail="loginctl/logind did not complete the exact user lingering change; rollback verification is required",
            )
        try:
            verified_code, verified_stdout = self._read_command_runner(probe)
        except Exception:
            verified_code, verified_stdout = 1, ""
        if verified_code != 0 or verified_stdout.strip().casefold() != "yes":
            return InstallerStepResult(
                status=InstallerStepStatus.RECOVERY_REQUIRED,
                operation=operation.kind,
                detail="user lingering was not positively verified after loginctl returned",
            )
        if self._journal_path is not None:
            _update_linux_setup_journal(
                self._journal_path,
                owner_uid=self._owner_uid,
                ownership_updates={
                    "linger_change_intent": True,
                    "linger_changed": True,
                },
            )
            self._attempt_linger_changed = True
        return InstallerStepResult(
            status=InstallerStepStatus.APPLIED,
            operation=operation.kind,
        )

    def _prepare_managed_file_write(
        self,
        path: Path,
        *,
        desired_state: str,
        allow_same_attempt_endpoint_rebind: bool = False,
    ) -> bool:
        if self._journal_path is None:
            return True
        journal = _parse_linux_setup_journal(self._journal_path, owner_uid=self._owner_uid)
        ownership = journal["ownership"]
        assert isinstance(ownership, dict)
        files = ownership.get("files")
        if not isinstance(files, dict):
            raise LinuxSetupRecoveryRequired("setup journal managed-file ownership is invalid")
        order = ownership.get("file_order")
        if not isinstance(order, list):
            raise LinuxSetupRecoveryRequired("setup journal managed-file order is invalid")
        key = str(path)
        desired_sha = hashlib.sha256(desired_state.encode("utf-8")).hexdigest()
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != self._owner_uid:
                raise LinuxSetupRecoveryRequired("managed file target has foreign identity")
            current_bytes = path.read_bytes()
            current_sha = hashlib.sha256(current_bytes).hexdigest()
        else:
            current_bytes = None
            current_sha = None

        record = files.get(key)
        if record is None:
            if info is not None and current_sha == desired_sha and stat.S_IMODE(info.st_mode) == 0o600:
                return False
            files[key] = {
                "before_exists": info is not None,
                "before_text": None if current_bytes is None else current_bytes.decode("utf-8"),
                "before_mode": None if info is None else stat.S_IMODE(info.st_mode),
                "before_sha256": current_sha,
                "intended_sha256": desired_sha,
                "managed_sha256": None,
            }
            if key not in order:
                order.append(key)
            _update_linux_setup_journal(
                self._journal_path,
                owner_uid=self._owner_uid,
                ownership_updates={"files": files, "file_order": order},
            )
            self._attempt_file_targets.add(key)
            return True
        if not isinstance(record, dict):
            raise LinuxSetupRecoveryRequired("setup journal managed-file record is invalid")
        before_sha = record.get("before_sha256")
        intended_sha = record.get("intended_sha256")
        managed_sha = record.get("managed_sha256")
        if intended_sha != desired_sha:
            if not allow_same_attempt_endpoint_rebind:
                raise LinuxSetupRecoveryRequired("managed file desired bytes changed across setup retry")
            if (
                info is None
                or stat.S_IMODE(info.st_mode) != 0o600
                or not isinstance(intended_sha, str)
                or managed_sha != intended_sha
                or current_sha != managed_sha
            ):
                raise LinuxSetupRecoveryRequired(
                    "managed endpoint candidate bytes are not the exact prior same-attempt write"
                )
            record["intended_sha256"] = desired_sha
            _update_linux_setup_journal(
                self._journal_path,
                owner_uid=self._owner_uid,
                ownership_updates={"files": files},
            )
            intended_sha = desired_sha
        before_exists = record.get("before_exists") is True
        if info is not None and current_sha == desired_sha and stat.S_IMODE(info.st_mode) == 0o600:
            if managed_sha is None:
                record["managed_sha256"] = desired_sha
                _update_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                    ownership_updates={"files": files},
                )
            self._attempt_file_targets.add(key)
            return False
        if info is None:
            if before_exists or managed_sha is not None:
                raise LinuxSetupRecoveryRequired("managed file disappeared during setup")
        else:
            allowed = {
                value
                for value in (before_sha, intended_sha, managed_sha)
                if isinstance(value, str)
            }
            if current_sha not in allowed:
                raise LinuxSetupRecoveryRequired("managed file changed outside the setup attempt")
        self._attempt_file_targets.add(key)
        return True

    def _mark_managed_file_written(self, path: Path, *, desired_state: str) -> None:
        if self._journal_path is None:
            return
        journal = _parse_linux_setup_journal(self._journal_path, owner_uid=self._owner_uid)
        ownership = journal["ownership"]
        assert isinstance(ownership, dict)
        files = ownership.get("files")
        if not isinstance(files, dict) or not isinstance(files.get(str(path)), dict):
            raise LinuxSetupRecoveryRequired("managed file write has no durable rollback ownership record")
        record = files[str(path)]
        assert isinstance(record, dict)
        expected_sha = hashlib.sha256(desired_state.encode("utf-8")).hexdigest()
        if _sha256_regular_owner_file(path, owner_uid=self._owner_uid) != expected_sha:
            raise LinuxSetupRecoveryRequired("managed file write did not preserve exact desired bytes")
        record["managed_sha256"] = expected_sha
        _update_linux_setup_journal(
            self._journal_path,
            owner_uid=self._owner_uid,
            ownership_updates={"files": files},
        )
        self._attempt_file_targets.add(str(path))

    def _prepare_dynamic_managed_file(
        self,
        path: Path,
    ) -> tuple[_ServiceGovernanceToken, bool]:
        if self._journal_path is None:
            return
        journal = _parse_linux_setup_journal(self._journal_path, owner_uid=self._owner_uid)
        ownership = journal["ownership"]
        assert isinstance(ownership, dict)
        files = ownership.get("files")
        order = ownership.get("file_order")
        if not isinstance(files, dict) or not isinstance(order, list):
            raise LinuxSetupRecoveryRequired("setup journal dynamic-file ownership is invalid")
        key = str(path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != self._owner_uid:
                raise LinuxSetupRecoveryRequired("dynamic managed file has foreign identity")
            current_bytes = path.read_bytes()
            current_sha = hashlib.sha256(current_bytes).hexdigest()
        else:
            current_bytes = None
            current_sha = None
        record = files.get(key)
        if record is None:
            files[key] = {
                "before_exists": info is not None,
                "before_text": None if current_bytes is None else current_bytes.decode("utf-8"),
                "before_mode": None if info is None else stat.S_IMODE(info.st_mode),
                "before_sha256": current_sha,
                "managed_sha256": None,
            }
            if key not in order:
                order.append(key)
            _update_linux_setup_journal(
                self._journal_path,
                owner_uid=self._owner_uid,
                ownership_updates={"files": files, "file_order": order},
            )
        else:
            if not isinstance(record, dict):
                raise LinuxSetupRecoveryRequired("dynamic managed-file record is invalid")
            before_exists = record.get("before_exists") is True
            before_sha = record.get("before_sha256")
            managed_sha = record.get("managed_sha256")
            if info is None:
                if before_exists and managed_sha is not None:
                    raise LinuxSetupRecoveryRequired("dynamic managed file disappeared outside setup")
            else:
                allowed = {value for value in (before_sha, managed_sha) if isinstance(value, str)}
                if current_sha not in allowed:
                    raise LinuxSetupRecoveryRequired("dynamic managed file changed outside setup")
        self._attempt_file_targets.add(key)
        return _service_governance_token(current_bytes), current_bytes is not None

    def _mark_dynamic_managed_file(
        self,
        path: Path,
        *,
        expected_governance: _ServiceGovernanceToken | None = None,
    ) -> None:
        if self._journal_path is None:
            return
        journal = _parse_linux_setup_journal(self._journal_path, owner_uid=self._owner_uid)
        ownership = journal["ownership"]
        assert isinstance(ownership, dict)
        files = ownership.get("files")
        if not isinstance(files, dict):
            raise LinuxSetupRecoveryRequired("setup journal dynamic-file ownership is invalid")
        record = files.get(str(path))
        if not isinstance(record, dict):
            raise LinuxSetupRecoveryRequired("dynamic managed file has no durable rollback record")
        try:
            _require_owner_private_regular_file(path, owner_uid=self._owner_uid)
            current_bytes = path.read_bytes()
        except FileNotFoundError:
            current_bytes = None
        current_governance = _service_governance_token(current_bytes)
        if (
            expected_governance is not None
            and current_governance != expected_governance
        ):
            raise LinuxSetupRecoveryRequired(
                "governed service state changed outside the setup attempt"
            )
        if current_bytes is None:
            managed_sha = None
        else:
            managed_sha = hashlib.sha256(current_bytes).hexdigest()
        record["managed_sha256"] = managed_sha
        _update_linux_setup_journal(
            self._journal_path,
            owner_uid=self._owner_uid,
            ownership_updates={"files": files},
        )

    def finalize_setup(self, *, plan: SetupPlan) -> InstallerStepResult:
        if self._journal_path is None:
            return InstallerStepResult(
                status=InstallerStepStatus.RECOVERY_REQUIRED,
                operation="FINALIZE_SETUP",
                detail="provisional setup journal is unavailable at claim time",
            )
        try:
            _update_linux_setup_journal(
                self._journal_path,
                owner_uid=self._owner_uid,
                phase="CLAIMED",
            )
        except (OSError, ValueError, LinuxSetupRecoveryRequired) as exc:
            return InstallerStepResult(
                status=InstallerStepStatus.RECOVERY_REQUIRED,
                operation="FINALIZE_SETUP",
                detail=f"claimed setup journal could not be persisted: {type(exc).__name__}",
            )
        return InstallerStepResult(
            status=InstallerStepStatus.APPLIED,
            operation="FINALIZE_SETUP",
        )

    def mark_recovery_required(
        self,
        *,
        plan: SetupPlan,
        detail: str,
    ) -> InstallerStepResult:
        del plan, detail
        if self._journal_path is None:
            return InstallerStepResult(
                status=InstallerStepStatus.RECOVERY_REQUIRED,
                operation="MARK_RECOVERY_REQUIRED",
                detail="provisional setup journal is unavailable for durable recovery",
            )
        try:
            _update_linux_setup_journal(
                self._journal_path,
                owner_uid=self._owner_uid,
                phase="RECOVERY_REQUIRED",
            )
        except (OSError, ValueError, LinuxSetupRecoveryRequired) as exc:
            return InstallerStepResult(
                status=InstallerStepStatus.RECOVERY_REQUIRED,
                operation="MARK_RECOVERY_REQUIRED",
                detail=f"recovery journal could not be persisted: {type(exc).__name__}",
            )
        return InstallerStepResult(
            status=InstallerStepStatus.APPLIED,
            operation="MARK_RECOVERY_REQUIRED",
        )

    def rollback_setup(
        self,
        *,
        plan: SetupPlan,
        completed_operations: tuple[str, ...],
    ) -> InstallerStepResult:
        del completed_operations
        if self._journal_path is None:
            return InstallerStepResult(
                status=InstallerStepStatus.APPLIED,
                operation="ROLLBACK_SETUP",
            )
        try:
            journal = _parse_linux_setup_journal(self._journal_path, owner_uid=self._owner_uid)
            ownership = journal["ownership"]
            assert isinstance(ownership, dict)
            profile = Path(str(journal["browser_profile_dir"]))
            installing_user = str(journal["installing_user"])

            service_preexisting = ownership.get("service_enabled_preexisting")
            service_intent = ownership.get("service_enable_intent") is True
            service_changed = ownership.get("service_enable_changed") is True
            if service_preexisting not in {None, True, False}:
                raise LinuxSetupRecoveryRequired("service enablement ownership state is invalid")
            if service_preexisting is not None:
                try:
                    service_code, service_stdout = self._read_command_runner(
                        (self._systemctl_path, "--user", "is-enabled", WEBUI_SERVICE_NAME)
                    )
                except Exception as exc:
                    raise LinuxSetupRecoveryRequired(
                        "rollback could not inspect current user-service enablement"
                    ) from exc
                service_state = service_stdout.strip()
                service_enabled = service_code == 0 and service_state in {
                    "enabled",
                    "enabled-runtime",
                }
                service_disabled = service_state in {
                    "disabled",
                    "disabled-runtime",
                    "not-found",
                    "",
                }
                if not service_enabled and not service_disabled:
                    raise LinuxSetupRecoveryRequired(
                        "rollback observed ambiguous user-service enablement state"
                    )
                if service_preexisting is True:
                    if not service_enabled:
                        raise LinuxSetupRecoveryRequired(
                            "pre-existing user-service enablement changed outside setup"
                        )
                elif service_intent or service_changed:
                    if service_enabled:
                        if self._command_runner(
                            (self._systemctl_path, "--user", "disable", WEBUI_SERVICE_NAME)
                        ) != 0:
                            raise LinuxSetupRecoveryRequired(
                                "rollback could not disable the service enablement it owns"
                            )
                        verify_code, verify_stdout = self._read_command_runner(
                            (self._systemctl_path, "--user", "is-enabled", WEBUI_SERVICE_NAME)
                        )
                        verify_state = verify_stdout.strip()
                        if verify_code == 0 or verify_state not in {
                            "disabled",
                            "disabled-runtime",
                            "not-found",
                            "",
                        }:
                            raise LinuxSetupRecoveryRequired(
                                "rollback could not verify restored user-service disablement"
                            )
                elif service_enabled:
                    raise LinuxSetupRecoveryRequired(
                        "user service became enabled without durable setup intent"
                    )

            profile_exists = (
                False
                if journal.get("browser_family") is None
                else profile.exists() or profile.is_symlink()
            )
            profile_preexisting = ownership.get("profile_preexisting")
            profile_create_intent = ownership.get("profile_create_intent") is True
            profile_created = ownership.get("profile_created") is True
            user_js_preexisting = ownership.get("user_js_preexisting")
            user_js_intent = ownership.get("user_js_create_intent") is True
            nss_preexisting = ownership.get("nss_preexisting")
            nss_intent = ownership.get("nss_create_intent") is True
            trust_preexisting = ownership.get("trust_preexisting")
            trust_intent = ownership.get("trust_import_intent") is True
            trust_imported = ownership.get("trust_imported") is True
            manifest_intent = ownership.get("trust_manifest_intent") is True
            manifest_created = ownership.get("trust_manifest_created") is True
            for state_value, label in (
                (profile_preexisting, "profile"),
                (user_js_preexisting, "user.js"),
                (nss_preexisting, "NSS"),
                (trust_preexisting, "trust"),
            ):
                if state_value not in {None, True, False}:
                    raise LinuxSetupRecoveryRequired(
                        f"{label} ownership state is invalid"
                    )

            if profile_preexisting is True and not profile_exists:
                raise LinuxSetupRecoveryRequired(
                    "pre-existing Firefox profile disappeared outside setup"
                )
            if profile_exists:
                profile_info = _require_owner_private_directory(
                    profile,
                    owner_uid=self._owner_uid,
                )
                expected_profile_dev = ownership.get("profile_device")
                expected_profile_ino = ownership.get("profile_inode")
                if expected_profile_dev is not None and (
                    profile_info.st_dev != expected_profile_dev
                    or profile_info.st_ino != expected_profile_ino
                ):
                    raise LinuxSetupRecoveryRequired(
                        "Firefox profile identity changed outside setup"
                    )

                root_fingerprint = journal.get("root_fingerprint_sha256")
                nss_files = tuple(
                    profile / filename
                    for filename in ("cert9.db", "key4.db", "pkcs11.txt")
                )
                nss_present = [path.exists() for path in nss_files]
                if (
                    trust_preexisting is False
                    and (trust_intent or trust_imported)
                    and all(nss_present)
                ):
                    if self._certutil_path is None:
                        raise LinuxSetupRecoveryRequired(
                            "rollback cannot inspect imported Firefox trust"
                        )
                    list_code, list_stdout = self._read_command_runner(
                        (self._certutil_path, "-L", "-d", f"sql:{profile}")
                    )
                    if list_code != 0:
                        raise LinuxSetupRecoveryRequired(
                            "rollback cannot inspect Firefox NSS certificate state"
                        )
                    entry = next(
                        (
                            item
                            for item in _certutil_entries(list_stdout)
                            if item[0] == LINUX_FIREFOX_CA_NICKNAME
                        ),
                        None,
                    )
                    if entry is not None:
                        if entry[1] != "C,," or not isinstance(root_fingerprint, str):
                            raise LinuxSetupRecoveryRequired(
                                "rollback found ambiguous FactLane Firefox trust state"
                            )
                        export_code, export_stdout = self._read_command_runner(
                            (
                                self._certutil_path,
                                "-L",
                                "-d",
                                f"sql:{profile}",
                                "-n",
                                LINUX_FIREFOX_CA_NICKNAME,
                                "-a",
                            )
                        )
                        if (
                            export_code != 0
                            or _certificate_fingerprint_from_pem_text(export_stdout)
                            != root_fingerprint
                        ):
                            raise LinuxSetupRecoveryRequired(
                                "rollback found a foreign FactLane Firefox root"
                            )
                        if self._command_runner(
                            (
                                self._certutil_path,
                                "-D",
                                "-d",
                                f"sql:{profile}",
                                "-n",
                                LINUX_FIREFOX_CA_NICKNAME,
                            )
                        ) != 0:
                            raise LinuxSetupRecoveryRequired(
                                "rollback could not remove the imported Firefox trust"
                            )
                        verify_code, verify_stdout = self._read_command_runner(
                            (self._certutil_path, "-L", "-d", f"sql:{profile}")
                        )
                        if verify_code != 0 or any(
                            nickname == LINUX_FIREFOX_CA_NICKNAME
                            for nickname, _trust in _certutil_entries(verify_stdout)
                        ):
                            raise LinuxSetupRecoveryRequired(
                                "rollback could not verify imported Firefox trust removal"
                            )
                elif trust_preexisting is False and trust_imported and not all(nss_present):
                    raise LinuxSetupRecoveryRequired(
                        "rollback cannot prove imported Firefox trust ownership from partial NSS state"
                    )

                manifest_path = profile / LINUX_FIREFOX_TRUST_MANIFEST
                if trust_preexisting is False and (manifest_intent or manifest_created):
                    if manifest_path.exists() or manifest_path.is_symlink():
                        _require_owner_private_regular_file(
                            manifest_path,
                            owner_uid=self._owner_uid,
                        )
                        if not isinstance(root_fingerprint, str):
                            raise LinuxSetupRecoveryRequired(
                                "rollback lacks the exact Firefox root fingerprint"
                            )
                        expected_manifest = _linux_firefox_manifest_text(
                            hostname=str(journal["hostname"]),
                            profile=profile,
                            firefox_executable=str(journal["browser_executable"]),
                            root_fingerprint_sha256=root_fingerprint,
                        )
                        if manifest_path.read_text(encoding="utf-8") != expected_manifest:
                            raise LinuxSetupRecoveryRequired(
                                "rollback will not remove foreign Firefox trust-manifest bytes"
                            )
                        manifest_path.unlink()

                if nss_preexisting is False and nss_intent:
                    nss_present = [path.exists() for path in nss_files]
                    if all(nss_present):
                        if self._certutil_path is None:
                            raise LinuxSetupRecoveryRequired(
                                "rollback cannot inspect owned Firefox NSS state"
                            )
                        list_code, list_stdout = self._read_command_runner(
                            (self._certutil_path, "-L", "-d", f"sql:{profile}")
                        )
                        if list_code != 0:
                            raise LinuxSetupRecoveryRequired(
                                "rollback cannot inspect owned Firefox NSS state"
                            )
                        if _certutil_entries(list_stdout):
                            raise LinuxSetupRecoveryRequired(
                                "rollback found unrelated certificate entries in setup-created NSS state"
                            )
                    for path, exists in zip(nss_files, nss_present, strict=True):
                        if not exists:
                            continue
                        _require_owner_private_regular_file(
                            path,
                            owner_uid=self._owner_uid,
                        )
                        path.unlink()

                user_js = profile / LINUX_FIREFOX_MANAGED_USER_JS
                created_files = ownership.get("profile_created_files")
                if not isinstance(created_files, list):
                    raise LinuxSetupRecoveryRequired(
                        "Firefox profile-created file ownership is invalid"
                    )
                if user_js_preexisting is False and (
                    user_js_intent
                    or LINUX_FIREFOX_MANAGED_USER_JS in created_files
                ):
                    if user_js.exists() or user_js.is_symlink():
                        _require_owner_private_regular_file(
                            user_js,
                            owner_uid=self._owner_uid,
                        )
                        if user_js.read_text(encoding="utf-8") != render_linux_firefox_network_policy(
                            str(journal["hostname"])
                        ):
                            raise LinuxSetupRecoveryRequired(
                                "rollback will not remove foreign Firefox user.js bytes"
                            )
                        user_js.unlink()

                if profile_preexisting is False and (
                    profile_create_intent or profile_created
                ):
                    try:
                        profile.rmdir()
                    except OSError as exc:
                        raise LinuxSetupRecoveryRequired(
                            "rollback found unrelated state in the setup-created Firefox profile"
                        ) from exc

            files = ownership.get("files")
            order = ownership.get("file_order")
            if not isinstance(files, dict) or not isinstance(order, list):
                raise LinuxSetupRecoveryRequired("setup journal rollback file ownership is invalid")
            service_state_path = self._journal_path.parent / "webui-service.json"
            user_unit_targets = {
                Path(operation.target)
                for operation in plan.operations
                if operation.kind == "INSTALL_USER_UNIT"
            }
            restored_user_unit = False
            for key in reversed(order):
                record = files.get(key)
                if not isinstance(record, dict):
                    raise LinuxSetupRecoveryRequired("setup journal rollback file record is invalid")
                managed_path = Path(key)
                if managed_path == service_state_path:
                    with service_state_file_guard(managed_path):
                        _restore_managed_file_from_record(
                            managed_path,
                            record,
                            owner_uid=self._owner_uid,
                        )
                else:
                    _restore_managed_file_from_record(
                        managed_path,
                        record,
                        owner_uid=self._owner_uid,
                    )
                if managed_path in user_unit_targets:
                    restored_user_unit = True

            if restored_user_unit and self._command_runner(
                (self._systemctl_path, "--user", "daemon-reload")
            ) != 0:
                raise LinuxSetupRecoveryRequired(
                    "rollback restored the user unit bytes but could not reload the user manager"
                )

            linger_preexisting = ownership.get("linger_preexisting")
            linger_intent = ownership.get("linger_change_intent") is True
            linger_changed = ownership.get("linger_changed") is True
            if linger_preexisting not in {None, True, False}:
                raise LinuxSetupRecoveryRequired("lingering ownership state is invalid")
            if linger_preexisting is not None:
                if self._loginctl_path is None:
                    raise LinuxSetupRecoveryRequired("rollback cannot inspect user lingering")
                code, stdout = self._read_command_runner(
                    (
                        self._loginctl_path,
                        "show-user",
                        "--property=Linger",
                        "--value",
                        "--",
                        installing_user,
                    )
                )
                state = stdout.strip().casefold()
                if code != 0 or state not in {"yes", "no"}:
                    raise LinuxSetupRecoveryRequired(
                        "rollback observed ambiguous user lingering state"
                    )
                if linger_preexisting is True:
                    if state != "yes":
                        raise LinuxSetupRecoveryRequired(
                            "pre-existing user lingering changed outside setup"
                        )
                elif linger_intent or linger_changed:
                    if state == "yes":
                        if self._command_runner(
                            (self._loginctl_path, "disable-linger", "--", installing_user)
                        ) != 0:
                            raise LinuxSetupRecoveryRequired(
                                "rollback could not restore user lingering"
                            )
                        verify_code, verify_stdout = self._read_command_runner(
                            (
                                self._loginctl_path,
                                "show-user",
                                "--property=Linger",
                                "--value",
                                "--",
                                installing_user,
                            )
                        )
                        if verify_code != 0 or verify_stdout.strip().casefold() != "no":
                            raise LinuxSetupRecoveryRequired(
                                "rollback could not verify restored user lingering"
                            )
                elif state == "yes":
                    raise LinuxSetupRecoveryRequired(
                        "user lingering became enabled without durable setup intent"
                    )

            caddy_preexisting = ownership.get("caddy_state_preexisting")
            caddy_intent = ownership.get("caddy_state_create_intent") is True
            caddy_created = ownership.get("caddy_state_created") is True
            if caddy_preexisting not in {None, True, False}:
                raise LinuxSetupRecoveryRequired("Caddy ownership state is invalid")
            if caddy_preexisting is False and (caddy_intent or caddy_created):
                caddy_state = Path(plan.caddy_state_dir)
                if caddy_state.exists() or caddy_state.is_symlink():
                    info = _require_owner_private_directory(
                        caddy_state,
                        owner_uid=self._owner_uid,
                    )
                    expected_dev = ownership.get("caddy_state_device")
                    expected_ino = ownership.get("caddy_state_inode")
                    if caddy_created and (
                        info.st_dev != expected_dev or info.st_ino != expected_ino
                    ):
                        raise LinuxSetupRecoveryRequired(
                            "rollback Caddy state identity changed outside setup"
                        )
                    if not caddy_created:
                        try:
                            next(caddy_state.iterdir())
                        except StopIteration:
                            caddy_state.rmdir()
                        else:
                            raise LinuxSetupRecoveryRequired(
                                "rollback found unreceipted Caddy contents after create intent"
                            )
                    else:
                        _remove_owned_caddy_state_tree(
                            caddy_state,
                            owner_uid=self._owner_uid,
                            hostname=str(journal["hostname"]),
                        )

            _update_linux_setup_journal(
                self._journal_path,
                owner_uid=self._owner_uid,
                phase="PROVISIONAL",
                ownership_replace=_new_linux_setup_ownership(),
            )
        except (OSError, ValueError, LinuxSetupRecoveryRequired) as exc:
            try:
                _update_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                    phase="RECOVERY_REQUIRED",
                )
            except Exception:
                pass
            return InstallerStepResult(
                status=InstallerStepStatus.RECOVERY_REQUIRED,
                operation="ROLLBACK_SETUP",
                detail=str(exc),
            )
        return InstallerStepResult(
            status=InstallerStepStatus.APPLIED,
            operation="ROLLBACK_SETUP",
        )

    def activate_candidate(self, *, plan: SetupPlan) -> ManagedServiceActivationResult:
        self._activation_state_path = None
        self._activation_service_governance = None
        self._activation_state_custody_ready = False
        state_path: Path | None = None
        if self._journal_path is not None:
            state_path = self._journal_path.parent / "webui-service.json"
            self._activation_state_path = state_path
            try:
                _update_linux_setup_journal(
                    self._journal_path,
                    owner_uid=self._owner_uid,
                    phase="ACTIVATING",
                )
                (
                    self._activation_service_governance,
                    state_preexisting,
                ) = self._prepare_dynamic_managed_file(state_path)
                if state_preexisting and self._service_state_reader is not None:
                    reconciled = self._service_state_reader()
                    self._activation_service_governance = _reconciled_service_governance_baseline(
                        self._activation_service_governance,
                        _snapshot_governance_token(reconciled),
                    )
                    if (
                        reconciled.status
                        in {
                            ServiceStatus.STOPPED_UNTIL_REBOOT,
                            ServiceStatus.MAINTENANCE,
                            ServiceStatus.RECOVERY_REQUIRED,
                        }
                        or reconciled.maintenance_operation_id is not None
                    ):
                        self._activation_state_custody_ready = True
                        return ManagedServiceActivationResult(
                            status=ManagedServiceActivationStatus.FAILED,
                            saved_endpoint=reconciled.saved_endpoint,
                            detail=(
                                "governed service state blocks candidate activation"
                            ),
                        )
                self._activation_state_custody_ready = True
            except (OSError, ValueError, LinuxSetupRecoveryRequired) as exc:
                return ManagedServiceActivationResult(
                    status=ManagedServiceActivationStatus.FAILED,
                    detail=f"service-state rollback ownership could not be established: {exc}",
                )

        def finish(result: ManagedServiceActivationResult) -> ManagedServiceActivationResult:
            if state_path is None:
                return result
            try:
                if not self._activation_state_custody_ready:
                    raise LinuxSetupRecoveryRequired(
                        "service-state rollback custody is not stable"
                    )
                with service_state_file_guard(state_path):
                    self._mark_dynamic_managed_file(
                        state_path,
                        expected_governance=self._activation_service_governance,
                    )
            except (OSError, ValueError, LinuxSetupRecoveryRequired) as exc:
                self._activation_state_custody_ready = False
                return ManagedServiceActivationResult(
                    status=ManagedServiceActivationStatus.FAILED,
                    detail=f"service-state rollback ownership became ambiguous: {exc}",
                )
            return result

        restart_argv = (
            self._systemctl_path,
            "--user",
            "restart",
            WEBUI_SERVICE_NAME,
        )
        if state_path is not None:
            try:
                with service_state_file_guard(state_path):
                    current_governance, _ = self._read_service_state_governance(state_path)
                    if current_governance != self._activation_service_governance:
                        self._activation_state_custody_ready = False
                        return ManagedServiceActivationResult(
                            status=ManagedServiceActivationStatus.FAILED,
                            detail="governed service state changed before candidate start",
                        )
                    start_code = self._command_runner(restart_argv)
            except (OSError, ValueError, LinuxSetupRecoveryRequired) as exc:
                self._activation_state_custody_ready = False
                return ManagedServiceActivationResult(
                    status=ManagedServiceActivationStatus.FAILED,
                    detail=f"candidate start governance could not be proven: {exc}",
                )
        else:
            start_code = self._command_runner(restart_argv)
        if start_code != 0:
            probed = self._probe_activation(plan.origin)
            if probed is not None and probed.status is ManagedServiceActivationStatus.PORT_CONFLICT:
                return finish(probed)
            return finish(ManagedServiceActivationResult(
                status=ManagedServiceActivationStatus.FAILED,
                detail="managed service restart command failed",
            ))
        if self._service_state_reader is None:
            return finish(ManagedServiceActivationResult(
                status=ManagedServiceActivationStatus.FAILED,
                detail="owner-private service-state reader is required for activation proof",
            ))

        deadline = self._clock() + self._activation_timeout
        while True:
            probed = self._probe_activation(plan.origin)
            if probed is not None and probed.status in {
                ManagedServiceActivationStatus.PORT_CONFLICT,
                ManagedServiceActivationStatus.FAILED,
            }:
                return finish(probed)

            snapshot = self._service_state_reader()
            if snapshot.saved_endpoint is not None and snapshot.saved_endpoint != plan.origin:
                migration_pending = (
                    snapshot.status in {ServiceStatus.STARTING, ServiceStatus.READY}
                    and is_legacy_endpoint_canonicalization(
                        snapshot.saved_endpoint,
                        plan.origin,
                        expected_legacy_hostname=plan.hostname,
                    )
                )
                if not migration_pending:
                    return finish(ManagedServiceActivationResult(
                        status=ManagedServiceActivationStatus.FAILED,
                        saved_endpoint=snapshot.saved_endpoint,
                        detail="service state saved endpoint differs from the candidate origin",
                    ))
            if snapshot.status is ServiceStatus.READY and snapshot.saved_endpoint == plan.origin:
                if (
                    probed is not None
                    and probed.status is not ManagedServiceActivationStatus.READY
                ):
                    now = self._clock()
                    if now >= deadline:
                        return finish(ManagedServiceActivationResult(
                            status=ManagedServiceActivationStatus.FAILED,
                            saved_endpoint=snapshot.saved_endpoint,
                            detail="managed service endpoint claimed but activation probe never confirmed readiness",
                        ))
                    self._sleep(min(self._poll_interval, deadline - now))
                    continue
                return finish(ManagedServiceActivationResult(
                    status=ManagedServiceActivationStatus.READY,
                    saved_endpoint=snapshot.saved_endpoint,
                ))
            if snapshot.status is ServiceStatus.FAILED:
                return finish(ManagedServiceActivationResult(
                    status=ManagedServiceActivationStatus.FAILED,
                    saved_endpoint=snapshot.saved_endpoint,
                    detail="managed service entered FAILED before candidate readiness",
                ))
            now = self._clock()
            if now >= deadline:
                return finish(ManagedServiceActivationResult(
                    status=ManagedServiceActivationStatus.FAILED,
                    saved_endpoint=snapshot.saved_endpoint,
                    detail="managed service activation readiness timed out",
                ))
            self._sleep(min(self._poll_interval, deadline - now))

    def stop_candidate(self, *, plan: SetupPlan) -> InstallerStepResult:
        del plan
        argv = (
            self._systemctl_path,
            "--user",
            "stop",
            WEBUI_SERVICE_NAME,
        )
        if self._journal_path is not None:
            state_path = self._journal_path.parent / "webui-service.json"
            if str(state_path) in self._attempt_file_targets:
                if (
                    self._activation_state_path == state_path
                    and not self._activation_state_custody_ready
                ):
                    return InstallerStepResult(
                        status=InstallerStepStatus.RECOVERY_REQUIRED,
                        operation="STOP_MANAGED_SERVICE",
                        detail=(
                            "managed service stopped, but governed service-state "
                            "custody changed outside setup; rollback was not authorized"
                        ),
                    )
                try:
                    with service_state_file_guard(state_path):
                        expected_governance = (
                            self._activation_service_governance
                            if self._activation_state_path == state_path
                            else None
                        )
                        current_governance, _ = self._read_service_state_governance(state_path)
                        if (
                            expected_governance is not None
                            and current_governance != expected_governance
                        ):
                            raise LinuxSetupRecoveryRequired(
                                "governed service state changed outside the setup attempt"
                            )
                        if self._command_runner(argv) != 0:
                            return InstallerStepResult(
                                status=InstallerStepStatus.FAILED,
                                operation="STOP_MANAGED_SERVICE",
                                detail="managed service stop command failed",
                            )
                        self._mark_dynamic_managed_file(
                            state_path,
                            expected_governance=expected_governance,
                        )
                except (OSError, ValueError, LinuxSetupRecoveryRequired) as exc:
                    if self._activation_state_path == state_path:
                        self._activation_state_custody_ready = False
                    return InstallerStepResult(
                        status=InstallerStepStatus.RECOVERY_REQUIRED,
                        operation="STOP_MANAGED_SERVICE",
                        detail=f"stopped service state could not be bound for rollback: {exc}",
                    )
                return InstallerStepResult(
                    status=InstallerStepStatus.APPLIED,
                    operation="STOP_MANAGED_SERVICE",
                )
        if self._command_runner(argv) != 0:
            return InstallerStepResult(
                status=InstallerStepStatus.FAILED,
                operation="STOP_MANAGED_SERVICE",
                detail="managed service stop command failed",
            )
        return InstallerStepResult(
            status=InstallerStepStatus.APPLIED,
            operation="STOP_MANAGED_SERVICE",
        )

    def _read_service_state_governance(
        self,
        state_path: Path,
    ) -> tuple[_ServiceGovernanceToken, bool]:
        try:
            _require_owner_private_regular_file(state_path, owner_uid=self._owner_uid)
            current_bytes = state_path.read_bytes()
        except FileNotFoundError:
            current_bytes = None
        return _service_governance_token(current_bytes), current_bytes is not None

    def _probe_activation(self, origin: str) -> ManagedServiceActivationResult | None:
        if self._activation_probe is None:
            return None
        return self._activation_probe(origin)


def _parse_windows_owner_panel_launcher(value: str) -> dict[str, str]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise WindowsWebUIError(
            "WINDOWS_LAUNCHER_SPEC_INVALID",
            "Owner Panel shortcut specification is invalid",
        ) from exc
    expected = {"arguments", "description", "target", "working_directory"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise WindowsWebUIError(
            "WINDOWS_LAUNCHER_SPEC_INVALID",
            "Owner Panel shortcut specification is invalid",
        )
    for field in expected:
        item = payload.get(field)
        if not isinstance(item, str) or not item or any(ord(char) < 32 for char in item):
            raise WindowsWebUIError(
                "WINDOWS_LAUNCHER_SPEC_INVALID",
                "Owner Panel shortcut specification is invalid",
            )
    if payload["arguments"] != "-m factlane.webui_launcher":
        raise WindowsWebUIError(
            "WINDOWS_LAUNCHER_SPEC_INVALID",
            "Owner Panel shortcut arguments changed",
        )
    _validate_windows_absolute_path(payload["target"], "Owner Panel shortcut runtime")
    _validate_windows_absolute_path(
        payload["working_directory"],
        "Owner Panel shortcut working directory",
    )
    if PureWindowsPath(payload["target"]).name.casefold() != "python.exe":
        raise WindowsWebUIError(
            "WINDOWS_LAUNCHER_SPEC_INVALID",
            "Owner Panel shortcut must use the console Python so the connection URL remains visible",
        )
    return {field: str(payload[field]) for field in expected}


def _write_windows_owner_panel_shortcut(
    destination: str,
    target: str,
    arguments: str,
    working_directory: str,
) -> None:
    if os.name != "nt":
        raise WindowsWebUIError(
            "WINDOWS_PLATFORM_REQUIRED",
            "Windows shortcut creation requires native Windows",
        )
    try:
        import ctypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        def guid(value: str) -> GUID:
            raw = uuid.UUID(value).bytes_le
            result = GUID()
            ctypes.memmove(ctypes.byref(result), raw, 16)
            return result

        def check(hr: int, operation: str) -> None:
            if hr < 0:
                raise OSError(f"{operation} failed with HRESULT 0x{hr & 0xffffffff:08x}")

        ole32 = ctypes.OleDLL("ole32")
        ole32.CoInitialize.argtypes = [ctypes.c_void_p]
        ole32.CoInitialize.restype = ctypes.c_long
        ole32.CoUninitialize.argtypes = []
        ole32.CoUninitialize.restype = None
        check(ole32.CoInitialize(None), "CoInitialize")
        clsid = guid("00021401-0000-0000-C000-000000000046")
        iid_shell_link = guid("000214F9-0000-0000-C000-000000000046")
        iid_persist_file = guid("0000010b-0000-0000-C000-000000000046")
        shell_link = ctypes.c_void_p()
        ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
        ]
        ole32.CoCreateInstance.restype = ctypes.c_long
        try:
            check(
                ole32.CoCreateInstance(
                    ctypes.byref(clsid), None, 1, ctypes.byref(iid_shell_link), ctypes.byref(shell_link)
                ),
                "CoCreateInstance(IShellLinkW)",
            )
            winfunctype = ctypes.WINFUNCTYPE

            def method(pointer: ctypes.c_void_p, index: int, restype, *argtypes):
                vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                return winfunctype(restype, ctypes.c_void_p, *argtypes)(vtable[index])

            release = method(shell_link, 2, ctypes.c_ulong)
            query_interface = method(
                shell_link, 0, ctypes.c_long, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
            )
            set_description = method(shell_link, 7, ctypes.c_long, ctypes.c_wchar_p)
            set_working_directory = method(shell_link, 9, ctypes.c_long, ctypes.c_wchar_p)
            set_arguments = method(shell_link, 11, ctypes.c_long, ctypes.c_wchar_p)
            set_path = method(shell_link, 20, ctypes.c_long, ctypes.c_wchar_p)
            persist_file = ctypes.c_void_p()
            check(
                set_path(shell_link, target), "IShellLinkW.SetPath"
            )
            try:
                check(set_arguments(shell_link, arguments), "IShellLinkW.SetArguments")
                check(set_working_directory(shell_link, working_directory), "IShellLinkW.SetWorkingDirectory")
                check(set_description(shell_link, "Open the local FactLane Owner Panel"), "IShellLinkW.SetDescription")
                check(query_interface(shell_link, ctypes.byref(iid_persist_file), ctypes.byref(persist_file)), "IShellLinkW.QueryInterface(IPersistFile)")
                persist_release = method(persist_file, 2, ctypes.c_ulong)
                save = method(persist_file, 6, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int)
                try:
                    check(save(persist_file, destination, 1), "IPersistFile.Save")
                    load = method(persist_file, 5, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_uint32)
                    check(load(persist_file, destination, 0), "IPersistFile.Load")
                    for index, expected in ((6, "Open the local FactLane Owner Panel"),
                                            (8, working_directory), (10, arguments)):
                        buffer = ctypes.create_unicode_buffer(32768)
                        getter = method(shell_link, index, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int)
                        check(getter(shell_link, buffer, len(buffer)), "IShellLinkW.GetProperty")
                        if buffer.value != expected:
                            raise ValueError("staged shortcut property differs")
                    buffer = ctypes.create_unicode_buffer(32768)
                    get_path = method(shell_link, 3, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int,
                                      ctypes.c_void_p, ctypes.c_uint32)
                    check(get_path(shell_link, buffer, len(buffer), None, 4), "IShellLinkW.GetPath")
                    if buffer.value.casefold() != target.casefold():
                        raise ValueError("staged shortcut target differs")
                finally:
                    persist_release(persist_file)
            finally:
                release(shell_link)
        finally:
            ole32.CoUninitialize()
    except Exception as exc:
        raise WindowsWebUIError(
            "WINDOWS_LAUNCHER_WRITE_FAILED",
            "Owner Panel shortcut could not be created",
        ) from exc
    created = validate_windows_path_no_reparse(destination)
    if not created.is_file():
        raise WindowsWebUIError(
            "WINDOWS_LAUNCHER_WRITE_FAILED",
            "Owner Panel shortcut was not created",
        )


WINDOWS_SETUP_JOURNAL_LIMIT = 4 * 1024 * 1024


def _decode_windows_setup_journal(raw: bytes, *, installation_id: str, owner_sid: str,
                                  targets: dict[str, str]) -> dict:
    def reject(message="Windows setup journal is invalid or foreign"):
        raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", message)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                reject()
            result[key] = value
        return result
    if len(raw) > WINDOWS_SETUP_JOURNAL_LIMIT:
        reject()
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
        if not isinstance(payload, dict) or set(payload) != {
            "version", "transaction_id", "installation_id", "owner_sid", "phase", "operations"
        }:
            reject()
        if type(payload["version"]) is not int or payload["version"] != 1:
            reject()
        if str(uuid.UUID(payload["transaction_id"])) != payload["transaction_id"]:
            reject()
        if payload["installation_id"] != installation_id or payload["owner_sid"] != owner_sid:
            reject()
        if payload["phase"] not in {"PREPARED", "APPLYING", "ROLLING_BACK", "RECOVERY_REQUIRED", "ROLLED_BACK", "COMMITTED"}:
            reject()
        operations = payload["operations"]
        if not isinstance(operations, list) or not operations or len(operations) > 3:
            reject()
        seen = set()
        for record in operations:
            if not isinstance(record, dict) or set(record) != {
                "kind", "target", "status", "before", "intended", "after", "restore"
            }:
                reject()
            kind = record["kind"]
            if kind in seen or kind not in targets or record["target"] != targets[kind]:
                reject()
            seen.add(kind)
            if record["status"] not in {"PREPARED", "APPLIED", "RESTORED", "CONFLICT"}:
                reject()
            for field in ("before", "intended", "after", "restore"):
                value = record[field]
                if value is None and field in {"after", "restore"}:
                    continue
                _validate_windows_setup_snapshot(value, task=kind == "REGISTER_WINDOWS_TASK")
            if kind == "REGISTER_WINDOWS_TASK":
                try:
                    source = windows.WindowsTaskSchedulerSupervisor._registration_source_from_xml(
                        record["intended"]["xml"]
                    )
                except WindowsWebUIError:
                    reject()
                if source != "FactLane setup " + payload["transaction_id"]:
                    reject("Windows setup task recipe is not bound to its transaction")
            if record["status"] == "APPLIED" and record["after"] is None:
                reject()
        if seen != set(targets) or [r["kind"] for r in operations] != list(targets):
            reject()
        if payload["phase"] == "COMMITTED" and any(r["status"] != "APPLIED" for r in operations):
            reject()
        return payload
    except (ValueError, TypeError, KeyError, UnicodeError, AttributeError) as exc:
        raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "Windows setup journal is malformed") from exc


def _validate_windows_setup_snapshot(value, *, task: bool) -> None:
    invalid = WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "Windows setup custody record is invalid")
    if not isinstance(value, dict) or type(value.get("exists")) is not bool:
        raise invalid
    if not value["exists"]:
        if set(value) != {"exists"}:
            raise invalid
        return
    keys = {"exists", "xml", "sddl", "enabled"} if task else {"exists", "identity", "content", "sddl"}
    if set(value) != keys or not isinstance(value["sddl"], str) or not 0 < len(value["sddl"]) <= 32768:
        raise invalid
    if task:
        if type(value["enabled"]) is not bool or not isinstance(value["xml"], str) or not 0 < len(value["xml"].encode()) <= 262144:
            raise invalid
    else:
        identity = value["identity"]
        if not isinstance(identity, list) or len(identity) != 2 or any(type(n) is not int or n < 0 for n in identity):
            raise invalid
        try:
            if len(base64.b64decode(value["content"], validate=True)) > windows.WINDOWS_SETUP_FILE_LIMIT:
                raise invalid
        except (ValueError, TypeError) as exc:
            raise invalid from exc


def _windows_setup_config_from_snapshot(snapshot, *, installation_id):
    if not snapshot["exists"]:
        return None
    try:
        payload = json.loads(base64.b64decode(snapshot["content"], validate=True))
        if not isinstance(payload, dict) or set(payload) != set(WindowsManagedConfig.__dataclass_fields__):
            raise ValueError("unexpected managed config fields")
        config = WindowsManagedConfig(**payload)
        if config.installation_id != installation_id:
            raise ValueError("foreign installation config")
        return config
    except (ValueError, TypeError, WindowsWebUIError) as exc:
        raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "managed config is invalid or belongs to another installation") from exc


def _validate_windows_setup_config(snapshot, *, installation_id):
    _windows_setup_config_from_snapshot(snapshot, installation_id=installation_id)


def _authorized_windows_setup_config_evolution(previous, current, *, installation_id) -> bool:
    """Allow only the authenticated one-way memory-binding transition."""

    try:
        before = _windows_setup_config_from_snapshot(previous, installation_id=installation_id)
        after = _windows_setup_config_from_snapshot(current, installation_id=installation_id)
    except WindowsWebUIError:
        return False
    if before is None or after is None:
        return False
    immutable = (
        "installation_id",
        "origin",
        "native_control_pipe",
        "runtime_dir",
        "state_dir",
        "browser_profile_dir",
    )
    if any(getattr(before, name) != getattr(after, name) for name in immutable):
        return False
    if any(getattr(before, name) is not None for name in ("db", "profile", "host_id")):
        return False
    return all(getattr(after, name) is not None for name in ("db", "profile", "host_id"))


class ProductionWindowsInstallerBackend:
    """Owner-side native Windows setup backend; no system service or trust-store mutation."""

    def __init__(
        self,
        *,
        request: SetupRequest,
        supervisor: WindowsTaskSchedulerSupervisor | None = None,
        shortcut_writer: Callable[[str, str, str, str], None] | None = None,
        expected_precondition: WindowsSetupPrecondition | None = None,
    ) -> None:
        if os.name != "nt":
            raise ValueError("native Windows installer backend requires Windows")
        self.request = request
        self._expected_precondition = expected_precondition
        base_executable, environment_executable = windows_managed_python_executables(
            environment_executable=request.factlane_executable,
        )
        self._task_executable = base_executable
        self._task_arguments = windows_managed_python_arguments(environment_executable, "serve")
        self.supervisor = supervisor or WindowsTaskSchedulerSupervisor(
            expected_executable=base_executable,
            environment_executable=environment_executable,
        )
        self._shortcut_writer = shortcut_writer or _write_windows_owner_panel_shortcut
        root = PureWindowsPath(request.owner_config_home).parent
        if request.native_control_socket is None:
            raise ValueError("native Windows control endpoint is required")
        self._paths = WindowsOwnerPaths(
            root_dir=str(root),
            owner_config_home=request.owner_config_home,
            service_config_path=request.service_config_path,
            runtime_dir=request.runtime_dir,
            state_dir=request.state_dir,
            browser_profile_dir=request.browser_profile_dir,
            native_control_socket=request.native_control_socket,
            backend_endpoint=request.backend_socket,
            task_receipt_path=request.user_unit_path,
        )

    def capture_precondition(self) -> WindowsSetupPrecondition:
        """Capture every setup-owned Windows surface before native consent."""

        owner_sid = windows.current_user_sid()
        canonical = windows.windows_owner_paths(owner_sid=owner_sid)
        self._validate_launcher_target()
        if self._paths != canonical:
            raise WindowsWebUIError(
                "WINDOWS_SETUP_PATH_INVALID",
                "Windows setup requires fixed canonical owner paths",
            )
        service_state_path = str(PureWindowsPath(self.request.state_dir) / "webui-service.json")
        journal_path = str(PureWindowsPath(self.request.state_dir) / "windows-setup-journal.json")
        launcher = (
            windows.snapshot_windows_setup_file(
                self.request.launcher_path,
                owner_sid=owner_sid,
                owner_private=False,
            )
            if self.request.launcher_path is not None
            else {"exists": False}
        )
        return WindowsSetupPrecondition(
            owner_sid=owner_sid,
            service_config=windows.snapshot_windows_setup_file(
                self.request.service_config_path,
                owner_sid=owner_sid,
            ),
            scheduled_task=self.supervisor.snapshot_task(),
            launcher=launcher,
            service_state=windows.snapshot_windows_setup_file(
                service_state_path,
                owner_sid=owner_sid,
            ),
            setup_journal=windows.snapshot_windows_setup_file(
                journal_path,
                owner_sid=owner_sid,
                limit=WINDOWS_SETUP_JOURNAL_LIMIT,
            ),
        )

    def _require_precondition_unchanged(self) -> WindowsSetupPrecondition:
        expected = self._expected_precondition
        if expected is None:
            raise WindowsWebUIError(
                "WINDOWS_SETUP_PRECONDITION_REQUIRED",
                "Windows setup requires an exact pre-consent custody snapshot",
            )
        observed = self.capture_precondition()
        if observed != expected:
            raise WindowsWebUIError(
                "WINDOWS_SETUP_PRECONDITION_CHANGED",
                "Windows setup custody changed after native consent",
            )
        return observed

    def mutation_guard(self):
        return windows.windows_mutation_guard()

    @property
    def transaction_id(self) -> str | None:
        journal = getattr(self, "_journal", None)
        if not isinstance(journal, dict):
            return None
        value = journal.get("transaction_id")
        if not isinstance(value, str):
            return None
        try:
            return str(uuid.UUID(value))
        except ValueError:
            return None

    def _validate_launcher_target(self):
        if self.request.launcher_path is None:
            return
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise WindowsWebUIError("WINDOWS_SETUP_PATH_INVALID", "current-user Start Menu location is unavailable")
        expected = PureWindowsPath(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / WINDOWS_OWNER_PANEL_LAUNCHER_FILENAME
        if not expected.is_absolute() or ".." in expected.parts or PureWindowsPath(self.request.launcher_path) != expected:
            raise WindowsWebUIError("WINDOWS_SETUP_PATH_INVALID", "launcher must use the fixed current-user Start Menu target")

    def _targets(self):
        result = {"WRITE_SERVICE_CONFIG": self.request.service_config_path,
                  "REGISTER_WINDOWS_TASK": WINDOWS_TASK_NAME}
        if self.request.launcher_path is not None:
            result["INSTALL_OWNER_LAUNCHER"] = self.request.launcher_path
        return result

    def _snapshot_target(self, kind, target):
        if kind == "REGISTER_WINDOWS_TASK":
            return self.supervisor.snapshot_task()
        return windows.snapshot_windows_setup_file(
            target,
            owner_sid=self._owner_sid,
            owner_private=kind != "INSTALL_OWNER_LAUNCHER",
        )

    def _persist_journal(self):
        payload = json.dumps(self._journal, sort_keys=True, separators=(",", ":"))
        _decode_windows_setup_journal(payload.encode(), installation_id=self.request.installation_id,
                                     owner_sid=self._owner_sid, targets=self._targets())
        write_owner_private_text_atomic(self._journal_path, payload, owner_sid=self._owner_sid,
            expected_state=self._journal_custody, snapshot_limit=WINDOWS_SETUP_JOURNAL_LIMIT)
        self._journal_custody = windows.snapshot_windows_setup_file(self._journal_path, owner_sid=self._owner_sid,
                                                                   limit=WINDOWS_SETUP_JOURNAL_LIMIT)

    def _close_stages(self):
        stages, self._stages = getattr(self, "_stages", {}), {}
        for staged in stages.values():
            windows.close_windows_setup_stage(staged, owner_sid=self._owner_sid)

    def prepare_setup(self, *, plan):
        self._journal = None
        self._stages = {}
        precondition = self._require_precondition_unchanged()
        self._owner_sid = precondition.owner_sid
        canonical = windows.windows_owner_paths(owner_sid=self._owner_sid)
        self._validate_launcher_target()
        if self._paths != canonical:
            raise WindowsWebUIError("WINDOWS_SETUP_PATH_INVALID", "Windows setup requires fixed canonical owner paths")
        for raw in (self._paths.root_dir, self._paths.owner_config_home, self._paths.runtime_dir, self._paths.state_dir):
            path = validate_windows_path_no_reparse(raw)
            if path.exists():
                windows.validate_owner_private_path(path, owner_sid=self._owner_sid, directory=True)
            else:
                windows.ensure_owner_private_directory(path, owner_sid=self._owner_sid)
        self._journal_path = str(PureWindowsPath(self.request.state_dir) / "windows-setup-journal.json")
        prior = windows.snapshot_windows_setup_file(self._journal_path, owner_sid=self._owner_sid,
                                                   limit=WINDOWS_SETUP_JOURNAL_LIMIT)
        self._journal_custody = prior
        if prior["exists"]:
            previous = _decode_windows_setup_journal(base64.b64decode(prior["content"]),
                installation_id=self.request.installation_id, owner_sid=self._owner_sid, targets=self._targets())
            if previous["phase"] == "COMMITTED":
                for record in previous["operations"]:
                    current = self._snapshot_target(record["kind"], record["target"])
                    if current == record["after"]:
                        continue
                    if (
                        record["kind"] == "WRITE_SERVICE_CONFIG"
                        and _authorized_windows_setup_config_evolution(
                            record["after"],
                            current,
                            installation_id=self.request.installation_id,
                        )
                    ):
                        continue
                    raise WindowsWebUIError(
                        "WINDOWS_SETUP_RECOVERY_REQUIRED",
                        "committed setup targets changed outside authorized evolution",
                    )
            else:
                self._journal = previous
                recovered = self.rollback_setup(plan=plan, completed_operations=())
                if recovered.status is not InstallerStepStatus.APPLIED:
                    raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", recovered.detail or "interrupted setup unresolved")
            self._journal = None
        # Read current intent; setup never rewrites or replays service state.
        state_path = str(PureWindowsPath(self.request.state_dir) / "webui-service.json")
        state = windows.snapshot_windows_setup_file(state_path, owner_sid=self._owner_sid)
        if state["exists"]:
            intent = json.loads(base64.b64decode(state["content"]))
            if intent.get("status") in {ServiceStatus.STARTING.value, ServiceStatus.READY.value,
                                       ServiceStatus.MAINTENANCE.value, ServiceStatus.RECOVERY_REQUIRED.value}:
                raise WindowsWebUIError("WINDOWS_SETUP_BUSY", "managed lifecycle must be quiescent before setup")
        targets = self._targets()
        operations = [op for op in plan.operations if op.kind != "ENSURE_WINDOWS_OWNER_STATE"]
        if [(op.kind, op.target) for op in operations] != list(targets.items()):
            raise WindowsWebUIError("WINDOWS_SETUP_PATH_INVALID", "Windows setup operation order or targets changed")
        transaction_id = str(uuid.uuid4())
        records = []
        try:
            # Prove task ownership/quiescence before preparing any binding change.
            task_intended = self.supervisor.prepare_task(self._task_executable, transaction_id=transaction_id)
            for operation in operations:
                before = self._snapshot_target(operation.kind, operation.target)
                if operation.kind == "WRITE_SERVICE_CONFIG":
                    _validate_windows_setup_config(before, installation_id=self.request.installation_id)
                if operation.kind == "REGISTER_WINDOWS_TASK":
                    intended = task_intended
                else:
                    if operation.kind == "WRITE_SERVICE_CONFIG":
                        content = operation.desired_state.encode("utf-8")
                    else:
                        content = self._stage_launcher_bytes(operation)
                    if before["exists"] and base64.b64decode(before["content"]) == content:
                        intended = before
                    else:
                        staged = windows.stage_windows_setup_file(operation.target, content, owner_sid=self._owner_sid,
                            private_parent=operation.kind == "WRITE_SERVICE_CONFIG",
                            owner_private=operation.kind != "INSTALL_OWNER_LAUNCHER")
                        self._stages[operation.kind] = staged
                        intended = staged[2]
                records.append({"kind": operation.kind, "target": operation.target, "status": "PREPARED",
                                "before": before, "intended": intended, "after": None, "restore": None})
            self._journal = {"version": 1, "transaction_id": transaction_id,
                "installation_id": self.request.installation_id, "owner_sid": self._owner_sid,
                "phase": "PREPARED", "operations": records}
            self._persist_journal()
        except BaseException:
            self._close_stages()
            raise

    def _stage_launcher_bytes(self, operation):
        payload = _parse_windows_owner_panel_launcher(operation.desired_state)
        target = Path(payload["target"])
        if not target.is_file() or PureWindowsPath(payload["target"]).name.casefold() != "python.exe":
            raise WindowsWebUIError("WINDOWS_LAUNCHER_RUNTIME_INVALID", "FactLane console Python runtime is unavailable")
        if PureWindowsPath(operation.target).name != WINDOWS_OWNER_PANEL_LAUNCHER_FILENAME:
            raise WindowsWebUIError("WINDOWS_LAUNCHER_TARGET_INVALID", "Owner Panel shortcut filename changed")
        stage_path = str(PureWindowsPath(self.request.state_dir) / (".launcher-" + uuid.uuid4().hex + ".lnk"))
        stage_pre = None
        try:
            # Create the exact staging path under FactLane's owner-only write
            # contract before Shell COM ever sees it.  The parent DACL is
            # intentionally non-inheritable, so allowing IPersistFile.Save to
            # create the path would leave a window where the file is not yet
            # proven owner-private.
            windows.write_owner_private_text_atomic(
                stage_path,
                "",
                owner_sid=self._owner_sid,
            )
            stage_pre = windows.snapshot_windows_setup_file(
                stage_path,
                owner_sid=self._owner_sid,
            )
            self._shortcut_writer(stage_path, payload["target"], payload["arguments"], payload["working_directory"])
            staged = windows.snapshot_windows_setup_file(stage_path, owner_sid=self._owner_sid)
            if staged.get("identity") != stage_pre.get("identity"):
                raise WindowsWebUIError(
                    "WINDOWS_LAUNCHER_WRITE_FAILED",
                    "staged Owner Panel shortcut identity changed during Shell save",
                )
            if not staged["exists"]:
                raise WindowsWebUIError("WINDOWS_LAUNCHER_WRITE_FAILED", "staged launcher is missing")
            return base64.b64decode(staged["content"])
        finally:
            # Never delete a path whose identity escaped this transaction.  Shell
            # COM is expected to update the precreated owner-private file in
            # place; a replacement is rejected above and preserved for explicit
            # recovery rather than being mistaken for disposable staging output.
            partial = windows.snapshot_windows_setup_file(stage_path, owner_sid=self._owner_sid)
            if (
                partial["exists"]
                and stage_pre is not None
                and partial.get("identity") == stage_pre.get("identity")
            ):
                windows.delete_windows_setup_file(stage_path, expected=partial, owner_sid=self._owner_sid)

    def apply_operation(self, operation, *, plan):
        if not self._journal or self._journal["phase"] not in {"PREPARED", "APPLYING"}:
            raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "setup was not durably prepared")
        if operation.kind == "ENSURE_WINDOWS_OWNER_STATE":
            return InstallerStepResult(status=InstallerStepStatus.APPLIED, operation=operation.kind)
        record = next(r for r in self._journal["operations"] if r["kind"] == operation.kind)
        if operation.target != record["target"]:
            raise WindowsWebUIError("WINDOWS_SETUP_PATH_INVALID", "prepared target changed")
        self._journal["phase"] = "APPLYING"
        self._persist_journal()  # Includes the current PREPARED op before any publication.
        task_post = None
        if record["intended"] != record["before"]:
            if operation.kind == "REGISTER_WINDOWS_TASK":
                task_post = self.supervisor.publish_task(
                    record["intended"],
                    expected=record["before"],
                    transaction_id=self._journal["transaction_id"],
                )
            else:
                windows.publish_windows_setup_file(record["target"], self._stages[operation.kind],
                                                   expected=record["before"], owner_sid=self._owner_sid,
                                                   owner_private=operation.kind != "INSTALL_OWNER_LAUNCHER")
                windows.close_windows_setup_stage(self._stages.pop(operation.kind), owner_sid=self._owner_sid)
        post = self._snapshot_target(record["kind"], record["target"])
        if operation.kind == "REGISTER_WINDOWS_TASK":
            if task_post is None:
                task_post = record["before"]
            if post != task_post:
                raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "setup task postimage changed after registration")
        elif post != record["intended"]:
            raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "setup postimage could not be verified")
        record["after"] = post
        record["status"] = "APPLIED"
        self._persist_journal()
        return InstallerStepResult(status=InstallerStepStatus.APPLIED, operation=operation.kind)

    def commit_setup(self, *, plan):
        if any(r["status"] != "APPLIED" or self._snapshot_target(r["kind"], r["target"]) != r["after"]
               for r in self._journal["operations"]):
            raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "setup commit postconditions changed")
        self._journal["phase"] = "COMMITTED"
        self._persist_journal()
        self._close_stages()

    def _restore_record(self, record, expected):
        if record["kind"] == "REGISTER_WINDOWS_TASK":
            # Crash recovery rereads owner intent rather than replaying saved state.
            state_path = str(PureWindowsPath(self.request.state_dir) / "webui-service.json")
            state = windows.snapshot_windows_setup_file(state_path, owner_sid=self._owner_sid)
            if state["exists"]:
                intent = json.loads(base64.b64decode(state["content"]))
                if (intent.get("status") in {
                        ServiceStatus.MAINTENANCE.value,
                        ServiceStatus.RECOVERY_REQUIRED.value,
                    }
                        and record["before"].get("enabled") is True):
                    raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "current owner intent prevents task enable restoration")
            self.supervisor.restore_task(record["before"], expected=expected)
            return record["before"]
        original = record["before"]
        if not original["exists"]:
            windows.delete_windows_setup_file(
                record["target"],
                expected=expected,
                owner_sid=self._owner_sid,
                owner_private=record["kind"] != "INSTALL_OWNER_LAUNCHER",
            )
            return original
        staged = windows.stage_windows_setup_file(record["target"], base64.b64decode(original["content"]),
            owner_sid=self._owner_sid, private_parent=record["kind"] == "WRITE_SERVICE_CONFIG", sddl=original["sddl"],
            owner_private=record["kind"] != "INSTALL_OWNER_LAUNCHER")
        try:
            record["restore"] = staged[2]
            self._persist_journal()  # Recovery may itself be interrupted after publication.
            windows.publish_windows_setup_file(
                record["target"],
                staged,
                expected=expected,
                owner_sid=self._owner_sid,
                owner_private=record["kind"] != "INSTALL_OWNER_LAUNCHER",
            )
            return staged[2]
        finally:
            windows.close_windows_setup_stage(staged, owner_sid=self._owner_sid)

    def _owned_task_intermediate(self, record, current):
        if record["kind"] != "REGISTER_WINDOWS_TASK" or not current.get("exists"):
            return False
        matcher = getattr(self.supervisor, "matches_prepared_task", None)
        return bool(
            matcher is not None
            and matcher(
                record["intended"],
                current,
                transaction_id=self._journal["transaction_id"],
            )
        )

    def _assert_recovery_quiescent(self):
        state_path = str(PureWindowsPath(self.request.state_dir) / "webui-service.json")
        state = windows.snapshot_windows_setup_file(state_path, owner_sid=self._owner_sid)
        if state["exists"]:
            intent = json.loads(base64.b64decode(state["content"]))
            if intent.get("status") in {ServiceStatus.STARTING.value, ServiceStatus.READY.value}:
                raise WindowsWebUIError("WINDOWS_SETUP_BUSY", "managed service must be quiescent before recovery")
        self.supervisor.verify_quiescent()

    def rollback_setup(self, *, plan, completed_operations):
        del plan, completed_operations
        self._close_stages()
        if getattr(self, "_journal", None) is None:
            return InstallerStepResult(status=InstallerStepStatus.APPLIED, operation="ROLLBACK", detail="no prepared mutation")
        self._assert_recovery_quiescent()
        self._journal["phase"] = "ROLLING_BACK"
        self._persist_journal()
        conflicts = []
        for record in reversed(self._journal["operations"]):
            try:
                current = self._snapshot_target(record["kind"], record["target"])
                if current == record["before"] or (record["restore"] is not None and current == record["restore"]):
                    record["status"] = "RESTORED"
                elif record["after"] is not None and current == record["after"]:
                    record["restore"] = self._restore_record(record, current)
                    if self._snapshot_target(record["kind"], record["target"]) != record["restore"]:
                        raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "rollback postimage changed")
                    record["status"] = "RESTORED"
                elif record["after"] is None and (
                    (
                        record["kind"] != "REGISTER_WINDOWS_TASK"
                        and current == record["intended"]
                    )
                    or self._owned_task_intermediate(record, current)
                ):
                    record["restore"] = self._restore_record(record, current)
                    if self._snapshot_target(record["kind"], record["target"]) != record["restore"]:
                        raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "rollback postimage changed")
                    record["status"] = "RESTORED"
                else:
                    raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "target changed outside transaction custody")
            except Exception:
                record["status"] = "CONFLICT"
                conflicts.append(record["kind"])
            self._persist_journal()
        self._journal["phase"] = "RECOVERY_REQUIRED" if conflicts else "ROLLED_BACK"
        self._persist_journal()
        return InstallerStepResult(status=InstallerStepStatus.RECOVERY_REQUIRED if conflicts else InstallerStepStatus.APPLIED,
            operation="ROLLBACK", detail="unresolved custody: " + ", ".join(conflicts) if conflicts else "exact owned state restored")

    def rollback_committed_setup(self, transaction_id: str) -> InstallerStepResult:
        """Rollback one exact committed Windows setup transaction, or fail closed."""

        try:
            expected_transaction_id = str(uuid.UUID(transaction_id))
        except (ValueError, TypeError) as exc:
            raise WindowsWebUIError(
                "WINDOWS_SETUP_TRANSACTION_INVALID",
                "Windows setup transaction identity is invalid",
            ) from exc
        self._journal = None
        self._stages = {}
        self._owner_sid = windows.current_user_sid()
        canonical = windows.windows_owner_paths(owner_sid=self._owner_sid)
        self._validate_launcher_target()
        if self._paths != canonical:
            raise WindowsWebUIError(
                "WINDOWS_SETUP_PATH_INVALID",
                "Windows setup requires fixed canonical owner paths",
            )
        for raw in (
            self._paths.root_dir,
            self._paths.owner_config_home,
            self._paths.runtime_dir,
            self._paths.state_dir,
        ):
            path = validate_windows_path_no_reparse(raw)
            if not path.exists():
                raise WindowsWebUIError(
                    "WINDOWS_SETUP_RECOVERY_REQUIRED",
                    "Windows setup recovery owner path is unavailable",
                )
            windows.validate_owner_private_path(path, owner_sid=self._owner_sid, directory=True)
        self._journal_path = str(PureWindowsPath(self.request.state_dir) / "windows-setup-journal.json")
        custody = windows.snapshot_windows_setup_file(
            self._journal_path,
            owner_sid=self._owner_sid,
            limit=WINDOWS_SETUP_JOURNAL_LIMIT,
        )
        if not custody["exists"]:
            raise WindowsWebUIError(
                "WINDOWS_SETUP_RECOVERY_REQUIRED",
                "committed Windows setup journal is unavailable",
            )
        journal = _decode_windows_setup_journal(
            base64.b64decode(custody["content"]),
            installation_id=self.request.installation_id,
            owner_sid=self._owner_sid,
            targets=self._targets(),
        )
        if journal["transaction_id"] != expected_transaction_id or journal["phase"] != "COMMITTED":
            raise WindowsWebUIError(
                "WINDOWS_SETUP_TRANSACTION_MISMATCH",
                "committed Windows setup transaction does not match the requested rollback",
            )
        self._journal = journal
        self._journal_custody = custody
        return self.rollback_setup(plan=None, completed_operations=())

    def read_saved_endpoint(self) -> str | None:
        return None

    def activate_candidate(self, *, plan: SetupPlan) -> ManagedServiceActivationResult:
        del plan
        return ManagedServiceActivationResult(
            status=ManagedServiceActivationStatus.PENDING,
            detail="Windows endpoint activation is owned by the managed runtime",
        )

    def stop_candidate(self, *, plan: SetupPlan) -> InstallerStepResult:
        del plan
        return InstallerStepResult(
            status=InstallerStepStatus.APPLIED,
            operation="STOP_MANAGED_SERVICE",
        )


def rollback_committed_windows_webui_setup(
    request: SetupRequest,
    transaction_id: str,
    *,
    supervisor: WindowsTaskSchedulerSupervisor | None = None,
) -> SetupApplyResult:
    """Recover one exact committed native-Windows setup without replaying service state."""

    backend = ProductionWindowsInstallerBackend(
        request=request,
        supervisor=supervisor,
    )
    try:
        with backend.mutation_guard():
            rollback = backend.rollback_committed_setup(transaction_id)
    except WindowsWebUIError as exc:
        return SetupApplyResult(
            status=InstallerStepStatus.RECOVERY_REQUIRED,
            endpoint_state=EndpointState.PROPOSED,
            transaction_id=transaction_id,
            detail=exc.safe_message,
        )
    return SetupApplyResult(
        status=rollback.status,
        endpoint_state=EndpointState.PROPOSED,
        transaction_id=transaction_id,
        detail=rollback.detail,
    )


def _rebind_plan_endpoint(
    plan: SetupPlan,
    port: int,
    state: EndpointState,
) -> SetupPlan:
    _validate_saved_port(port)
    origin = f"http://127.0.0.1:{port}"
    caddy_config = render_caddy_config(
        hostname=plan.hostname,
        port=port,
        backend_socket=plan.backend_socket,
        caddy_state_dir=plan.caddy_state_dir,
    )
    operations: list[PlannedOperation] = []
    for operation in plan.operations:
        if operation.kind == "WRITE_SERVICE_CONFIG":
            payload = json.loads(operation.desired_state)
            if not isinstance(payload, dict):
                raise ValueError("service config plan payload is invalid")
            payload["origin"] = origin
            desired_state = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            operations.append(replace(operation, desired_state=desired_state))
        elif operation.kind == "WRITE_CADDY_CONFIG":
            operations.append(replace(operation, desired_state=caddy_config))
        else:
            operations.append(operation)
    return replace(
        plan,
        origin=origin,
        saved_port=port,
        caddy_config=caddy_config,
        operations=tuple(operations),
        endpoint_state=state,
    )


def _saved_endpoint_port_for_plan(saved_endpoint: str, plan: SetupPlan) -> int:
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(saved_endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("saved endpoint is invalid; explicit endpoint recovery is required") from exc
    local_http = (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None
        and saved_endpoint == f"http://127.0.0.1:{port}"
    )
    legacy_https = (
        parsed.scheme == "https"
        and parsed.hostname == plan.hostname
        and port is not None
        and saved_endpoint == f"https://{plan.hostname}:{port}"
    )
    if (
        not (local_http or legacy_https)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port is None
        or not WEBUI_PORT_MIN <= port <= WEBUI_PORT_MAX
    ):
        raise ValueError("saved endpoint is invalid; explicit endpoint recovery is required")
    return port


def _consent_granted(consent: SetupConsent, key: str) -> bool:
    if key == "hostname_mapping":
        return consent.hostname_mapping
    if key == "browser_profile_trust":
        return consent.browser_profile_trust
    if key == "user_lingering":
        return consent.user_lingering
    if key == "windows_native_setup":
        return consent.windows_native_setup
    return False


def _is_windows_setup_plan(plan: SetupPlan) -> bool:
    kinds = {operation.kind for operation in plan.operations}
    return "ENSURE_WINDOWS_OWNER_STATE" in kinds or "REGISTER_WINDOWS_TASK" in kinds


def _atomic_owner_file_write(
    path: Path,
    content: str,
    *,
    private_parent: bool,
) -> None:
    _validate_absolute_path(str(path), "owner file target")
    _ensure_real_directory_chain(path.parent)
    if private_parent:
        os.chmod(path.parent, 0o700)
    try:
        target_stat = path.lstat()
    except FileNotFoundError:
        target_stat = None
    if target_stat is not None and (
        stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode)
    ):
        raise ValueError("owner file target must be a regular file and never a symlink")

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
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


def _ensure_real_directory_chain(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("owner directory must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            continue
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise ValueError("owner file path must not traverse symlink or non-directory components")


def linux_setup_journal_path(state_dir: str | os.PathLike[str]) -> Path:
    value = os.fspath(state_dir)
    _validate_absolute_path(value, "Linux setup state directory")
    return Path(value) / LINUX_SETUP_JOURNAL_FILENAME


def render_linux_firefox_network_policy(hostname: str) -> str:
    _validate_hostname(hostname)
    return (
        f'user_pref("network.dns.localDomains", "{hostname}");\n'
        'user_pref("network.trr.mode", 5);\n'
        'user_pref("network.proxy.type", 0);\n'
    )


def render_linux_setup_journal(
    *,
    request: SetupRequest,
    browser: BrowserInstall | None,
    hostname: str,
) -> str:
    _validate_hostname(hostname)
    payload = {
        "version": 1,
        "phase": "PROVISIONAL",
        "installation_id": request.installation_id,
        "hostname": hostname,
        "installing_user": request.installing_user,
        "browser_family": browser.family if browser is not None else None,
        "browser_executable": browser.executable if browser is not None else None,
        "browser_profile_dir": request.browser_profile_dir,
        "state_dir": request.state_dir,
        "caddy_state_dir": request.caddy_state_dir,
        "root_fingerprint_sha256": None,
        "ownership": _new_linux_setup_ownership(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def _new_linux_setup_ownership() -> dict[str, object]:
    return {
        "files": {},
        "file_order": [],
        "linger_preexisting": None,
        "linger_change_intent": False,
        "linger_changed": False,
        "service_enabled_preexisting": None,
        "service_enable_intent": False,
        "service_enable_changed": False,
        "profile_preexisting": None,
        "profile_device": None,
        "profile_inode": None,
        "profile_create_intent": False,
        "profile_created": False,
        "profile_created_files": [],
        "user_js_preexisting": None,
        "user_js_create_intent": False,
        "nss_preexisting": None,
        "nss_create_intent": False,
        "trust_preexisting": None,
        "trust_import_intent": False,
        "trust_imported": False,
        "trust_manifest_intent": False,
        "trust_manifest_created": False,
        "caddy_state_preexisting": None,
        "caddy_state_create_intent": False,
        "caddy_state_device": None,
        "caddy_state_inode": None,
        "caddy_state_created": False,
    }


def _require_owner_private_regular_file(path: Path, *, owner_uid: int) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LinuxSetupRecoveryRequired("foreign managed state is not a regular file")
    if info.st_uid != owner_uid:
        raise LinuxSetupRecoveryRequired("foreign managed state is not owned by the installing user")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise LinuxSetupRecoveryRequired("foreign managed state is not owner-private mode 0600")
    return info


def _require_owner_private_directory(path: Path, *, owner_uid: int) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LinuxSetupRecoveryRequired("foreign managed state is not a real directory")
    if info.st_uid != owner_uid:
        raise LinuxSetupRecoveryRequired("foreign managed directory is not owned by the installing user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise LinuxSetupRecoveryRequired("foreign managed directory is not owner-private mode 0700")
    return info


def _sha256_regular_owner_file(path: Path, *, owner_uid: int) -> str:
    _require_owner_private_regular_file(path, owner_uid=owner_uid)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _restore_managed_file_from_record(
    path: Path,
    record: dict[str, object],
    *,
    owner_uid: int,
) -> None:
    before_exists = record.get("before_exists") is True
    before_sha = record.get("before_sha256")
    intended_sha = record.get("intended_sha256")
    managed_sha = record.get("managed_sha256")
    try:
        current_info = path.lstat()
    except FileNotFoundError:
        current_info = None
    if current_info is not None:
        if stat.S_ISLNK(current_info.st_mode) or not stat.S_ISREG(current_info.st_mode):
            raise LinuxSetupRecoveryRequired("rollback target changed to foreign non-regular state")
        if current_info.st_uid != owner_uid:
            raise LinuxSetupRecoveryRequired("rollback target ownership changed outside setup")
        current_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        allowed = {
            value
            for value in (before_sha, intended_sha, managed_sha)
            if isinstance(value, str)
        }
        if current_sha not in allowed:
            raise LinuxSetupRecoveryRequired("rollback target bytes changed outside setup")
        if before_exists and current_sha == before_sha:
            return
    elif before_exists:
        raise LinuxSetupRecoveryRequired("rollback target disappeared outside setup")
    else:
        return

    if not before_exists:
        assert current_info is not None
        path.unlink()
        return
    before_text = record.get("before_text")
    before_mode = record.get("before_mode")
    if not isinstance(before_text, str) or isinstance(before_mode, bool) or not isinstance(before_mode, int):
        raise LinuxSetupRecoveryRequired("rollback journal lacks exact original file bytes/mode")
    _atomic_owner_file_write(path, before_text, private_parent=False)
    os.chmod(path, before_mode)
    if hashlib.sha256(path.read_bytes()).hexdigest() != before_sha:
        raise LinuxSetupRecoveryRequired("rollback failed to restore exact original file bytes")


def _remove_owner_created_tree(path: Path, *, owner_uid: int) -> None:
    _require_owner_private_directory(path, owner_uid=owner_uid)
    for root, directories, files in os.walk(path, topdown=False, followlinks=False):
        root_path = Path(root)
        root_info = root_path.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != owner_uid:
            raise LinuxSetupRecoveryRequired("rollback found foreign directory state")
        for filename in files:
            target = root_path / filename
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid:
                raise LinuxSetupRecoveryRequired("rollback found foreign file state")
            target.unlink()
        for dirname in directories:
            target = root_path / dirname
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid:
                raise LinuxSetupRecoveryRequired("rollback found foreign nested directory state")
            target.rmdir()
    path.rmdir()


def _remove_owned_caddy_state_tree(
    path: Path,
    *,
    owner_uid: int,
    hostname: str,
) -> None:
    """Remove only the dedicated Caddy state namespace proven owned by setup."""

    _require_owner_private_directory(path, owner_uid=owner_uid)
    _validate_hostname(hostname)
    bootstrap = path / ".factlane-ca-bootstrap.Caddyfile"
    for child in path.iterdir():
        if child.name == "pki":
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid:
                raise LinuxSetupRecoveryRequired("Caddy PKI state has foreign identity")
            continue
        if child.name in {"certificates", "locks"}:
            _require_owner_private_directory(child, owner_uid=owner_uid)
            continue
        if child == bootstrap:
            _require_owner_private_regular_file(child, owner_uid=owner_uid)
            expected = _render_caddy_ca_bootstrap_config(
                hostname=hostname,
                caddy_state_dir=str(path),
            )
            if child.read_text(encoding="utf-8") != expected:
                raise LinuxSetupRecoveryRequired("Caddy bootstrap state has foreign bytes")
            child.unlink()
            continue
        if child.name == "last_clean.json":
            _remove_owned_caddy_last_clean_marker(child, owner_uid=owner_uid)
            continue
        raise LinuxSetupRecoveryRequired(
            "Caddy state contains unrelated top-level content; rollback will not remove it"
        )
    pki = path / "pki"
    if pki.exists() or pki.is_symlink():
        allowed_directories = {
            "pki",
            "pki/authorities",
            "pki/authorities/local",
        }
        allowed_files = {
            "pki/authorities/local/root.crt",
            "pki/authorities/local/root.key",
            "pki/authorities/local/intermediate.crt",
            "pki/authorities/local/intermediate.key",
        }
        for root, directories, files in os.walk(pki, topdown=False, followlinks=False):
            root_path = Path(root)
            root_relative = root_path.relative_to(path).as_posix()
            if root_relative not in allowed_directories:
                raise LinuxSetupRecoveryRequired(
                    "Caddy PKI state contains an unrelated directory"
                )
            root_info = root_path.lstat()
            if (
                stat.S_ISLNK(root_info.st_mode)
                or not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != owner_uid
            ):
                raise LinuxSetupRecoveryRequired("Caddy PKI directory has foreign identity")
            for filename in files:
                target = root_path / filename
                target_relative = target.relative_to(path).as_posix()
                if target_relative not in allowed_files:
                    raise LinuxSetupRecoveryRequired(
                        "Caddy PKI state contains an unrelated file"
                    )
                info = target.lstat()
                if (
                    stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != owner_uid
                ):
                    raise LinuxSetupRecoveryRequired("Caddy PKI file has foreign identity")
                target.unlink()
            for dirname in directories:
                target = root_path / dirname
                target_relative = target.relative_to(path).as_posix()
                if target_relative not in allowed_directories:
                    raise LinuxSetupRecoveryRequired(
                        "Caddy PKI state contains an unrelated directory"
                    )
                info = target.lstat()
                if (
                    stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != owner_uid
                ):
                    raise LinuxSetupRecoveryRequired(
                        "Caddy PKI nested directory has foreign identity"
                    )
                target.rmdir()
        pki.rmdir()

    certificates = path / "certificates"
    if certificates.exists() or certificates.is_symlink():
        leaf_dir = f"certificates/local/{hostname}"
        allowed_directories = {
            "certificates",
            "certificates/local",
            leaf_dir,
        }
        allowed_files = {
            f"{leaf_dir}/{hostname}.crt",
            f"{leaf_dir}/{hostname}.key",
            f"{leaf_dir}/{hostname}.json",
        }
        for root, directories, files in os.walk(
            certificates,
            topdown=False,
            followlinks=False,
        ):
            root_path = Path(root)
            root_relative = root_path.relative_to(path).as_posix()
            if root_relative not in allowed_directories:
                raise LinuxSetupRecoveryRequired(
                    "Caddy certificate state contains an unrelated directory"
                )
            _require_owner_private_directory(root_path, owner_uid=owner_uid)
            for filename in files:
                target = root_path / filename
                target_relative = target.relative_to(path).as_posix()
                if target_relative not in allowed_files:
                    raise LinuxSetupRecoveryRequired(
                        "Caddy certificate state contains an unrelated file"
                    )
                _require_owner_private_regular_file(target, owner_uid=owner_uid)
                target.unlink()
            for dirname in directories:
                target = root_path / dirname
                target_relative = target.relative_to(path).as_posix()
                if target_relative not in allowed_directories:
                    raise LinuxSetupRecoveryRequired(
                        "Caddy certificate state contains an unrelated directory"
                    )
                _require_owner_private_directory(target, owner_uid=owner_uid)
                target.rmdir()
        certificates.rmdir()

    locks = path / "locks"
    if locks.exists() or locks.is_symlink():
        _require_owner_private_directory(locks, owner_uid=owner_uid)
        if any(locks.iterdir()):
            raise LinuxSetupRecoveryRequired(
                "Caddy lock state is not empty after the managed service stopped"
            )
        locks.rmdir()
    path.rmdir()


def _remove_owned_caddy_last_clean_marker(path: Path, *, owner_uid: int) -> None:
    """Remove only Caddy's bounded storage-cleaning marker from setup-created state."""

    info = _require_owner_private_regular_file(path, owner_uid=owner_uid)
    if info.st_size > 1024:
        raise LinuxSetupRecoveryRequired("Caddy last-clean marker exceeds the bounded schema")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinuxSetupRecoveryRequired("Caddy last-clean marker is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {"tls"}:
        raise LinuxSetupRecoveryRequired("Caddy last-clean marker has foreign top-level fields")
    tls = payload.get("tls")
    if not isinstance(tls, dict) or set(tls) != {"timestamp", "instance_id"}:
        raise LinuxSetupRecoveryRequired("Caddy last-clean marker has foreign TLS fields")
    timestamp = tls.get("timestamp")
    instance_id = tls.get("instance_id")
    if not isinstance(timestamp, str) or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        timestamp,
    ) is None:
        raise LinuxSetupRecoveryRequired("Caddy last-clean marker timestamp is invalid")
    if not isinstance(instance_id, str):
        raise LinuxSetupRecoveryRequired("Caddy last-clean marker instance identity is invalid")
    try:
        parsed = uuid.UUID(instance_id)
    except ValueError as exc:
        raise LinuxSetupRecoveryRequired("Caddy last-clean marker instance identity is invalid") from exc
    if str(parsed) != instance_id.lower():
        raise LinuxSetupRecoveryRequired("Caddy last-clean marker instance identity is non-canonical")
    path.unlink()


def _parse_linux_setup_journal(path: Path, *, owner_uid: int) -> dict[str, object]:
    _require_owner_private_regular_file(path, owner_uid=owner_uid)
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinuxSetupRecoveryRequired("provisional setup journal is unreadable or invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise LinuxSetupRecoveryRequired("provisional setup journal has a foreign schema")
    installation_id = payload.get("installation_id")
    hostname = payload.get("hostname")
    if not isinstance(installation_id, str) or hostname != webui_hostname(installation_id):
        raise LinuxSetupRecoveryRequired("provisional setup journal has a foreign installation binding")
    phase = payload.get("phase")
    if phase not in {"PROVISIONAL", "TRUST_READY", "ACTIVATING", "CLAIMED", "RECOVERY_REQUIRED"}:
        raise LinuxSetupRecoveryRequired("provisional setup journal has an invalid phase")
    ownership = payload.get("ownership")
    if not isinstance(ownership, dict):
        raise LinuxSetupRecoveryRequired("provisional setup journal ownership state is invalid")
    return payload


def load_linux_provisional_installation_id(
    state_dir: str | os.PathLike[str],
    *,
    owner_uid: int,
) -> str | None:
    path = linux_setup_journal_path(state_dir)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    payload = _parse_linux_setup_journal(path, owner_uid=owner_uid)
    expected_state = str(Path(os.fspath(state_dir)))
    if payload.get("state_dir") != expected_state:
        raise LinuxSetupRecoveryRequired("provisional setup journal is bound to a different state directory")
    installation_id = payload["installation_id"]
    assert isinstance(installation_id, str)
    return installation_id


def _ensure_linux_setup_journal(
    path: Path,
    desired_state: str,
    *,
    owner_uid: int,
) -> dict[str, object]:
    try:
        expected = json.loads(desired_state)
    except json.JSONDecodeError as exc:
        raise ValueError("provisional setup journal desired state is invalid") from exc
    if not isinstance(expected, dict) or expected.get("version") != 1:
        raise ValueError("provisional setup journal desired state has an invalid schema")
    try:
        path.lstat()
    except FileNotFoundError:
        _atomic_owner_file_write(path, desired_state, private_parent=True)
        _require_owner_private_regular_file(path, owner_uid=owner_uid)
        _require_owner_private_directory(path.parent, owner_uid=owner_uid)
        return expected
    current = _parse_linux_setup_journal(path, owner_uid=owner_uid)
    stable_binding_keys = (
        "version",
        "installation_id",
        "hostname",
        "installing_user",
        "state_dir",
        "caddy_state_dir",
        "browser_profile_dir",
    )
    if any(current.get(key) != expected.get(key) for key in stable_binding_keys):
        raise LinuxSetupRecoveryRequired("provisional setup journal conflicts with the exact setup target")
    browser_keys = ("browser_family", "browser_executable")
    if any(current.get(key) != expected.get(key) for key in browser_keys):
        # Browser identity was a binding field in the legacy HTTPS/isolated-
        # Firefox setup.  A successfully CLAIMED legacy installation may be
        # canonicalized forward to the browser-neutral loopback transport.  Do
        # not relax this rule for interrupted/provisional/recovery attempts.
        if current.get("phase") != "CLAIMED" or any(
            expected.get(key) is not None for key in browser_keys
        ):
            raise LinuxSetupRecoveryRequired(
                "provisional setup journal conflicts with the exact setup target"
            )
        for key in browser_keys:
            current[key] = expected.get(key)
    return current


def _update_linux_setup_journal(
    path: Path,
    *,
    owner_uid: int,
    phase: str | None = None,
    root_fingerprint_sha256: str | None = None,
    ownership_updates: dict[str, object] | None = None,
    ownership_replace: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = _parse_linux_setup_journal(path, owner_uid=owner_uid)
    if phase is not None:
        if phase not in {"PROVISIONAL", "TRUST_READY", "ACTIVATING", "CLAIMED", "RECOVERY_REQUIRED"}:
            raise ValueError("invalid Linux setup journal phase")
        payload["phase"] = phase
    if root_fingerprint_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", root_fingerprint_sha256):
            raise ValueError("root fingerprint must be lowercase SHA-256 hex")
        payload["root_fingerprint_sha256"] = root_fingerprint_sha256
    if ownership_replace is not None:
        payload["ownership"] = ownership_replace
    elif ownership_updates:
        ownership = payload.get("ownership")
        if not isinstance(ownership, dict):
            raise LinuxSetupRecoveryRequired("provisional setup journal ownership state is invalid")
        ownership.update(ownership_updates)
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    _atomic_owner_file_write(path, rendered, private_parent=True)
    _require_owner_private_regular_file(path, owner_uid=owner_uid)
    return payload


def ensure_linux_firefox_profile_network_policy(
    *,
    profile_dir: str | os.PathLike[str] | Path,
    hostname: str,
    owner_uid: int,
) -> LinuxFirefoxNetworkPolicyReceipt:
    _validate_hostname(hostname)
    profile = Path(profile_dir)
    _validate_absolute_path(str(profile), "isolated Firefox profile")
    try:
        profile.lstat()
        profile_created = False
        _require_owner_private_directory(profile, owner_uid=owner_uid)
    except FileNotFoundError:
        _ensure_real_directory_chain(profile.parent)
        profile.mkdir(mode=0o700)
        os.chmod(profile, 0o700)
        _require_owner_private_directory(profile, owner_uid=owner_uid)
        profile_created = True
    user_js = profile / LINUX_FIREFOX_MANAGED_USER_JS
    expected = render_linux_firefox_network_policy(hostname)
    try:
        user_js.lstat()
    except FileNotFoundError:
        _atomic_owner_file_write(user_js, expected, private_parent=True)
        _require_owner_private_regular_file(user_js, owner_uid=owner_uid)
        user_js_created = True
    else:
        _require_owner_private_regular_file(user_js, owner_uid=owner_uid)
        try:
            current = user_js.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise LinuxSetupRecoveryRequired("foreign Firefox user.js cannot be read safely") from exc
        if current != expected:
            raise LinuxSetupRecoveryRequired("foreign Firefox user.js conflicts with managed network policy")
        user_js_created = False
    return LinuxFirefoxNetworkPolicyReceipt(
        status="CREATED" if profile_created or user_js_created else "REUSED",
        profile_dir=str(profile),
        user_js_path=str(user_js),
        profile_created=profile_created,
        user_js_created=user_js_created,
    )


def _certificate_fingerprint_from_pem_text(pem_text: str) -> str:
    if pem_text.count("-----BEGIN CERTIFICATE-----") != 1 or pem_text.count(
        "-----END CERTIFICATE-----"
    ) != 1:
        raise LinuxSetupRecoveryRequired("Caddy public root must contain exactly one certificate")
    if "PRIVATE KEY" in pem_text:
        raise LinuxSetupRecoveryRequired("Caddy public root file unexpectedly contains private key material")
    try:
        der = ssl.PEM_cert_to_DER_cert(pem_text)
    except ValueError as exc:
        raise LinuxSetupRecoveryRequired("Caddy public root certificate is invalid") from exc
    return hashlib.sha256(der).hexdigest()


def _root_certificate_fingerprint(path: Path, *, owner_uid: int) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LinuxSetupRecoveryRequired("Caddy public root must be a regular file")
    if info.st_uid != owner_uid:
        raise LinuxSetupRecoveryRequired("Caddy public root is not owned by the installing user")
    try:
        pem_text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise LinuxSetupRecoveryRequired("Caddy public root could not be read safely") from exc
    return _certificate_fingerprint_from_pem_text(pem_text)


def _linux_firefox_manifest_text(
    *,
    hostname: str,
    profile: Path,
    firefox_executable: str,
    root_fingerprint_sha256: str,
) -> str:
    payload = {
        "version": 1,
        "hostname": hostname,
        "profile_dir": str(profile),
        "firefox_executable": firefox_executable,
        "root_fingerprint_sha256": root_fingerprint_sha256,
        "nickname": LINUX_FIREFOX_CA_NICKNAME,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def _certutil_entries(output: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line or "Trust Attributes" in line or line.strip().startswith("SSL,S/MIME"):
            continue
        match = re.match(r"^(?P<nickname>.+?)(?:\s{2,})(?P<trust>[A-Za-z0-9,]+)\s*$", line)
        if match is None:
            continue
        entries.append((match.group("nickname").rstrip(), match.group("trust")))
    return entries


def ensure_linux_firefox_profile_trust(
    *,
    certutil_path: str,
    firefox_executable: str,
    profile_dir: str | os.PathLike[str] | Path,
    root_cert_path: str | os.PathLike[str] | Path,
    hostname: str,
    owner_uid: int,
    command_runner: CommandRunner | None = None,
    read_command_runner: ReadCommandRunner | None = None,
    mutation_intent_hook: Callable[[str], None] | None = None,
    recover_owned_missing_manifest: bool = False,
) -> LinuxFirefoxTrustReceipt:
    _validate_absolute_path(certutil_path, "certutil executable")
    _validate_absolute_path(firefox_executable, "Firefox executable")
    _validate_hostname(hostname)
    runner = command_runner or _run_argv
    reader = read_command_runner or _run_argv_capture
    profile = Path(profile_dir)
    _validate_absolute_path(str(profile), "isolated Firefox profile")
    _require_owner_private_directory(profile, owner_uid=owner_uid)
    user_js = profile / LINUX_FIREFOX_MANAGED_USER_JS
    _require_owner_private_regular_file(user_js, owner_uid=owner_uid)
    if user_js.read_text(encoding="utf-8") != render_linux_firefox_network_policy(hostname):
        raise LinuxSetupRecoveryRequired("foreign Firefox network policy conflicts with the managed profile")

    root_path = Path(root_cert_path)
    _validate_absolute_path(str(root_path), "Caddy public root certificate")
    root_fingerprint = _root_certificate_fingerprint(root_path, owner_uid=owner_uid)
    manifest_path = profile / LINUX_FIREFOX_TRUST_MANIFEST
    expected_manifest = _linux_firefox_manifest_text(
        hostname=hostname,
        profile=profile,
        firefox_executable=firefox_executable,
        root_fingerprint_sha256=root_fingerprint,
    )
    manifest_exists = False
    try:
        manifest_path.lstat()
    except FileNotFoundError:
        pass
    else:
        _require_owner_private_regular_file(manifest_path, owner_uid=owner_uid)
        if manifest_path.read_text(encoding="utf-8") != expected_manifest:
            raise LinuxSetupRecoveryRequired("foreign Firefox trust manifest conflicts with the exact managed binding")
        manifest_exists = True

    db_files = tuple(profile / name for name in ("cert9.db", "key4.db", "pkcs11.txt"))
    present = [path.exists() for path in db_files]
    if any(present) and not all(present):
        raise LinuxSetupRecoveryRequired("Firefox NSS SQL profile is incomplete or foreign")
    db_initialized = not any(present)
    created_db_files: list[Path] = []
    certificate_imported = False
    manifest_created = False
    db_arg = f"sql:{profile}"
    try:
        if db_initialized:
            if mutation_intent_hook is not None:
                mutation_intent_hook("nss")
            if runner((certutil_path, "-N", "--empty-password", "-d", db_arg)) != 0:
                raise LinuxSetupRecoveryRequired("Firefox NSS SQL profile could not be initialized")
            for path in db_files:
                if not path.exists():
                    raise LinuxSetupRecoveryRequired("Firefox NSS initialization did not create the exact SQL profile")
                os.chmod(path, 0o600)
                _require_owner_private_regular_file(path, owner_uid=owner_uid)
                created_db_files.append(path)
        else:
            for path in db_files:
                _require_owner_private_regular_file(path, owner_uid=owner_uid)

        list_code, list_stdout = reader((certutil_path, "-L", "-d", db_arg))
        if list_code != 0:
            raise LinuxSetupRecoveryRequired("Firefox NSS certificate database could not be inspected")
        entries = _certutil_entries(list_stdout)
        managed_entry = next((item for item in entries if item[0] == LINUX_FIREFOX_CA_NICKNAME), None)
        same_root_other_nickname: str | None = None
        for nickname, _trust in entries:
            export_code, export_stdout = reader(
                (certutil_path, "-L", "-d", db_arg, "-n", nickname, "-a")
            )
            if export_code != 0:
                raise LinuxSetupRecoveryRequired("Firefox NSS certificate state could not be verified")
            try:
                fingerprint = _certificate_fingerprint_from_pem_text(export_stdout)
            except LinuxSetupRecoveryRequired:
                continue
            if fingerprint == root_fingerprint and nickname != LINUX_FIREFOX_CA_NICKNAME:
                same_root_other_nickname = nickname
                break
        if same_root_other_nickname is not None:
            raise LinuxSetupRecoveryRequired(
                "foreign Firefox trust already contains the FactLane root under another nickname"
            )

        if managed_entry is not None:
            export_code, export_stdout = reader(
                (
                    certutil_path,
                    "-L",
                    "-d",
                    db_arg,
                    "-n",
                    LINUX_FIREFOX_CA_NICKNAME,
                    "-a",
                )
            )
            if export_code != 0 or _certificate_fingerprint_from_pem_text(export_stdout) != root_fingerprint:
                raise LinuxSetupRecoveryRequired("foreign Firefox FactLane nickname is bound to a different root")
            if managed_entry[1] != "C,,":
                raise LinuxSetupRecoveryRequired("Firefox FactLane root trust flags drifted from the exact SSL CA binding")
            if not manifest_exists:
                if not recover_owned_missing_manifest:
                    raise LinuxSetupRecoveryRequired("Firefox FactLane trust exists without its ownership manifest")
                if mutation_intent_hook is not None:
                    mutation_intent_hook("manifest")
                _atomic_owner_file_write(manifest_path, expected_manifest, private_parent=True)
                _require_owner_private_regular_file(manifest_path, owner_uid=owner_uid)
                return LinuxFirefoxTrustReceipt(
                    status="RECOVERED",
                    profile_dir=str(profile),
                    manifest_path=str(manifest_path),
                    root_fingerprint_sha256=root_fingerprint,
                    db_initialized=False,
                    certificate_imported=True,
                    manifest_created=True,
                )
            return LinuxFirefoxTrustReceipt(
                status="REUSED",
                profile_dir=str(profile),
                manifest_path=str(manifest_path),
                root_fingerprint_sha256=root_fingerprint,
                db_initialized=db_initialized,
                certificate_imported=False,
                manifest_created=False,
            )
        if manifest_exists:
            raise LinuxSetupRecoveryRequired("Firefox trust manifest exists but the managed root is missing")

        import_argv = (
            certutil_path,
            "-A",
            "-d",
            db_arg,
            "-n",
            LINUX_FIREFOX_CA_NICKNAME,
            "-t",
            "C,,",
            "-i",
            str(root_path),
        )
        if mutation_intent_hook is not None:
            mutation_intent_hook("trust")
        if runner(import_argv) != 0:
            raise LinuxSetupRecoveryRequired("Firefox NSS rejected the exact FactLane public root import")
        certificate_imported = True
        verify_code, verify_stdout = reader((certutil_path, "-L", "-d", db_arg))
        if verify_code != 0:
            raise LinuxSetupRecoveryRequired("Firefox NSS trust import could not be verified")
        verified = next(
            (item for item in _certutil_entries(verify_stdout) if item[0] == LINUX_FIREFOX_CA_NICKNAME),
            None,
        )
        if verified is None or verified[1] != "C,,":
            raise LinuxSetupRecoveryRequired("Firefox NSS trust import did not bind exact SSL CA trust")
        export_code, export_stdout = reader(
            (certutil_path, "-L", "-d", db_arg, "-n", LINUX_FIREFOX_CA_NICKNAME, "-a")
        )
        if export_code != 0 or _certificate_fingerprint_from_pem_text(export_stdout) != root_fingerprint:
            raise LinuxSetupRecoveryRequired("Firefox NSS trust import fingerprint verification failed")
        if mutation_intent_hook is not None:
            mutation_intent_hook("manifest")
        _atomic_owner_file_write(manifest_path, expected_manifest, private_parent=True)
        _require_owner_private_regular_file(manifest_path, owner_uid=owner_uid)
        manifest_created = True
        return LinuxFirefoxTrustReceipt(
            status="IMPORTED",
            profile_dir=str(profile),
            manifest_path=str(manifest_path),
            root_fingerprint_sha256=root_fingerprint,
            db_initialized=db_initialized,
            certificate_imported=True,
            manifest_created=True,
        )
    except BaseException:
        if certificate_imported:
            try:
                runner((certutil_path, "-D", "-d", db_arg, "-n", LINUX_FIREFOX_CA_NICKNAME))
            except Exception:
                pass
        if manifest_created:
            try:
                manifest_path.unlink()
            except OSError:
                pass
        if db_initialized:
            for path in reversed(created_db_files):
                try:
                    path.unlink()
                except OSError:
                    pass
        raise


def validate_linux_firefox_managed_profile(
    *,
    certutil_path: str,
    firefox_executable: str,
    profile_dir: str | os.PathLike[str] | Path,
    root_cert_path: str | os.PathLike[str] | Path,
    hostname: str,
    owner_uid: int,
    read_command_runner: ReadCommandRunner | None = None,
) -> str:
    """Validate the already-provisioned isolated Firefox profile without mutating it."""

    _validate_absolute_path(certutil_path, "certutil executable")
    _validate_absolute_path(firefox_executable, "Firefox executable")
    _validate_hostname(hostname)
    profile = Path(profile_dir)
    _require_owner_private_directory(profile, owner_uid=owner_uid)
    user_js = profile / LINUX_FIREFOX_MANAGED_USER_JS
    _require_owner_private_regular_file(user_js, owner_uid=owner_uid)
    if user_js.read_text(encoding="utf-8") != render_linux_firefox_network_policy(hostname):
        raise LinuxSetupRecoveryRequired("managed Firefox network policy drifted")
    root_path = Path(root_cert_path)
    root_fingerprint = _root_certificate_fingerprint(root_path, owner_uid=owner_uid)
    manifest_path = profile / LINUX_FIREFOX_TRUST_MANIFEST
    _require_owner_private_regular_file(manifest_path, owner_uid=owner_uid)
    expected_manifest = _linux_firefox_manifest_text(
        hostname=hostname,
        profile=profile,
        firefox_executable=firefox_executable,
        root_fingerprint_sha256=root_fingerprint,
    )
    if manifest_path.read_text(encoding="utf-8") != expected_manifest:
        raise LinuxSetupRecoveryRequired("managed Firefox trust manifest drifted")
    for filename in ("cert9.db", "key4.db", "pkcs11.txt"):
        _require_owner_private_regular_file(profile / filename, owner_uid=owner_uid)
    reader = read_command_runner or _run_argv_capture
    db_arg = f"sql:{profile}"
    code, output = reader((certutil_path, "-L", "-d", db_arg))
    if code != 0:
        raise LinuxSetupRecoveryRequired("managed Firefox NSS database is unreadable")
    entry = next(
        (item for item in _certutil_entries(output) if item[0] == LINUX_FIREFOX_CA_NICKNAME),
        None,
    )
    if entry is None or entry[1] != "C,,":
        raise LinuxSetupRecoveryRequired("managed Firefox NSS trust binding is missing or drifted")
    code, pem = reader(
        (certutil_path, "-L", "-d", db_arg, "-n", LINUX_FIREFOX_CA_NICKNAME, "-a")
    )
    if code != 0 or _certificate_fingerprint_from_pem_text(pem) != root_fingerprint:
        raise LinuxSetupRecoveryRequired("managed Firefox NSS root fingerprint drifted")
    return root_fingerprint


def _render_caddy_ca_bootstrap_config(*, hostname: str, caddy_state_dir: str) -> str:
    _validate_hostname(hostname)
    _validate_absolute_path(caddy_state_dir, "Caddy state directory")
    return (
        "{\n"
        "    admin off\n"
        "    persist_config off\n"
        "    auto_https disable_redirects\n"
        "    skip_install_trust\n"
        "    storage_check off\n"
        "    storage_clean_interval off\n"
        f"    storage file_system {_caddy_token(caddy_state_dir)}\n"
        "}\n\n"
        f"https://{hostname}:0 {{\n"
        "    bind 127.0.0.1\n"
        "    tls {\n"
        "        issuer internal\n"
        "    }\n"
        "    respond 503\n"
        "}\n"
    )


def _caddy_bootstrap_storage_ready(
    state_dir: Path,
    *,
    hostname: str,
    owner_uid: int,
) -> str | None:
    """Return the local-root fingerprint only after Caddy's initial storage writes are complete.

    The public root is written before the CA key/intermediate material and before the exact
    leaf certificate acquisition has finished. Stopping Caddy at root.crt readiness can
    interrupt CertMagic's atomic FileStorage.Store() and strand a numeric temp file. The
    bootstrap is ready only after the exact startup storage graph is complete, issuance locks
    are gone, and no atomic temp remains.
    """

    _validate_hostname(hostname)
    allowed_dirs = {
        Path("pki"),
        Path("pki/authorities"),
        Path("pki/authorities/local"),
        Path("locks"),
        Path("certificates"),
        Path("certificates/local"),
        Path("certificates/local") / hostname,
    }
    allowed_files = {
        Path(".factlane-ca-bootstrap.Caddyfile"),
        Path("pki/authorities/local/root.crt"),
        Path("pki/authorities/local/root.key"),
        Path("pki/authorities/local/intermediate.crt"),
        Path("pki/authorities/local/intermediate.key"),
        Path("certificates/local") / hostname / f"{hostname}.crt",
        Path("certificates/local") / hostname / f"{hostname}.key",
        Path("certificates/local") / hostname / f"{hostname}.json",
    }
    expected_lock = Path("locks") / f"issue_cert_{hostname}.lock"

    try:
        _require_owner_private_directory(state_dir, owner_uid=owner_uid)
        current = list(state_dir.rglob("*"))
    except FileNotFoundError:
        return None

    for path in current:
        try:
            relative = path.relative_to(state_dir)
            info = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            if relative not in allowed_dirs:
                raise LinuxSetupRecoveryRequired(
                    "Caddy bootstrap created an unexpected storage directory"
                )
            _require_owner_private_directory(path, owner_uid=owner_uid)
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise LinuxSetupRecoveryRequired(
                "Caddy bootstrap storage contains foreign non-regular state"
            )
        if relative in allowed_files:
            _require_owner_private_regular_file(path, owner_uid=owner_uid)
            if info.st_size <= 0:
                return None
            continue
        if relative == expected_lock:
            try:
                _require_owner_private_regular_file(path, owner_uid=owner_uid)
            except FileNotFoundError:
                return None
            continue
        if re.fullmatch(r"\d{1,10}", path.name):
            try:
                _require_owner_private_regular_file(path, owner_uid=owner_uid)
            except FileNotFoundError:
                # Caddy atomically creates/removes numeric storage temps while
                # bootstrap is converging.  Disappearance after the first lstat
                # means only that readiness must be re-probed.
                return None
            return None
        raise LinuxSetupRecoveryRequired(
            "Caddy bootstrap created unexpected storage content"
        )

    for relative in allowed_files - {Path(".factlane-ca-bootstrap.Caddyfile")}:
        path = state_dir / relative
        try:
            info = _require_owner_private_regular_file(path, owner_uid=owner_uid)
        except FileNotFoundError:
            return None
        if info.st_size <= 0:
            return None

    locks = state_dir / "locks"
    try:
        _require_owner_private_directory(locks, owner_uid=owner_uid)
        if any(locks.iterdir()):
            return None
    except FileNotFoundError:
        return None

    metadata_path = state_dir / "certificates" / "local" / hostname / f"{hostname}.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict) or metadata.get("sans") != [hostname]:
        raise LinuxSetupRecoveryRequired(
            "Caddy bootstrap leaf-certificate metadata is invalid"
        )

    root_path = state_dir / "pki" / "authorities" / "local" / "root.crt"
    return _root_certificate_fingerprint(root_path, owner_uid=owner_uid)


def _spawn_caddy_bootstrap(argv: tuple[str, ...]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def bootstrap_linux_caddy_root(
    *,
    caddy_path: str,
    caddy_state_dir: str | os.PathLike[str],
    hostname: str,
    owner_uid: int,
    process_factory: Callable[[tuple[str, ...]], object] | None = None,
    clock: InstallClock = time.monotonic,
    sleep: InstallSleep = time.sleep,
    timeout: float = 10.0,
    require_complete_existing_state: bool = False,
) -> LinuxCaddyBootstrapReceipt:
    _validate_absolute_path(caddy_path, "Caddy executable")
    _validate_hostname(hostname)
    if timeout <= 0:
        raise ValueError("Caddy bootstrap timeout must be positive")
    state_dir = Path(caddy_state_dir)
    _validate_absolute_path(str(state_dir), "Caddy state directory")
    try:
        state_dir.lstat()
        state_created = False
        state_info = _require_owner_private_directory(state_dir, owner_uid=owner_uid)
    except FileNotFoundError:
        _ensure_real_directory_chain(state_dir.parent)
        state_dir.mkdir(mode=0o700)
        os.chmod(state_dir, 0o700)
        state_info = _require_owner_private_directory(state_dir, owner_uid=owner_uid)
        state_created = True
    root_path = state_dir / "pki" / "authorities" / "local" / "root.crt"
    try:
        fingerprint = _root_certificate_fingerprint(root_path, owner_uid=owner_uid)
    except FileNotFoundError:
        fingerprint = None
    if fingerprint is not None and require_complete_existing_state:
        fingerprint = _caddy_bootstrap_storage_ready(
            state_dir,
            hostname=hostname,
            owner_uid=owner_uid,
        )
    if fingerprint is not None:
        return LinuxCaddyBootstrapReceipt(
            status="REUSED",
            root_cert_path=str(root_path),
            root_fingerprint_sha256=fingerprint,
            caddy_state_created=state_created,
            caddy_state_device=int(state_info.st_dev),
            caddy_state_inode=int(state_info.st_ino),
        )

    bootstrap_config = state_dir / ".factlane-ca-bootstrap.Caddyfile"
    if bootstrap_config.exists() or bootstrap_config.is_symlink():
        raise LinuxSetupRecoveryRequired("foreign Caddy bootstrap config already exists")
    content = _render_caddy_ca_bootstrap_config(
        hostname=hostname,
        caddy_state_dir=str(state_dir),
    )
    _atomic_owner_file_write(bootstrap_config, content, private_parent=True)
    _require_owner_private_regular_file(bootstrap_config, owner_uid=owner_uid)
    factory = process_factory or _spawn_caddy_bootstrap
    process = None
    bootstrap_completed = False
    try:
        argv = (caddy_path, "run", "--config", str(bootstrap_config), "--adapter", "caddyfile")
        process = factory(argv)
        deadline = clock() + timeout
        while True:
            fingerprint = _caddy_bootstrap_storage_ready(
                state_dir,
                hostname=hostname,
                owner_uid=owner_uid,
            )
            if fingerprint is not None:
                bootstrap_completed = True
                break
            poll = getattr(process, "poll", None)
            if not callable(poll) or poll() is not None:
                raise LinuxSetupRecoveryRequired("Caddy CA bootstrap exited before complete storage readiness")
            now = clock()
            if now >= deadline:
                raise LinuxSetupRecoveryRequired("Caddy CA bootstrap timed out before complete storage readiness")
            sleep(min(0.05, deadline - now))
    finally:
        if process is not None:
            poll = getattr(process, "poll", None)
            terminate = getattr(process, "terminate", None)
            wait = getattr(process, "wait", None)
            kill = getattr(process, "kill", None)
            try:
                if callable(poll) and poll() is None and callable(terminate):
                    terminate()
                if callable(wait):
                    try:
                        wait(timeout=2.0)
                    except Exception:
                        if callable(kill):
                            kill()
                        try:
                            wait(timeout=2.0)
                        except Exception as exc:
                            raise LinuxSetupRecoveryRequired("Caddy CA bootstrap could not be proven stopped") from exc
                if callable(poll) and poll() is None:
                    raise LinuxSetupRecoveryRequired("Caddy CA bootstrap remained running after termination")
            finally:
                try:
                    bootstrap_config.unlink()
                except FileNotFoundError:
                    pass
        else:
            try:
                bootstrap_config.unlink()
            except FileNotFoundError:
                pass
        if not bootstrap_completed and state_created:
            try:
                _remove_owner_created_tree(state_dir, owner_uid=owner_uid)
            except (OSError, LinuxSetupRecoveryRequired) as exc:
                raise LinuxSetupRecoveryRequired(
                    "failed Caddy CA bootstrap could not roll back the state directory it created"
                ) from exc
    assert fingerprint is not None
    return LinuxCaddyBootstrapReceipt(
        status="CREATED",
        root_cert_path=str(root_path),
        root_fingerprint_sha256=fingerprint,
        caddy_state_created=state_created,
        caddy_state_device=int(state_info.st_dev),
        caddy_state_inode=int(state_info.st_ino),
    )


def probe_linux_managed_https(
    origin: str,
    *,
    root_cert_path: str | os.PathLike[str] | None,
    timeout_seconds: float = 1.0,
) -> ManagedServiceActivationResult:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return ManagedServiceActivationResult(
            status=ManagedServiceActivationStatus.FAILED,
            detail="managed origin is invalid",
        )
    hostname = parsed.hostname
    local_http = (
        parsed.scheme == "http"
        and hostname == "127.0.0.1"
        and port is not None
        and WEBUI_PORT_MIN <= port <= WEBUI_PORT_MAX
        and origin == f"http://127.0.0.1:{port}"
    )
    legacy_https = (
        parsed.scheme == "https"
        and hostname is not None
        and port is not None
        and origin == f"https://{hostname}:{port}"
    )
    if not (local_http or legacy_https):
        return ManagedServiceActivationResult(
            status=ManagedServiceActivationStatus.FAILED,
            detail="managed origin is invalid",
        )
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout_seconds) as raw:
            raw.settimeout(timeout_seconds)
            stream: socket.socket | ssl.SSLSocket = raw
            wrapped: ssl.SSLSocket | None = None
            if legacy_https:
                assert hostname is not None
                _validate_hostname(hostname)
                if root_cert_path is None:
                    raise FileNotFoundError("legacy HTTPS root is unavailable")
                context = ssl.create_default_context(cafile=os.fspath(root_cert_path))
                context.check_hostname = True
                context.verify_mode = ssl.CERT_REQUIRED
                wrapped = context.wrap_socket(raw, server_hostname=hostname)
                wrapped.settimeout(timeout_seconds)
                stream = wrapped
            try:
                stream.sendall(
                    (
                        "GET /health HTTP/1.1\r\n"
                        f"Host: {hostname}:{port}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                )
                response = HTTPResponse(stream)
                response.begin()
                payload = response.read(1024)
                if response.status == 200 and payload == b'{"status":"OK"}':
                    return ManagedServiceActivationResult(
                        status=ManagedServiceActivationStatus.READY,
                        saved_endpoint=origin,
                    )
                if response.status in {502, 503, 504}:
                    return ManagedServiceActivationResult(
                        status=ManagedServiceActivationStatus.PENDING,
                        detail="managed loopback gateway is up but backend health is not ready",
                    )
                return ManagedServiceActivationResult(
                    status=ManagedServiceActivationStatus.PORT_CONFLICT,
                    detail="loopback listener did not return the exact FactLane health proof",
                )
            finally:
                if wrapped is not None:
                    wrapped.close()
    except (FileNotFoundError, ValueError):
        return ManagedServiceActivationResult(
            status=ManagedServiceActivationStatus.FAILED,
            detail="managed endpoint trust material is unavailable or invalid",
        )
    except ssl.SSLError:
        return ManagedServiceActivationResult(
            status=ManagedServiceActivationStatus.PORT_CONFLICT,
            detail="loopback listener failed exact Caddy CA/SNI verification",
        )
    except (HTTPException, OSError, TimeoutError):
        return ManagedServiceActivationResult(
            status=ManagedServiceActivationStatus.PENDING,
            detail="managed endpoint is not ready",
        )


def _run_argv(argv: tuple[str, ...]) -> int:
    completed = subprocess.run(
        argv,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.returncode


def _run_argv_capture(argv: tuple[str, ...]) -> tuple[int, str]:
    completed = subprocess.run(
        argv,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return int(completed.returncode), completed.stdout


def _environment_blockers(environment: InstallEnvironment) -> tuple[str, ...]:
    reasons: list[str] = []
    if environment.system == "Windows":
        if os.name != "nt":
            reasons.append("native Windows setup is unavailable from this Linux host")
        return tuple(reasons)
    if environment.system != "Linux":
        reasons.append("Linux is the only supported WebUI service platform")
    if not environment.systemd:
        reasons.append("a running Linux systemd host with systemctl is required")
    if environment.caddy_path is None:
        reasons.append("Caddy is required for the installation-private loopback gateway")
    if environment.zenity_path is None:
        reasons.append("zenity is required for native setup confirmation")
    if environment.euid == 0:
        reasons.append("WebUI setup must run as the installing user, not root")
    return tuple(reasons)


def _validate_hostname(hostname: str) -> None:
    match = _HOSTNAME_RE.fullmatch(hostname) if isinstance(hostname, str) else None
    if match is None:
        raise ValueError("hostname must be exactly factlane-<installation-id>.test")
    webui_hostname(match.group(1))


def _validate_saved_port(port: int) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not WEBUI_PORT_MIN <= port <= WEBUI_PORT_MAX:
        raise ValueError(
            f"saved WebUI port must be an integer from {WEBUI_PORT_MIN} through {WEBUI_PORT_MAX}"
        )


def _validate_absolute_path(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise ValueError(f"{label} must be an absolute POSIX path")
    if any(ch in value for ch in ("\n", "\r")):
        raise ValueError(f"{label} must not contain line breaks")
    path = PurePosixPath(value)
    if ".." in path.parts:
        raise ValueError(f"{label} must not contain parent traversal")


def _validate_windows_absolute_path(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be an absolute Windows path")
    if any(ch in value for ch in ("\n", "\r")):
        raise ValueError(f"{label} must not contain line breaks")
    path = PureWindowsPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be an absolute Windows path")
    folded = value.replace("/", "\\").casefold()
    if folded.startswith("\\\\wsl") or folded.startswith("\\\\wsl.localhost"):
        raise ValueError(f"{label} must not use WSL paths")
    if value.startswith("/home/") or value.startswith("/mnt/"):
        raise ValueError(f"{label} must not use Linux paths")




def _caddy_token(value: str) -> str:
    if _SAFE_TOKEN_RE.fullmatch(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_token(value: str) -> str:
    if _SAFE_TOKEN_RE.fullmatch(value):
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'
