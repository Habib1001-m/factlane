from __future__ import annotations

import json
import socket
import sys
from dataclasses import replace
from pathlib import Path

import pytest

_LINUX_OWNER_PRIVATE_WRITE = pytest.mark.skipif(
    sys.platform != "linux",
    reason="production managed-WebUI owner-private file writes are Linux-only",
)

from factlane.webui_service import ServiceSnapshot, ServiceStatus

from factlane.webui_install import (
    BrowserInstall,
    EndpointState,
    InstallEnvironment,
    ManagedServiceActivationResult,
    ManagedServiceActivationStatus,
    InstallerStepResult,
    InstallerStepStatus,
    PortClaim,
    ProductionLinuxInstallerBackend,
    SetupConsent,
    SetupRequest,
    WEBUI_SERVICE_NAME,
    apply_webui_setup,
    consent_requirements,
    detect_install_environment,
    render_hosts_with_mapping,
    plan_webui_setup,
    render_caddy_config,
    render_user_unit,
    webui_hostname,
)


class _FakeInstallerBackend:
    def __init__(
        self,
        *,
        step_statuses: dict[str, InstallerStepStatus] | None = None,
        activation_by_port: dict[int, ManagedServiceActivationStatus] | None = None,
        saved_endpoint: str | None = None,
    ) -> None:
        self.step_statuses = step_statuses or {}
        self.activation_by_port = activation_by_port or {}
        self.operations = []
        self.activations = []
        self.stops = []
        self.saved_endpoint = saved_endpoint

    def apply_operation(self, operation, *, plan):
        self.operations.append((operation, plan))
        status = self.step_statuses.get(operation.kind, InstallerStepStatus.APPLIED)
        return InstallerStepResult(status=status, operation=operation.kind)

    def read_saved_endpoint(self):
        return self.saved_endpoint

    def activate_candidate(self, *, plan):
        self.activations.append(plan)
        status = self.activation_by_port.get(
            plan.saved_port,
            ManagedServiceActivationStatus.READY,
        )
        if status is ManagedServiceActivationStatus.READY:
            self.saved_endpoint = plan.origin
            return ManagedServiceActivationResult(
                status=status,
                saved_endpoint=plan.origin,
            )
        return ManagedServiceActivationResult(status=status)

    def stop_candidate(self, *, plan):
        self.stops.append(plan)
        return InstallerStepResult(
            status=InstallerStepStatus.APPLIED,
            operation="STOP_MANAGED_SERVICE",
        )


class _InstallClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _service_snapshot(
    status: ServiceStatus,
    saved_endpoint: str | None,
) -> ServiceSnapshot:
    return ServiceSnapshot(
        status=status,
        saved_endpoint=saved_endpoint,
        stopped_boot_id=None,
        intent_revision=0,
        crash_attempts=0,
        next_retry_at=None,
        ready_since=0.0 if status is ServiceStatus.READY else None,
        maintenance_operation_id=None,
    )


def _environment() -> InstallEnvironment:
    return InstallEnvironment(
        system="Linux",
        systemd=True,
        caddy_path="/usr/bin/caddy",
        zenity_path="/usr/bin/zenity",
        browser=BrowserInstall(family="chromium", executable="/usr/bin/google-chrome"),
        euid=1000,
    )


def _request(
    *,
    lingering_required: bool = True,
    memory_configured: bool = True,
) -> SetupRequest:
    return SetupRequest(
        installation_id="1a2b3c4d-55aa",
        saved_port=8767,
        lingering_required=lingering_required,
        installing_user="alice",
        db="/home/alice/.local/share/factlane/memories.db" if memory_configured else None,
        profile="nomic-768" if memory_configured else None,
        host_id="owner-host-a" if memory_configured else None,
        factlane_executable="/home/alice/.local/bin/factlane",
        owner_config_home="/home/alice/.config",
        service_config_path="/home/alice/.config/factlane/webui.json",
        caddy_config_path="/home/alice/.config/factlane/Caddyfile",
        user_unit_path="/home/alice/.config/systemd/user/factlane-webui.service",
        backend_socket="/run/user/1000/factlane/webui.sock",
        runtime_dir="/run/user/1000/factlane",
        state_dir="/home/alice/.local/state/factlane",
        caddy_state_dir="/home/alice/.local/state/factlane/caddy",
        browser_profile_dir="/home/alice/.local/state/factlane/browser",
    )


def _consent(*, lingering: bool = True) -> SetupConsent:
    return SetupConsent(
        hostname_mapping=True,
        browser_profile_trust=True,
        user_lingering=lingering,
    )


def test_detects_linux_systemd_caddy_zenity_and_supported_browser() -> None:
    executables = {
        "systemctl": "/usr/bin/systemctl",
        "caddy": "/usr/bin/caddy",
        "zenity": "/usr/bin/zenity",
        "google-chrome": "/opt/google/chrome/google-chrome",
    }

    environment = detect_install_environment(
        system_name=lambda: "Linux",
        path_exists=lambda path: path == Path("/run/systemd/system"),
        which=lambda name: executables.get(name),
        geteuid=lambda: 1000,
    )

    assert environment == InstallEnvironment(
        system="Linux",
        systemd=True,
        caddy_path="/usr/bin/caddy",
        zenity_path="/usr/bin/zenity",
        browser=BrowserInstall(
            family="chromium",
            executable="/opt/google/chrome/google-chrome",
        ),
        euid=1000,
    )


def test_non_linux_detection_returns_unsupported_facts_without_posix_uid_support() -> None:
    environment = detect_install_environment(
        system_name=lambda: "Windows",
        path_exists=lambda _path: False,
        which=lambda _name: None,
        geteuid=lambda: -1,
    )

    receipt = plan_webui_setup(_request(), environment, _consent())

    assert environment.systemd is False
    assert receipt.status == "BLOCKED"
    assert receipt.plan is None
    assert any("Linux" in reason for reason in receipt.blocked_reasons)


@pytest.mark.parametrize(
    ("missing", "expected_reason"),
    [
        ("systemd", "systemd"),
        ("caddy", "Caddy"),
        ("zenity", "zenity"),
        ("browser", "browser"),
    ],
)
def test_setup_fails_closed_when_required_dependency_is_missing(
    missing: str,
    expected_reason: str,
) -> None:
    values = {
        "systemd": True,
        "caddy_path": "/usr/bin/caddy",
        "zenity_path": "/usr/bin/zenity",
        "browser": BrowserInstall(family="firefox", executable="/usr/bin/firefox"),
    }
    if missing == "systemd":
        values["systemd"] = False
    elif missing == "caddy":
        values["caddy_path"] = None
    elif missing == "zenity":
        values["zenity_path"] = None
    else:
        values["browser"] = None

    receipt = plan_webui_setup(
        _request(),
        InstallEnvironment(system="Linux", euid=1000, **values),
        _consent(),
    )

    assert receipt.status == "BLOCKED"
    assert receipt.plan is None
    assert receipt.operations == ()
    assert any(expected_reason in reason for reason in receipt.blocked_reasons)


def test_setup_fails_closed_when_run_as_root() -> None:
    environment = InstallEnvironment(
        system="Linux",
        systemd=True,
        caddy_path="/usr/bin/caddy",
        zenity_path="/usr/bin/zenity",
        browser=BrowserInstall(family="firefox", executable="/usr/bin/firefox"),
        euid=0,
    )

    receipt = plan_webui_setup(_request(), environment, _consent())

    assert receipt.status == "BLOCKED"
    assert receipt.plan is None
    assert any("root" in reason for reason in receipt.blocked_reasons)


def test_consent_values_must_be_explicit_booleans() -> None:
    with pytest.raises(ValueError, match="explicit booleans"):
        SetupConsent(
            hostname_mapping=1,  # type: ignore[arg-type]
            browser_profile_trust=True,
            user_lingering=True,
        )


@pytest.mark.parametrize(
    ("consent", "expected_reason"),
    [
        (
            SetupConsent(
                hostname_mapping=False,
                browser_profile_trust=True,
                user_lingering=True,
            ),
            "hostname",
        ),
        (
            SetupConsent(
                hostname_mapping=True,
                browser_profile_trust=False,
                user_lingering=True,
            ),
            "browser",
        ),
        (
            SetupConsent(
                hostname_mapping=True,
                browser_profile_trust=True,
                user_lingering=False,
            ),
            "lingering",
        ),
    ],
)
def test_setup_fails_closed_without_every_required_consent(
    consent: SetupConsent,
    expected_reason: str,
) -> None:
    receipt = plan_webui_setup(_request(), _environment(), consent)

    assert receipt.status == "BLOCKED"
    assert receipt.plan is None
    assert receipt.operations == ()
    assert any(expected_reason in reason.lower() for reason in receipt.blocked_reasons)


def test_lingering_consent_is_not_required_when_existing_state_needs_no_change() -> None:
    receipt = plan_webui_setup(
        _request(lingering_required=False),
        _environment(),
        SetupConsent(
            hostname_mapping=True,
            browser_profile_trust=True,
            user_lingering=False,
        ),
    )

    assert receipt.status == "READY_TO_APPLY"
    assert receipt.plan is not None
    assert "ENABLE_USER_LINGERING" not in {operation.kind for operation in receipt.operations}


def test_consent_explanations_name_the_exact_state_changes() -> None:
    requirements = {item.key: item for item in consent_requirements(lingering_required=True)}

    assert set(requirements) == {
        "hostname_mapping",
        "browser_profile_trust",
        "user_lingering",
    }
    assert "exact local hostname" in requirements["hostname_mapping"].explanation
    assert "isolated FactLane browser profile" in requirements["browser_profile_trust"].explanation
    assert "boot before login" in requirements["user_lingering"].explanation


def test_hostname_and_caddy_config_are_exact_and_have_no_http_or_http3_listener() -> None:
    hostname = webui_hostname("1a2b3c4d-55aa")
    assert hostname == "factlane-1a2b3c4d-55aa.test"

    config = render_caddy_config(
        hostname=hostname,
        port=8767,
        backend_socket="/run/user/1000/factlane/webui.sock",
        caddy_state_dir="/home/alice/.local/state/factlane/caddy",
    )

    assert "admin off" in config
    assert "auto_https disable_redirects" in config
    assert "skip_install_trust" in config
    assert "protocols h1 h2" in config
    assert "strict_sni_host on" in config
    assert "storage file_system /home/alice/.local/state/factlane/caddy" in config
    assert "issuer internal" in config
    assert "https://factlane-1a2b3c4d-55aa.test:8767" in config
    assert "bind 127.0.0.1" in config
    assert "unix//run/user/1000/factlane/webui.sock" in config
    assert ":80" not in config
    assert "http://" not in config
    assert "h3" not in config
    assert "acme" not in config.lower()


@pytest.mark.parametrize(
    "installation_id",
    ["", "UPPER", ".bad", "bad.", "bad_name", "bad/name", "bad\nname", "-bad", "bad-"],
)
def test_hostname_rejects_non_label_installation_ids(installation_id: str) -> None:
    with pytest.raises(ValueError):
        webui_hostname(installation_id)


@pytest.mark.parametrize("port", [0, 8764, 8775, 65535, True])
def test_caddy_config_accepts_only_saved_webui_port_range(port: int) -> None:
    with pytest.raises(ValueError):
        render_caddy_config(
            hostname="factlane-1a2b3c4d.test",
            port=port,
            backend_socket="/run/user/1000/factlane/webui.sock",
            caddy_state_dir="/home/alice/.local/state/factlane/caddy",
        )


def test_packaged_user_unit_runs_the_factlane_service_as_the_user() -> None:
    rendered = render_user_unit(
        factlane_executable="/home/alice/.local/bin/factlane",
        service_config_path="/home/alice/.config/factlane/webui.json",
    )

    assert "ExecStart=/home/alice/.local/bin/factlane serve webui\n" in rendered
    assert "--config" not in rendered
    assert "/home/alice/.config/factlane/webui.json" not in rendered
    assert "User=root" not in rendered
    assert "sudo" not in rendered
    assert "Restart=no" in rendered
    assert "WantedBy=default.target" in rendered
    assert "@FACTLANE_" not in rendered


def test_setup_request_requires_service_config_at_the_explicit_canonical_config_home() -> None:
    with pytest.raises(ValueError, match="canonical runtime config path"):
        replace(
            _request(),
            service_config_path="/home/alice/custom/webui.json",
        )


def test_setup_plan_writes_exact_runtime_config_with_fixed_owner_private_semantics() -> None:
    request = _request()
    receipt = plan_webui_setup(request, _environment(), _consent())

    assert receipt.plan is not None
    operations = {operation.kind: operation for operation in receipt.operations}
    config_write = operations["WRITE_SERVICE_CONFIG"]
    assert config_write.target == request.service_config_path
    assert config_write.write_contract == "OWNER_PRIVATE_ATOMIC_REGULAR_FILE_0600"
    assert json.loads(config_write.desired_state) == {
        "backend_socket": request.backend_socket,
        "caddy_path": "/usr/bin/caddy",
        "caddyfile": request.caddy_config_path,
        "db": request.db,
        "host_id": request.host_id,
        "origin": "https://factlane-1a2b3c4d-55aa.test:8767",
        "profile": request.profile,
        "runtime_dir": request.runtime_dir,
        "state_dir": request.state_dir,
    }
    assert config_write.desired_state == (
        '{"backend_socket":"/run/user/1000/factlane/webui.sock",'
        '"caddy_path":"/usr/bin/caddy",'
        '"caddyfile":"/home/alice/.config/factlane/Caddyfile",'
        '"db":"/home/alice/.local/share/factlane/memories.db",'
        '"host_id":"owner-host-a",'
        '"origin":"https://factlane-1a2b3c4d-55aa.test:8767",'
        '"profile":"nomic-768",'
        '"runtime_dir":"/run/user/1000/factlane",'
        '"state_dir":"/home/alice/.local/state/factlane"}\n'
    )


def test_initial_setup_emits_transport_config_with_null_memory_selection_trio() -> None:
    request = _request(memory_configured=False)
    receipt = plan_webui_setup(request, _environment(), _consent())

    assert receipt.plan is not None
    config_write = next(
        operation
        for operation in receipt.operations
        if operation.kind == "WRITE_SERVICE_CONFIG"
    )
    payload = json.loads(config_write.desired_state)
    assert payload["db"] is None
    assert payload["profile"] is None
    assert payload["host_id"] is None
    assert payload["origin"] == "https://factlane-1a2b3c4d-55aa.test:8767"
    assert payload["backend_socket"] == request.backend_socket
    assert config_write.write_contract == "OWNER_PRIVATE_ATOMIC_REGULAR_FILE_0600"


@pytest.mark.parametrize(
    "changes",
    [
        {"db": None},
        {"profile": None},
        {"host_id": None},
        {"db": None, "profile": None},
        {"db": None, "host_id": None},
        {"profile": None, "host_id": None},
    ],
)
def test_setup_request_rejects_partial_memory_selection_trio(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="all configured or all absent"):
        replace(_request(), **changes)


def test_setup_request_rejects_non_string_configured_memory_identity() -> None:
    with pytest.raises(ValueError, match="strings when configured"):
        replace(_request(), profile=123)  # type: ignore[arg-type]


def test_successful_setup_returns_explicit_preserving_operations_and_generated_material() -> None:
    receipt = plan_webui_setup(_request(), _environment(), _consent())

    assert receipt.status == "READY_TO_APPLY"
    assert receipt.blocked_reasons == ()
    assert receipt.plan is not None
    plan = receipt.plan
    assert plan.hostname == "factlane-1a2b3c4d-55aa.test"
    assert plan.origin == "https://factlane-1a2b3c4d-55aa.test:8767"
    assert "issuer internal" in plan.caddy_config
    assert "serve webui" in plan.user_unit

    operations = {operation.kind: operation for operation in receipt.operations}
    assert set(operations) == {
        "WRITE_SERVICE_CONFIG",
        "WRITE_CADDY_CONFIG",
        "INSTALL_USER_UNIT",
        "ENSURE_HOSTNAME_MAPPING",
        "ENSURE_ISOLATED_PROFILE_TRUST",
        "ENABLE_USER_LINGERING",
        "REGISTER_USER_SERVICE",
    }
    hostname = operations["ENSURE_HOSTNAME_MAPPING"]
    assert hostname.target == "/etc/hosts"
    assert hostname.desired_state == "127.0.0.1\tfactlane-1a2b3c4d-55aa.test"
    assert hostname.preserve_unrelated_state is True
    assert hostname.consent_key == "hostname_mapping"

    trust = operations["ENSURE_ISOLATED_PROFILE_TRUST"]
    assert trust.target == "/home/alice/.local/state/factlane/browser"
    assert trust.preserve_unrelated_state is True
    assert trust.consent_key == "browser_profile_trust"
    assert "public root" in trust.desired_state
    assert "system" not in trust.desired_state.lower()

    lingering = operations["ENABLE_USER_LINGERING"]
    assert lingering.target == "alice"
    assert lingering.consent_key == "user_lingering"
    assert lingering.preserve_unrelated_state is True


def test_plan_is_pure_and_exposes_no_apply_or_shell_commands() -> None:
    receipt = plan_webui_setup(_request(), _environment(), _consent())

    assert receipt.plan is not None
    assert not hasattr(receipt.plan, "apply")
    for operation in receipt.operations:
        assert not hasattr(operation, "run")
        assert not hasattr(operation, "command")


def test_plan_marks_endpoint_as_proposed_not_claimed() -> None:
    receipt = plan_webui_setup(_request(), _environment(), _consent())

    assert receipt.plan is not None
    assert receipt.plan.endpoint_state is EndpointState.PROPOSED
    assert receipt.plan.saved_port == 8767


def test_port_claim_holds_loopback_socket_against_racing_binder() -> None:
    claim = PortClaim.acquire()
    attacker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        attacker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        with pytest.raises(OSError):
            attacker.bind(("127.0.0.1", claim.port))
        assert claim.active is True
    finally:
        attacker.close()
        claim.close()


def test_port_claim_tries_candidates_in_order_without_check_then_release() -> None:
    first = PortClaim.acquire()
    try:
        second = PortClaim.acquire()
        try:
            assert second.port > first.port
            challenger = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with pytest.raises(OSError):
                    challenger.bind(("127.0.0.1", second.port))
            finally:
                challenger.close()
        finally:
            second.close()
    finally:
        first.close()


def test_first_install_iterates_candidates_and_claims_only_after_managed_service_ready() -> None:
    backend = _FakeInstallerBackend(
        activation_by_port={
            8765: ManagedServiceActivationStatus.PORT_CONFLICT,
            8766: ManagedServiceActivationStatus.READY,
        }
    )
    receipt = plan_webui_setup(
        replace(_request(), saved_port=8771),
        _environment(),
        _consent(),
    )

    result = apply_webui_setup(receipt, _consent(), backend)

    assert result.status is InstallerStepStatus.APPLIED
    assert result.endpoint_state is EndpointState.CLAIMED
    assert result.claimed_port == 8766
    assert result.origin == "https://factlane-1a2b3c4d-55aa.test:8766"
    assert [plan.saved_port for plan in backend.activations] == [8765, 8766]
    assert [plan.saved_port for plan in backend.stops] == [8765]
    candidate_writes = [
        (operation.kind, json.loads(operation.desired_state).get("origin") if operation.kind == "WRITE_SERVICE_CONFIG" else plan.origin)
        for operation, plan in backend.operations
        if operation.kind in {"WRITE_CADDY_CONFIG", "WRITE_SERVICE_CONFIG"}
    ]
    assert candidate_writes == [
        ("WRITE_CADDY_CONFIG", "https://factlane-1a2b3c4d-55aa.test:8765"),
        ("WRITE_SERVICE_CONFIG", "https://factlane-1a2b3c4d-55aa.test:8765"),
        ("WRITE_CADDY_CONFIG", "https://factlane-1a2b3c4d-55aa.test:8766"),
        ("WRITE_SERVICE_CONFIG", "https://factlane-1a2b3c4d-55aa.test:8766"),
    ]


def test_unknown_candidate_activation_failure_stops_once_and_never_skips_to_next_port() -> None:
    backend = _FakeInstallerBackend(
        activation_by_port={8765: ManagedServiceActivationStatus.FAILED}
    )
    receipt = plan_webui_setup(_request(), _environment(), _consent())

    result = apply_webui_setup(receipt, _consent(), backend)

    assert result.status is InstallerStepStatus.FAILED
    assert result.endpoint_state is EndpointState.PROPOSED
    assert [plan.saved_port for plan in backend.activations] == [8765]
    assert [plan.saved_port for plan in backend.stops] == [8765]


def test_existing_saved_endpoint_gets_exactly_one_attempt_and_conflict_requires_recovery() -> None:
    saved = "https://factlane-1a2b3c4d-55aa.test:8769"
    backend = _FakeInstallerBackend(
        saved_endpoint=saved,
        activation_by_port={8769: ManagedServiceActivationStatus.PORT_CONFLICT},
    )
    receipt = plan_webui_setup(_request(), _environment(), _consent())

    result = apply_webui_setup(receipt, _consent(), backend)

    assert result.status is InstallerStepStatus.RECOVERY_REQUIRED
    assert result.endpoint_state is EndpointState.PROPOSED
    assert [plan.saved_port for plan in backend.activations] == [8769]
    assert [plan.saved_port for plan in backend.stops] == [8769]


def test_apply_refuses_non_ready_receipt_and_missing_operation_consent() -> None:
    blocked = plan_webui_setup(
        _request(),
        _environment(),
        SetupConsent(
            hostname_mapping=False,
            browser_profile_trust=True,
            user_lingering=True,
        ),
    )
    backend = _FakeInstallerBackend()
    blocked_result = apply_webui_setup(blocked, _consent(), backend)
    assert blocked_result.status is InstallerStepStatus.BLOCKED
    assert backend.operations == []

    ready = plan_webui_setup(_request(), _environment(), _consent())
    denied = apply_webui_setup(
        ready,
        SetupConsent(
            hostname_mapping=False,
            browser_profile_trust=True,
            user_lingering=True,
        ),
        backend,
    )
    assert denied.status is InstallerStepStatus.BLOCKED
    assert backend.operations == []


def test_apply_propagates_typed_privileged_and_browser_trust_incomplete_results() -> None:
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    privileged = _FakeInstallerBackend(
        step_statuses={
            "ENSURE_HOSTNAME_MAPPING": InstallerStepStatus.NEEDS_PRIVILEGED_INSTALLER,
        }
    )
    result = apply_webui_setup(receipt, _consent(), privileged)
    assert result.status is InstallerStepStatus.NEEDS_PRIVILEGED_INSTALLER
    assert result.endpoint_state is EndpointState.PROPOSED
    assert result.claimed_port is None
    assert all(op.kind != "WRITE_SERVICE_CONFIG" for op, _plan in privileged.operations)

    incomplete = _FakeInstallerBackend(
        step_statuses={
            "ENSURE_ISOLATED_PROFILE_TRUST": InstallerStepStatus.INCOMPLETE_BROWSER_TRUST,
        }
    )
    result = apply_webui_setup(receipt, _consent(), incomplete)
    assert result.status is InstallerStepStatus.INCOMPLETE_BROWSER_TRUST
    assert result.endpoint_state is EndpointState.PROPOSED
    assert result.claimed_port is None
    assert all(op.kind != "WRITE_SERVICE_CONFIG" for op, _plan in incomplete.operations)


@_LINUX_OWNER_PRIVATE_WRITE
def test_production_backend_owner_private_write_is_atomic_regular_0600_under_0700_parent(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        command_runner=lambda argv: calls.append(argv) or 0,
    )
    target = tmp_path / "private" / "webui.json"
    operation = replace(
        plan_webui_setup(_request(), _environment(), _consent()).operations[0],
        target=str(target),
    )
    plan = plan_webui_setup(_request(), _environment(), _consent()).plan
    assert plan is not None

    result = backend.apply_operation(operation, plan=plan)

    assert result.status is InstallerStepStatus.APPLIED
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == operation.desired_state
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert calls == []


def test_production_backend_rejects_symlink_target_without_touching_referent(
    tmp_path: Path,
) -> None:
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        command_runner=lambda _argv: 0,
    )
    referent = tmp_path / "referent.json"
    referent.write_text("preserve-me", encoding="utf-8")
    target = tmp_path / "private" / "webui.json"
    target.parent.mkdir()
    target.symlink_to(referent)
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    assert receipt.plan is not None
    operation = replace(receipt.operations[0], target=str(target))

    result = backend.apply_operation(operation, plan=receipt.plan)

    assert result.status is InstallerStepStatus.FAILED
    assert referent.read_text(encoding="utf-8") == "preserve-me"
    assert target.is_symlink()


def test_production_backend_rejects_symlink_directory_component(tmp_path: Path) -> None:
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        command_runner=lambda _argv: 0,
    )
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    assert receipt.plan is not None
    operation = replace(
        receipt.operations[0],
        target=str(linked_parent / "webui.json"),
    )

    result = backend.apply_operation(operation, plan=receipt.plan)

    assert result.status is InstallerStepStatus.FAILED
    assert not (real_parent / "webui.json").exists()


@_LINUX_OWNER_PRIVATE_WRITE
def test_user_unit_write_preserves_unrelated_sibling_units(tmp_path: Path) -> None:
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        command_runner=lambda _argv: 0,
    )
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    assert receipt.plan is not None
    unit = next(op for op in receipt.operations if op.kind == "INSTALL_USER_UNIT")
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    sibling = unit_dir / "other.service"
    sibling.write_text("unrelated", encoding="utf-8")
    operation = replace(unit, target=str(unit_dir / WEBUI_SERVICE_NAME))

    result = backend.apply_operation(operation, plan=receipt.plan)

    assert result.status is InstallerStepStatus.APPLIED
    assert sibling.read_text(encoding="utf-8") == "unrelated"
    assert (unit_dir / WEBUI_SERVICE_NAME).read_text(encoding="utf-8") == unit.desired_state


def test_production_backend_fails_closed_for_privilege_and_trust() -> None:
    calls: list[tuple[str, ...]] = []
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        command_runner=lambda argv: calls.append(argv) or 0,
    )
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    assert receipt.plan is not None
    operations = {operation.kind: operation for operation in receipt.operations}

    privileged = backend.apply_operation(
        operations["ENSURE_HOSTNAME_MAPPING"], plan=receipt.plan
    )
    trust = backend.apply_operation(
        operations["ENSURE_ISOLATED_PROFILE_TRUST"], plan=receipt.plan
    )
    lingering = backend.apply_operation(
        operations["ENABLE_USER_LINGERING"], plan=receipt.plan
    )
    assert privileged.status is InstallerStepStatus.NEEDS_PRIVILEGED_INSTALLER
    assert lingering.status is InstallerStepStatus.NEEDS_PRIVILEGED_INSTALLER
    assert trust.status is InstallerStepStatus.INCOMPLETE_BROWSER_TRUST
    assert calls == []


def test_production_apply_stops_at_privilege_boundary_before_files_or_commands() -> None:
    calls: list[tuple[str, ...]] = []
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        command_runner=lambda argv: calls.append(argv) or 0,
    )
    receipt = plan_webui_setup(_request(), _environment(), _consent())

    result = apply_webui_setup(receipt, _consent(), backend)

    assert result.status is InstallerStepStatus.NEEDS_PRIVILEGED_INSTALLER
    assert result.endpoint_state is EndpointState.PROPOSED
    assert result.completed_operations == ()
    assert calls == []


def test_production_backend_registers_only_exact_user_service_with_argv_no_shell() -> None:
    calls: list[tuple[str, ...]] = []
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/opt/systemd/bin/systemctl",
        command_runner=lambda argv: calls.append(argv) or 0,
    )
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    assert receipt.plan is not None
    operation = next(
        op for op in receipt.operations if op.kind == "REGISTER_USER_SERVICE"
    )

    result = backend.apply_operation(operation, plan=receipt.plan)

    assert result.status is InstallerStepStatus.APPLIED
    assert calls == [
        ("/opt/systemd/bin/systemctl", "--user", "daemon-reload"),
        ("/opt/systemd/bin/systemctl", "--user", "enable", WEBUI_SERVICE_NAME),
    ]


def test_production_backend_managed_activation_uses_exact_start_stop_argv_and_service_state() -> None:
    calls: list[tuple[str, ...]] = []
    clock = _InstallClock()
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    assert receipt.plan is not None
    snapshots = iter(
        [
            _service_snapshot(ServiceStatus.STARTING, None),
            _service_snapshot(ServiceStatus.READY, receipt.plan.origin),
        ]
    )
    current = _service_snapshot(ServiceStatus.STARTING, None)

    def state_reader() -> ServiceSnapshot:
        nonlocal current
        try:
            current = next(snapshots)
        except StopIteration:
            pass
        return current

    probe_calls: list[str] = []

    def probe(origin: str) -> ManagedServiceActivationResult:
        probe_calls.append(origin)
        return ManagedServiceActivationResult(
            status=(
                ManagedServiceActivationStatus.PENDING
                if len(probe_calls) == 1
                else ManagedServiceActivationStatus.READY
            ),
        )

    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/opt/systemd/bin/systemctl",
        command_runner=lambda argv: calls.append(argv) or 0,
        service_state_reader=state_reader,
        activation_probe=probe,
        clock=clock,
        sleep=clock.sleep,
        poll_interval=0.1,
    )

    activated = backend.activate_candidate(plan=receipt.plan)
    stopped = backend.stop_candidate(plan=receipt.plan)

    assert activated == ManagedServiceActivationResult(
        status=ManagedServiceActivationStatus.READY,
        saved_endpoint=receipt.plan.origin,
    )
    assert stopped.status is InstallerStepStatus.APPLIED
    assert calls == [
        ("/opt/systemd/bin/systemctl", "--user", "start", WEBUI_SERVICE_NAME),
        ("/opt/systemd/bin/systemctl", "--user", "stop", WEBUI_SERVICE_NAME),
    ]
    assert probe_calls


def test_production_backend_port_conflict_comes_only_from_typed_probe_not_stderr() -> None:
    calls: list[tuple[str, ...]] = []
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    assert receipt.plan is not None
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/opt/systemd/bin/systemctl",
        command_runner=lambda argv: calls.append(argv) or 1,
        activation_probe=lambda origin: ManagedServiceActivationResult(
            status=ManagedServiceActivationStatus.PORT_CONFLICT,
            detail=f"typed conflict for {origin}",
        ),
    )

    result = backend.activate_candidate(plan=receipt.plan)

    assert result.status is ManagedServiceActivationStatus.PORT_CONFLICT
    assert result.detail == f"typed conflict for {receipt.plan.origin}"
    assert calls == [
        ("/opt/systemd/bin/systemctl", "--user", "start", WEBUI_SERVICE_NAME),
    ]


def test_activation_ready_must_report_exact_claimed_endpoint() -> None:
    class _WrongEndpointBackend(_FakeInstallerBackend):
        def activate_candidate(self, *, plan):
            self.activations.append(plan)
            return ManagedServiceActivationResult(
                status=ManagedServiceActivationStatus.READY,
                saved_endpoint="https://factlane-1a2b3c4d-55aa.test:8774",
            )

    receipt = plan_webui_setup(_request(), _environment(), _consent())
    backend = _WrongEndpointBackend()

    result = apply_webui_setup(receipt, _consent(), backend)

    assert result.status is InstallerStepStatus.FAILED
    assert result.endpoint_state is EndpointState.PROPOSED
    assert [plan.saved_port for plan in backend.stops] == [8765]


def test_hosts_renderer_preserves_unrelated_entries_and_replaces_only_exact_hostname() -> None:
    existing = (
        "127.0.0.1 localhost\n"
        "10.0.0.9 factlane-old.test other-name\n"
        "192.0.2.7 factlane-1a2b3c4d-55aa.test keep-me\n"
        "# custom comment\n"
    )

    rendered = render_hosts_with_mapping(
        existing,
        hostname="factlane-1a2b3c4d-55aa.test",
    )

    assert "127.0.0.1 localhost" in rendered
    assert "10.0.0.9 factlane-old.test other-name" in rendered
    assert "192.0.2.7 keep-me" in rendered
    assert "# custom comment" in rendered
    assert rendered.endswith("127.0.0.1\tfactlane-1a2b3c4d-55aa.test\n")
