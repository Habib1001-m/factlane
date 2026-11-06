from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from enum import Enum
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol

from .webui_runtime import WebUIRuntimeConfig
from .webui_service import ServiceSnapshot, ServiceStatus


WEBUI_SERVICE_NAME = "factlane-webui.service"
WEBUI_PORT_MIN = 8765
WEBUI_PORT_MAX = 8774
OWNER_PRIVATE_ATOMIC_WRITE_CONTRACT = "OWNER_PRIVATE_ATOMIC_REGULAR_FILE_0600"

_INSTALLATION_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_HOSTNAME_RE = re.compile(r"factlane-([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.test")
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:@+,-]+")
_SUPPORTED_BROWSERS = (
    ("google-chrome", "chromium"),
    ("google-chrome-stable", "chromium"),
    ("chromium", "chromium"),
    ("chromium-browser", "chromium"),
    ("firefox", "firefox"),
)
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
    detail: str | None = None


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
class InstallEnvironment:
    system: str
    systemd: bool
    caddy_path: str | None
    zenity_path: str | None
    browser: BrowserInstall | None
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

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, bool)
            for value in (
                self.hostname_mapping,
                self.browser_profile_trust,
                self.user_lingering,
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

    def __post_init__(self) -> None:
        webui_hostname(self.installation_id)
        _validate_saved_port(self.saved_port)
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
            _validate_absolute_path(self.db, "database path")
            if not self.profile or any(
                ch.isspace() or ord(ch) < 32 for ch in self.profile
            ):
                raise ValueError("profile must be a non-empty identifier without whitespace")
            if not self.host_id or any(
                ch.isspace() or ord(ch) < 32 for ch in self.host_id
            ):
                raise ValueError("host_id must be a non-empty identifier without whitespace")
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
            _validate_absolute_path(value, label)
        canonical_config_home = str(PurePosixPath(self.owner_config_home))
        if canonical_config_home != self.owner_config_home:
            raise ValueError("Owner config home must be a canonical absolute POSIX path")
        expected_service_config = str(
            PurePosixPath(self.owner_config_home) / "factlane" / "webui.json"
        )
        if self.service_config_path != expected_service_config:
            raise ValueError(
                "service_config_path must equal the canonical runtime config path "
                "<owner_config_home>/factlane/webui.json"
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
            if self.write_contract != OWNER_PRIVATE_ATOMIC_WRITE_CONTRACT:
                raise ValueError(
                    "WRITE_SERVICE_CONFIG requires the fixed owner-private atomic 0600 contract"
                )
        elif self.write_contract is not None:
            raise ValueError("write_contract is only valid for WRITE_SERVICE_CONFIG")


@dataclass(frozen=True)
class SetupPlan:
    hostname: str
    origin: str
    saved_port: int
    browser: BrowserInstall
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
    requirements = [
        ConsentRequirement(
            key="hostname_mapping",
            explanation=(
                "Allow one exact local hostname mapping for this FactLane installation; "
                "all unrelated hostname mappings must be preserved."
            ),
        ),
        ConsentRequirement(
            key="browser_profile_trust",
            explanation=(
                "Trust this installation's public local issuer only inside an isolated "
                "FactLane browser profile; its protected issuer key can authorize local UI "
                "certificates, while normal browser profiles and other trust entries must "
                "be preserved."
            ),
        ),
    ]
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
        "    auto_https disable_redirects\n"
        "    skip_install_trust\n"
        "    servers {\n"
        "        protocols h1 h2\n"
        "        strict_sni_host on\n"
        "    }\n"
        f"    storage file_system {_caddy_token(caddy_state_dir)}\n"
        "}\n"
        "\n"
        f"https://{hostname}:{port} {{\n"
        "    bind 127.0.0.1\n"
        "    tls {\n"
        "        issuer internal\n"
        "    }\n"
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


def plan_webui_setup(
    request: SetupRequest,
    environment: InstallEnvironment,
    consent: SetupConsent,
) -> SetupReceipt:
    """Build a deterministic setup plan without applying any host mutation."""

    blocked_reasons = list(_environment_blockers(environment))
    if not consent.hostname_mapping:
        blocked_reasons.append("explicit consent for the exact hostname mapping is required")
    if not consent.browser_profile_trust:
        blocked_reasons.append("explicit consent for isolated browser profile trust is required")
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

    browser = environment.browser
    if browser is None:  # Defensive: the blocker above should make this unreachable.
        return SetupReceipt(
            status="BLOCKED",
            blocked_reasons=("a supported browser is required",),
            plan=None,
        )

    hostname = webui_hostname(request.installation_id)
    origin = f"https://{hostname}:{request.saved_port}"
    caddy_path = environment.caddy_path
    if caddy_path is None:  # Defensive: dependency blockers above make this unreachable.
        return SetupReceipt(
            status="BLOCKED",
            blocked_reasons=("Caddy is required for the installation-private HTTPS gateway",),
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
    runtime_config_json = json.dumps(
        asdict(runtime_config),
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
    public_root = str(
        PurePosixPath(request.caddy_state_dir)
        / "pki"
        / "authorities"
        / "local"
        / "root.crt"
    )

    operations: list[PlannedOperation] = [
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
        PlannedOperation(
            kind="ENSURE_HOSTNAME_MAPPING",
            target="/etc/hosts",
            desired_state=f"127.0.0.1\t{hostname}",
            consent_key="hostname_mapping",
        ),
        PlannedOperation(
            kind="ENSURE_ISOLATED_PROFILE_TRUST",
            target=request.browser_profile_dir,
            desired_state=(
                f"Import only public root certificate {public_root} into this isolated "
                f"{browser.family} profile; leave every other profile and trust entry untouched."
            ),
            consent_key="browser_profile_trust",
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
    try:
        crossing_kinds = {
            "ENSURE_HOSTNAME_MAPPING",
            "ENSURE_ISOLATED_PROFILE_TRUST",
            "ENABLE_USER_LINGERING",
        }
        endpoint_writes = {"WRITE_SERVICE_CONFIG", "WRITE_CADDY_CONFIG"}
        ordered_prerequisites = (
            [op for op in plan.operations if op.kind in crossing_kinds]
            + [
                op
                for op in plan.operations
                if op.kind not in crossing_kinds
                and op.kind not in endpoint_writes
            ]
        )
        for operation in ordered_prerequisites:
            result = backend.apply_operation(operation, plan=plan)
            if result.status is not InstallerStepStatus.APPLIED:
                return SetupApplyResult(
                    status=result.status,
                    endpoint_state=EndpointState.PROPOSED,
                    completed_operations=tuple(completed),
                    detail=result.detail,
                )
            completed.append(operation.kind)
        saved_endpoint = backend.read_saved_endpoint()
        if saved_endpoint is not None:
            try:
                saved_port = _saved_endpoint_port_for_plan(saved_endpoint, plan)
            except ValueError as exc:
                return SetupApplyResult(
                    status=InstallerStepStatus.RECOVERY_REQUIRED,
                    endpoint_state=EndpointState.PROPOSED,
                    completed_operations=tuple(completed),
                    detail=str(exc),
                )
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
                    return SetupApplyResult(
                        status=written.status,
                        endpoint_state=EndpointState.PROPOSED,
                        completed_operations=tuple(completed),
                        detail=written.detail,
                    )
                completed.append(operation.kind)

            activation = backend.activate_candidate(plan=candidate_plan)
            if activation.status is ManagedServiceActivationStatus.READY:
                if activation.saved_endpoint != candidate_plan.origin:
                    stopped = backend.stop_candidate(plan=candidate_plan)
                    detail = "managed service reported READY without the exact claimed candidate endpoint"
                    if stopped.status is not InstallerStepStatus.APPLIED:
                        detail += "; managed service stop also failed"
                    return SetupApplyResult(
                        status=InstallerStepStatus.FAILED,
                        endpoint_state=EndpointState.PROPOSED,
                        completed_operations=tuple(completed),
                        detail=detail,
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
                return SetupApplyResult(
                    status=InstallerStepStatus.FAILED,
                    endpoint_state=EndpointState.PROPOSED,
                    completed_operations=tuple(completed),
                    detail="candidate activation failed and managed service could not be stopped safely",
                )
            if activation.status is ManagedServiceActivationStatus.PORT_CONFLICT:
                if saved_endpoint is not None:
                    return SetupApplyResult(
                        status=InstallerStepStatus.RECOVERY_REQUIRED,
                        endpoint_state=EndpointState.PROPOSED,
                        completed_operations=tuple(completed),
                        detail=(
                            "saved endpoint is unavailable; explicit endpoint recovery is required"
                        ),
                    )
                continue
            return SetupApplyResult(
                status=InstallerStepStatus.FAILED,
                endpoint_state=EndpointState.PROPOSED,
                completed_operations=tuple(completed),
                detail=activation.detail or "managed service candidate activation failed",
            )

        return SetupApplyResult(
            status=InstallerStepStatus.FAILED,
            endpoint_state=EndpointState.PROPOSED,
            completed_operations=tuple(completed),
            detail=f"no managed endpoint from {WEBUI_PORT_MIN} through {WEBUI_PORT_MAX} could be activated",
        )
    except Exception as exc:
        return SetupApplyResult(
            status=InstallerStepStatus.FAILED,
            endpoint_state=EndpointState.PROPOSED,
            completed_operations=tuple(completed),
            detail=f"setup apply failed safely: {type(exc).__name__}",
        )


CommandRunner = Callable[[tuple[str, ...]], int]
ServiceStateReader = Callable[[], ServiceSnapshot]
ActivationProbe = Callable[[str], ManagedServiceActivationResult]
InstallClock = Callable[[], float]
InstallSleep = Callable[[float], None]


class ProductionLinuxInstallerBackend:
    """Linux installer backend with explicit argv-only owner-side operations.

    This backend intentionally does not invent privilege escalation or browser trust
    mechanisms. Those crossings return typed incomplete results until a supported
    privileged installer and browser-specific trust implementation are supplied.
    """

    def __init__(
        self,
        *,
        systemctl_path: str,
        command_runner: CommandRunner | None = None,
        service_state_reader: ServiceStateReader | None = None,
        activation_probe: ActivationProbe | None = None,
        clock: InstallClock = time.monotonic,
        sleep: InstallSleep = time.sleep,
        activation_timeout: float = 10.0,
        poll_interval: float = 0.1,
    ) -> None:
        _validate_absolute_path(systemctl_path, "systemctl executable")
        if activation_timeout <= 0:
            raise ValueError("activation_timeout must be positive")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._systemctl_path = systemctl_path
        self._command_runner = command_runner or _run_argv
        self._service_state_reader = service_state_reader
        self._activation_probe = activation_probe
        self._clock = clock
        self._sleep = sleep
        self._activation_timeout = activation_timeout
        self._poll_interval = poll_interval

    def apply_operation(
        self,
        operation: PlannedOperation,
        *,
        plan: SetupPlan,
    ) -> InstallerStepResult:
        del plan
        if operation.kind in {
            "WRITE_SERVICE_CONFIG",
            "WRITE_CADDY_CONFIG",
            "INSTALL_USER_UNIT",
        }:
            try:
                _atomic_owner_file_write(
                    Path(operation.target),
                    operation.desired_state,
                    private_parent=operation.kind != "INSTALL_USER_UNIT",
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

        if operation.kind == "ENSURE_HOSTNAME_MAPPING":
            return InstallerStepResult(
                status=InstallerStepStatus.NEEDS_PRIVILEGED_INSTALLER,
                operation=operation.kind,
                detail=(
                    "exact /etc/hosts mutation needs a separately authorized privileged "
                    "installer; no sudo/pkexec path is assumed"
                ),
            )
        if operation.kind == "ENABLE_USER_LINGERING":
            return InstallerStepResult(
                status=InstallerStepStatus.NEEDS_PRIVILEGED_INSTALLER,
                operation=operation.kind,
                detail=(
                    "lingering change needs a separately authorized privileged installer; "
                    "no elevation mechanism is assumed"
                ),
            )
        if operation.kind == "ENSURE_ISOLATED_PROFILE_TRUST":
            return InstallerStepResult(
                status=InstallerStepStatus.INCOMPLETE_BROWSER_TRUST,
                operation=operation.kind,
                detail=(
                    "browser-specific isolated-profile trust mechanism is not proven by "
                    "this backend"
                ),
            )
        if operation.kind == "REGISTER_USER_SERVICE":
            if operation.target != WEBUI_SERVICE_NAME:
                return InstallerStepResult(
                    status=InstallerStepStatus.FAILED,
                    operation=operation.kind,
                    detail="user service target is not the exact FactLane service",
                )
            commands = (
                (self._systemctl_path, "--user", "daemon-reload"),
                (
                    self._systemctl_path,
                    "--user",
                    "enable",
                    WEBUI_SERVICE_NAME,
                ),
            )
            for argv in commands:
                if self._command_runner(argv) != 0:
                    return InstallerStepResult(
                        status=InstallerStepStatus.FAILED,
                        operation=operation.kind,
                        detail="exact user-service registration command failed",
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

    def activate_candidate(self, *, plan: SetupPlan) -> ManagedServiceActivationResult:
        start_argv = (
            self._systemctl_path,
            "--user",
            "start",
            WEBUI_SERVICE_NAME,
        )
        start_code = self._command_runner(start_argv)
        if start_code != 0:
            probed = self._probe_activation(plan.origin)
            if probed is not None and probed.status is ManagedServiceActivationStatus.PORT_CONFLICT:
                return probed
            return ManagedServiceActivationResult(
                status=ManagedServiceActivationStatus.FAILED,
                detail="managed service start command failed",
            )
        if self._service_state_reader is None:
            return ManagedServiceActivationResult(
                status=ManagedServiceActivationStatus.FAILED,
                detail="owner-private service-state reader is required for activation proof",
            )

        deadline = self._clock() + self._activation_timeout
        while True:
            probed = self._probe_activation(plan.origin)
            if probed is not None and probed.status in {
                ManagedServiceActivationStatus.PORT_CONFLICT,
                ManagedServiceActivationStatus.FAILED,
            }:
                return probed

            snapshot = self._service_state_reader()
            if snapshot.saved_endpoint is not None and snapshot.saved_endpoint != plan.origin:
                return ManagedServiceActivationResult(
                    status=ManagedServiceActivationStatus.FAILED,
                    saved_endpoint=snapshot.saved_endpoint,
                    detail="service state saved endpoint differs from the candidate origin",
                )
            if snapshot.status is ServiceStatus.READY and snapshot.saved_endpoint == plan.origin:
                if (
                    probed is not None
                    and probed.status is not ManagedServiceActivationStatus.READY
                ):
                    now = self._clock()
                    if now >= deadline:
                        return ManagedServiceActivationResult(
                            status=ManagedServiceActivationStatus.FAILED,
                            saved_endpoint=snapshot.saved_endpoint,
                            detail="managed service endpoint claimed but activation probe never confirmed readiness",
                        )
                    self._sleep(min(self._poll_interval, deadline - now))
                    continue
                return ManagedServiceActivationResult(
                    status=ManagedServiceActivationStatus.READY,
                    saved_endpoint=snapshot.saved_endpoint,
                )
            if snapshot.status is ServiceStatus.FAILED:
                return ManagedServiceActivationResult(
                    status=ManagedServiceActivationStatus.FAILED,
                    saved_endpoint=snapshot.saved_endpoint,
                    detail="managed service entered FAILED before candidate readiness",
                )
            now = self._clock()
            if now >= deadline:
                return ManagedServiceActivationResult(
                    status=ManagedServiceActivationStatus.FAILED,
                    saved_endpoint=snapshot.saved_endpoint,
                    detail="managed service activation readiness timed out",
                )
            self._sleep(min(self._poll_interval, deadline - now))

    def stop_candidate(self, *, plan: SetupPlan) -> InstallerStepResult:
        del plan
        argv = (
            self._systemctl_path,
            "--user",
            "stop",
            WEBUI_SERVICE_NAME,
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

    def _probe_activation(self, origin: str) -> ManagedServiceActivationResult | None:
        if self._activation_probe is None:
            return None
        return self._activation_probe(origin)


def render_hosts_with_mapping(
    existing: str,
    *,
    hostname: str,
    address: str = "127.0.0.1",
) -> str:
    """Return hosts text with one exact mapping while preserving unrelated entries."""

    _validate_hostname(hostname)
    if address != "127.0.0.1":
        raise ValueError("FactLane WebUI hostname mapping must target 127.0.0.1")
    output: list[str] = []
    for raw_line in existing.splitlines():
        content, marker, comment = raw_line.partition("#")
        tokens = content.split()
        if len(tokens) >= 2 and hostname in tokens[1:]:
            aliases = [token for token in tokens[1:] if token != hostname]
            if aliases:
                rebuilt = " ".join((tokens[0], *aliases))
                if marker:
                    rebuilt += f" #{comment}"
                output.append(rebuilt)
            elif marker and comment:
                output.append(f"#{comment}")
            continue
        output.append(raw_line)
    output.append(f"{address}\t{hostname}")
    return "\n".join(output) + "\n"


def _rebind_plan_endpoint(
    plan: SetupPlan,
    port: int,
    state: EndpointState,
) -> SetupPlan:
    _validate_saved_port(port)
    origin = f"https://{plan.hostname}:{port}"
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
    if (
        parsed.scheme != "https"
        or parsed.hostname != plan.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port is None
        or not WEBUI_PORT_MIN <= port <= WEBUI_PORT_MAX
        or saved_endpoint != f"https://{plan.hostname}:{port}"
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
    return False


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


def _environment_blockers(environment: InstallEnvironment) -> tuple[str, ...]:
    reasons: list[str] = []
    if environment.system != "Linux":
        reasons.append("Linux is the only supported WebUI service platform")
    if not environment.systemd:
        reasons.append("a running Linux systemd host with systemctl is required")
    if environment.caddy_path is None:
        reasons.append("Caddy is required for the installation-private HTTPS gateway")
    if environment.zenity_path is None:
        reasons.append("zenity is required for native setup confirmation")
    if environment.browser is None:
        reasons.append("a supported Chromium or Firefox browser is required")
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
