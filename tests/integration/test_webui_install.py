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
    EndpointState,
    InstallEnvironment,
    ManagedServiceActivationResult,
    ManagedServiceActivationStatus,
    InstallerStepResult,
    InstallerStepStatus,
    PortClaim,
    ProductionLinuxInstallerBackend,
    LINUX_OWNER_PANEL_LAUNCHER_FILENAME,
    SetupConsent,
    SetupRequest,
    WEBUI_SERVICE_NAME,
    apply_webui_setup,
    consent_requirements,
    detect_install_environment,
    plan_webui_setup,
    render_caddy_config,
    render_linux_owner_panel_launcher,
    render_windows_owner_panel_launcher,
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
        governance_revision=0,
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
        browser=None,
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
        hostname_mapping=False,
        browser_profile_trust=False,
        user_lingering=lingering,
    )


def test_detects_linux_systemd_caddy_zenity_without_browser_requirement() -> None:
    executables = {
        "systemctl": "/usr/bin/systemctl",
        "caddy": "/usr/bin/caddy",
        "zenity": "/usr/bin/zenity",
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
        browser=None,
        euid=1000,
    )


def test_non_linux_detection_returns_unsupported_facts_without_posix_uid_support() -> None:
    environment = detect_install_environment(
        system_name=lambda: "Darwin",
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
        "browser": None,
    }
    if missing == "systemd":
        values["systemd"] = False
    elif missing == "caddy":
        values["caddy_path"] = None
    elif missing == "zenity":
        values["zenity_path"] = None

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
        browser=None,
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


def test_setup_requires_only_lingering_consent_when_lingering_is_needed() -> None:
    consent = SetupConsent(
        hostname_mapping=False,
        browser_profile_trust=False,
        user_lingering=False,
    )
    receipt = plan_webui_setup(_request(), _environment(), consent)

    assert receipt.status == "BLOCKED"
    assert receipt.plan is None
    assert receipt.operations == ()
    assert any("lingering" in reason.lower() for reason in receipt.blocked_reasons)


def test_lingering_consent_is_not_required_when_existing_state_needs_no_change() -> None:
    receipt = plan_webui_setup(
        _request(lingering_required=False),
        _environment(),
        SetupConsent(
            hostname_mapping=False,
            browser_profile_trust=False,
            user_lingering=False,
        ),
    )

    assert receipt.status == "READY_TO_APPLY"
    assert receipt.plan is not None
    assert "ENABLE_USER_LINGERING" not in {operation.kind for operation in receipt.operations}


def test_consent_explanations_name_the_exact_state_changes() -> None:
    requirements = {item.key: item for item in consent_requirements(lingering_required=True)}

    assert set(requirements) == {"user_lingering"}
    assert "boot before login" in requirements["user_lingering"].explanation


def test_caddy_config_is_exact_plain_loopback_to_af_unix_without_tls_or_http3() -> None:
    hostname = webui_hostname("1a2b3c4d-55aa")
    assert hostname == "factlane-1a2b3c4d-55aa.test"

    config = render_caddy_config(
        hostname=hostname,
        port=8767,
        backend_socket="/run/user/1000/factlane/webui.sock",
        caddy_state_dir="/home/alice/.local/state/factlane/caddy",
    )

    assert "admin off" in config
    assert "persist_config off" in config
    assert "storage_clean_interval off" in config
    assert "auto_https off" in config
    assert "http://127.0.0.1:8767" in config
    assert "bind 127.0.0.1" in config
    assert "unix//run/user/1000/factlane/webui.sock" in config
    assert "https://" not in config
    assert "tls" not in config.lower()
    assert "issuer" not in config.lower()
    assert "skip_install_trust" not in config
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
        "origin": "http://127.0.0.1:8767",
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
        '"origin":"http://127.0.0.1:8767",'
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
    assert payload["origin"] == "http://127.0.0.1:8767"
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
    assert plan.origin == "http://127.0.0.1:8767"
    assert plan.browser is None
    assert "http://127.0.0.1:8767" in plan.caddy_config
    assert "https://" not in plan.caddy_config
    assert "serve webui" in plan.user_unit

    operations = {operation.kind: operation for operation in receipt.operations}
    assert set(operations) == {
        "PERSIST_PROVISIONAL_SETUP",
        "WRITE_SERVICE_CONFIG",
        "WRITE_CADDY_CONFIG",
        "INSTALL_USER_UNIT",
        "ENABLE_USER_LINGERING",
        "REGISTER_USER_SERVICE",
    }
    assert all(operation.target != "/etc/hosts" for operation in receipt.operations)
    assert not {
        "BOOTSTRAP_CADDY_CA",
        "ENSURE_ISOLATED_PROFILE_NETWORK",
        "ENSURE_ISOLATED_PROFILE_TRUST",
        "ENSURE_HOSTNAME_MAPPING",
    } & set(operations)

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
    assert result.origin == "http://127.0.0.1:8766"
    assert [plan.saved_port for plan in backend.activations] == [8765, 8766]
    assert [plan.saved_port for plan in backend.stops] == [8765]
    candidate_writes = [
        (operation.kind, json.loads(operation.desired_state).get("origin") if operation.kind == "WRITE_SERVICE_CONFIG" else plan.origin)
        for operation, plan in backend.operations
        if operation.kind in {"WRITE_CADDY_CONFIG", "WRITE_SERVICE_CONFIG"}
    ]
    assert candidate_writes == [
        ("WRITE_CADDY_CONFIG", "http://127.0.0.1:8765"),
        ("WRITE_SERVICE_CONFIG", "http://127.0.0.1:8765"),
        ("WRITE_CADDY_CONFIG", "http://127.0.0.1:8766"),
        ("WRITE_SERVICE_CONFIG", "http://127.0.0.1:8766"),
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


def test_ambiguous_candidate_stop_returns_recovery_without_attempting_rollback() -> None:
    class _AmbiguousStopBackend(_FakeInstallerBackend):
        def __init__(self) -> None:
            super().__init__(
                activation_by_port={
                    8765: ManagedServiceActivationStatus.PORT_CONFLICT,
                }
            )
            self.rollback_calls = 0
            self.recovery_seals = 0

        def stop_candidate(self, *, plan):
            self.stops.append(plan)
            return InstallerStepResult(
                status=InstallerStepStatus.RECOVERY_REQUIRED,
                operation="STOP_MANAGED_SERVICE",
                detail="governed service state changed outside setup",
            )

        def rollback_setup(self, *, plan, completed_operations):
            del plan, completed_operations
            self.rollback_calls += 1
            return InstallerStepResult(
                status=InstallerStepStatus.APPLIED,
                operation="ROLLBACK_SETUP",
            )

        def mark_recovery_required(self, *, plan, detail):
            del plan, detail
            self.recovery_seals += 1
            return InstallerStepResult(
                status=InstallerStepStatus.APPLIED,
                operation="MARK_RECOVERY_REQUIRED",
            )

    backend = _AmbiguousStopBackend()
    receipt = plan_webui_setup(_request(), _environment(), _consent())

    result = apply_webui_setup(receipt, _consent(), backend)

    assert result.status is InstallerStepStatus.RECOVERY_REQUIRED
    assert "no rollback was attempted" in (result.detail or "")
    assert backend.rollback_calls == 0
    assert backend.recovery_seals == 1
    assert [plan.saved_port for plan in backend.stops] == [8765]


def test_legacy_https_saved_endpoint_reuses_port_once_with_canonical_http_candidate() -> None:
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
    assert [plan.origin for plan in backend.activations] == ["http://127.0.0.1:8769"]
    assert [plan.saved_port for plan in backend.stops] == [8769]


def test_apply_refuses_non_ready_receipt_and_missing_operation_consent() -> None:
    blocked = plan_webui_setup(
        _request(),
        _environment(),
        SetupConsent(
            hostname_mapping=False,
            browser_profile_trust=False,
            user_lingering=False,
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
            browser_profile_trust=False,
            user_lingering=False,
        ),
        backend,
    )
    assert denied.status is InstallerStepStatus.BLOCKED
    assert backend.operations == []


def test_linux_owner_panel_launcher_is_planned_and_applied_only_after_ready_activation() -> None:
    request = replace(
        _request(lingering_required=False),
        launcher_path=f"/home/alice/.local/share/applications/{LINUX_OWNER_PANEL_LAUNCHER_FILENAME}",
    )
    receipt = plan_webui_setup(request, _environment(), _consent(lingering=False))
    assert receipt.plan is not None
    launcher = next(op for op in receipt.operations if op.kind == "INSTALL_OWNER_LAUNCHER")
    assert launcher.target == request.launcher_path
    assert launcher.desired_state == render_linux_owner_panel_launcher(
        factlane_executable=request.factlane_executable
    )
    assert "Terminal=true" in launcher.desired_state
    assert 'Exec="/home/alice/.local/bin/factlane" launch webui' in launcher.desired_state

    backend = _FakeInstallerBackend()
    result = apply_webui_setup(receipt, _consent(lingering=False), backend)

    assert result.status is InstallerStepStatus.APPLIED
    kinds = [operation.kind for operation, _plan in backend.operations]
    assert kinds[-1] == "INSTALL_OWNER_LAUNCHER"
    assert result.completed_operations[-1] == "INSTALL_OWNER_LAUNCHER"


@_LINUX_OWNER_PRIVATE_WRITE
def test_linux_production_backend_writes_visible_terminal_launcher_without_shell(tmp_path: Path) -> None:
    request = replace(
        _request(lingering_required=False),
        launcher_path=f"/home/alice/.local/share/applications/{LINUX_OWNER_PANEL_LAUNCHER_FILENAME}",
    )
    receipt = plan_webui_setup(request, _environment(), _consent(lingering=False))
    assert receipt.plan is not None
    planned = next(op for op in receipt.operations if op.kind == "INSTALL_OWNER_LAUNCHER")
    target = tmp_path / LINUX_OWNER_PANEL_LAUNCHER_FILENAME
    operation = replace(planned, target=str(target))
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        command_runner=lambda _argv: pytest.fail("launcher write must not invoke a shell or command runner"),
    )

    result = backend.apply_operation(operation, plan=receipt.plan)

    assert result.status is InstallerStepStatus.APPLIED
    content = target.read_text(encoding="utf-8")
    assert "Terminal=true" in content
    assert 'Exec="/home/alice/.local/bin/factlane" launch webui' in content
    assert all(token not in content for token in ("sh -c", "bash -c", "sudo", "pkexec"))


def test_windows_owner_panel_launcher_spec_uses_visible_current_user_python_and_is_shell_free() -> None:
    rendered = render_windows_owner_panel_launcher(
        factlane_executable=r"C:\Users\Alice\factlane\Scripts\python.exe",
        owner_config_home=r"C:\Users\Alice\AppData\Local\FactLane\config",
    )
    payload = json.loads(rendered)

    assert payload == {
        "arguments": "-m factlane.webui_launcher",
        "description": "Open the local FactLane Owner Panel",
        "target": r"C:\Users\Alice\factlane\Scripts\python.exe",
        "working_directory": r"C:\Users\Alice\AppData\Local\FactLane",
    }
    assert all(token not in rendered.casefold() for token in ("cmd.exe", "powershell", "wscript.exe"))


def test_windows_setup_plan_adds_only_current_user_start_menu_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    import factlane.webui_install as install

    monkeypatch.setattr(install.os, "name", "nt")
    request = SetupRequest(
        installation_id="windows-owner-1",
        saved_port=8765,
        lingering_required=False,
        installing_user=r"DESKTOP\Alice",
        factlane_executable=r"C:\Users\Alice\factlane\Scripts\python.exe",
        owner_config_home=r"C:\Users\Alice\AppData\Local\FactLane\config",
        service_config_path=r"C:\Users\Alice\AppData\Local\FactLane\config\webui.json",
        caddy_config_path=r"C:\Users\Alice\AppData\Local\FactLane\config\transport.json",
        user_unit_path=r"C:\Users\Alice\AppData\Local\FactLane\state\task-scheduler.json",
        backend_socket=r"C:\Users\Alice\AppData\Local\FactLane\runtime\owner-http",
        runtime_dir=r"C:\Users\Alice\AppData\Local\FactLane\runtime",
        state_dir=r"C:\Users\Alice\AppData\Local\FactLane\state",
        caddy_state_dir=r"C:\Users\Alice\AppData\Local\FactLane\state\transport",
        browser_profile_dir=r"C:\Users\Alice\AppData\Local\FactLane\browser",
        native_control_socket=r"\\.\pipe\factlane-owner-test",
        launcher_path=(
            r"C:\Users\Alice\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\FactLane Owner Panel.lnk"
        ),
    )
    environment = InstallEnvironment(
        system="Windows",
        systemd=False,
        caddy_path=None,
        zenity_path=None,
        browser=None,
        euid=-1,
    )
    consent = SetupConsent(
        hostname_mapping=False,
        browser_profile_trust=False,
        user_lingering=False,
        windows_native_setup=True,
    )

    receipt = plan_webui_setup(request, environment, consent)

    assert receipt.status == "READY_TO_APPLY"
    launcher = next(op for op in receipt.operations if op.kind == "INSTALL_OWNER_LAUNCHER")
    assert launcher.target == request.launcher_path
    assert launcher.consent_key == "windows_native_setup"
    assert launcher.target.endswith(r"\Start Menu\Programs\FactLane Owner Panel.lnk")
    assert "ProgramData" not in launcher.target
    assert receipt.plan is not None
    assert receipt.plan.browser is None
    assert receipt.plan.origin == "http://127.0.0.1:8765"
    assert not {
        "ENSURE_ISOLATED_PROFILE_NETWORK",
        "ENSURE_ISOLATED_PROFILE_TRUST",
        "BOOTSTRAP_CADDY_CA",
    } & {operation.kind for operation in receipt.operations}


def test_apply_propagates_typed_provisional_failure_before_endpoint_writes() -> None:
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    provisional_failed = _FakeInstallerBackend(
        step_statuses={
            "PERSIST_PROVISIONAL_SETUP": InstallerStepStatus.RECOVERY_REQUIRED,
        }
    )
    result = apply_webui_setup(receipt, _consent(), provisional_failed)
    assert result.status is InstallerStepStatus.RECOVERY_REQUIRED
    assert result.endpoint_state is EndpointState.PROPOSED
    assert result.claimed_port is None
    assert all(op.kind != "WRITE_SERVICE_CONFIG" for op, _plan in provisional_failed.operations)


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
        next(
            op
            for op in plan_webui_setup(_request(), _environment(), _consent()).operations
            if op.kind == "WRITE_SERVICE_CONFIG"
        ),
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


@_LINUX_OWNER_PRIVATE_WRITE
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
    operation = replace(
        next(op for op in receipt.operations if op.kind == "WRITE_SERVICE_CONFIG"),
        target=str(target),
    )

    result = backend.apply_operation(operation, plan=receipt.plan)

    assert result.status is InstallerStepStatus.FAILED
    assert referent.read_text(encoding="utf-8") == "preserve-me"
    assert target.is_symlink()


@_LINUX_OWNER_PRIVATE_WRITE
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
        next(op for op in receipt.operations if op.kind == "WRITE_SERVICE_CONFIG"),
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


def test_linux_plan_has_no_browser_profile_ca_or_system_hostname_operations() -> None:
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    assert receipt.plan is not None
    assert receipt.plan.browser is None
    kinds = {operation.kind for operation in receipt.operations}
    assert "ENSURE_HOSTNAME_MAPPING" not in kinds
    assert "ENSURE_ISOLATED_PROFILE_NETWORK" not in kinds
    assert "ENSURE_ISOLATED_PROFILE_TRUST" not in kinds
    assert "BOOTSTRAP_CADDY_CA" not in kinds
    assert all(operation.target != "/etc/hosts" for operation in receipt.operations)


def test_production_backend_registers_only_exact_user_service_with_argv_no_shell() -> None:
    calls: list[tuple[str, ...]] = []
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/opt/systemd/bin/systemctl",
        owner_uid=1000,
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
        owner_uid=1000,
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
        ("/opt/systemd/bin/systemctl", "--user", "restart", WEBUI_SERVICE_NAME),
        ("/opt/systemd/bin/systemctl", "--user", "stop", WEBUI_SERVICE_NAME),
    ]
    assert probe_calls


@pytest.mark.parametrize("legacy_status", [ServiceStatus.STARTING, ServiceStatus.READY])
def test_production_backend_waits_through_exact_legacy_endpoint_migration(
    legacy_status: ServiceStatus,
) -> None:
    calls: list[tuple[str, ...]] = []
    clock = _InstallClock()
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    assert receipt.plan is not None
    legacy_origin = f"https://{receipt.plan.hostname}:{receipt.plan.saved_port}"
    snapshots = iter(
        [
            _service_snapshot(legacy_status, legacy_origin),
            _service_snapshot(ServiceStatus.READY, receipt.plan.origin),
        ]
    )
    current = _service_snapshot(legacy_status, legacy_origin)

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
        owner_uid=1000,
        command_runner=lambda argv: calls.append(argv) or 0,
        service_state_reader=state_reader,
        activation_probe=probe,
        clock=clock,
        sleep=clock.sleep,
        poll_interval=0.1,
    )

    activated = backend.activate_candidate(plan=receipt.plan)

    assert activated == ManagedServiceActivationResult(
        status=ManagedServiceActivationStatus.READY,
        saved_endpoint=receipt.plan.origin,
    )
    assert calls == [
        ("/opt/systemd/bin/systemctl", "--user", "restart", WEBUI_SERVICE_NAME),
    ]
    assert probe_calls == [receipt.plan.origin, receipt.plan.origin]


def test_production_backend_default_activation_budget_allows_slow_cold_start() -> None:
    calls: list[tuple[str, ...]] = []
    clock = _InstallClock()
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    assert receipt.plan is not None

    def state_reader() -> ServiceSnapshot:
        if clock.value < 15.0:
            return _service_snapshot(ServiceStatus.STARTING, receipt.plan.origin)
        return _service_snapshot(ServiceStatus.READY, receipt.plan.origin)

    def probe(_origin: str) -> ManagedServiceActivationResult:
        return ManagedServiceActivationResult(
            status=(
                ManagedServiceActivationStatus.PENDING
                if clock.value < 15.0
                else ManagedServiceActivationStatus.READY
            )
        )

    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/opt/systemd/bin/systemctl",
        owner_uid=1000,
        command_runner=lambda argv: calls.append(argv) or 0,
        service_state_reader=state_reader,
        activation_probe=probe,
        clock=clock,
        sleep=clock.sleep,
        poll_interval=0.5,
    )

    activated = backend.activate_candidate(plan=receipt.plan)

    assert activated == ManagedServiceActivationResult(
        status=ManagedServiceActivationStatus.READY,
        saved_endpoint=receipt.plan.origin,
    )
    assert 15.0 <= clock.value < 30.0
    assert calls == [
        ("/opt/systemd/bin/systemctl", "--user", "restart", WEBUI_SERVICE_NAME),
    ]


def test_production_backend_still_rejects_foreign_same_port_saved_endpoint() -> None:
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    assert receipt.plan is not None
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/opt/systemd/bin/systemctl",
        owner_uid=1000,
        command_runner=lambda _argv: 0,
        service_state_reader=lambda: _service_snapshot(
            ServiceStatus.STARTING,
            f"https://factlane-foreign.test:{receipt.plan.saved_port}",
        ),
        activation_probe=lambda _origin: ManagedServiceActivationResult(
            status=ManagedServiceActivationStatus.PENDING,
        ),
    )

    activated = backend.activate_candidate(plan=receipt.plan)

    assert activated.status is ManagedServiceActivationStatus.FAILED
    assert activated.saved_endpoint == f"https://factlane-foreign.test:{receipt.plan.saved_port}"
    assert activated.detail == "service state saved endpoint differs from the candidate origin"


def test_production_backend_port_conflict_comes_only_from_typed_probe_not_stderr() -> None:
    calls: list[tuple[str, ...]] = []
    receipt = plan_webui_setup(_request(), _environment(), _consent())
    assert receipt.plan is not None
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/opt/systemd/bin/systemctl",
        owner_uid=1000,
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
        ("/opt/systemd/bin/systemctl", "--user", "restart", WEBUI_SERVICE_NAME),
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


def test_windows_mutation_guard_reentrant_serializes_and_times_out(monkeypatch):
    import threading
    import factlane.webui_windows as windows
    from contextlib import contextmanager

    events = []
    @contextmanager
    def native_lock(*args, **kwargs):
        events.append('acquire')
        try:
            yield
        finally:
            events.append('release')

    monkeypatch.setattr(windows, '_native_windows_mutation_lock', native_lock, raising=False)
    monkeypatch.setattr(windows, 'windows_owner_paths', lambda: type('Paths', (), {'state_dir': r'C:\Local\FactLane\state'})())
    failures = []
    with windows.windows_mutation_guard(timeout=0.1):
        with windows.windows_mutation_guard(timeout=0.1):
            def contender():
                try:
                    with windows.windows_mutation_guard(timeout=0.01):
                        failures.append('entered')
                except windows.WindowsWebUIError as exc:
                    failures.append(exc.code)
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(1)
            assert not thread.is_alive()
        assert events == ['acquire']
    assert failures == ['WINDOWS_MUTATION_BUSY']
    assert events == ['acquire', 'release']
    with pytest.raises(RuntimeError), windows.windows_mutation_guard():
        raise RuntimeError('abort')
    with windows.windows_mutation_guard():
        pass
    assert events == ['acquire', 'release'] * 3


def test_service_transaction_acquires_file_guard_before_instance_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import factlane.webui_service as service
    state = service.WebUIServiceState(tmp_path / 'service.json', boot_id=lambda: 'boot')
    events = []
    @contextmanager
    def guard(path):
        events.append('mutation')
        yield
    @contextmanager
    def instance_lock():
        events.append('instance')
        yield
    monkeypatch.setattr(service, 'service_state_file_guard', guard)
    state._lock = instance_lock()
    state.snapshot()
    assert events == ['mutation', 'instance']


@pytest.mark.parametrize('failure', ['result', 'exception', 'commit'])
def test_windows_apply_rolls_back_current_partial_operation(failure):
    from contextlib import contextmanager
    import factlane.webui_install as install
    plan = plan_webui_setup(_request(), _environment(), _consent()).plan
    assert plan is not None
    events = []
    class Backend:
        @contextmanager
        def mutation_guard(self):
            events.append('lock')
            try:
                yield
            finally:
                events.append('unlock')
        def prepare_setup(self, *, plan):
            events.append('prepare')
        def apply_operation(self, operation, *, plan):
            events.append('partial')
            if failure == 'exception':
                raise RuntimeError('primary')
            return InstallerStepResult(status=InstallerStepStatus.FAILED if failure == 'result' else InstallerStepStatus.APPLIED, operation=operation.kind, detail='primary')
        def commit_setup(self, *, plan):
            events.append('commit')
            raise RuntimeError('commit')
        def rollback_setup(self, *, plan, completed_operations):
            events.append('rollback')
            return InstallerStepResult(status=InstallerStepStatus.APPLIED, operation='ROLLBACK')
    result = install._apply_windows_webui_setup(plan, _consent(), Backend())
    assert result.status is InstallerStepStatus.FAILED
    assert events[-2:] == ['rollback', 'unlock']
    assert events[:3] == ['lock', 'prepare', 'partial']
    assert 'failed safely' not in result.detail


def test_windows_apply_preflights_all_consent_before_prepare():
    import factlane.webui_install as install
    plan = plan_webui_setup(_request(), _environment(), _consent()).plan
    assert plan is not None
    operation = replace(plan.operations[-1], consent_key='windows_native_setup')
    plan = replace(plan, operations=(*plan.operations[:-1], operation))
    backend = _FakeInstallerBackend()
    result = install._apply_windows_webui_setup(plan, _consent(), backend)
    assert result.status is InstallerStepStatus.BLOCKED
    assert backend.operations == []


def test_task_snapshot_and_conditional_restore_plain_data():
    from types import SimpleNamespace
    from copy import deepcopy
    import factlane.webui_windows as windows
    supervisor = object.__new__(windows.WindowsTaskSchedulerSupervisor)
    supervisor.task_name = windows.WINDOWS_TASK_NAME
    supervisor.owner_sid = 'S-1-5-21-1'
    supervisor.user_id = 'Alice'
    supervisor.expected_executable = r'C:\Python\python.exe'
    supervisor.environment_executable = r'C:\FactLane\python.exe'
    task = SimpleNamespace(Xml='<Task/>', Enabled=False, State=1)
    task.GetSecurityDescriptor = lambda flags: 'O:ownerG:groupD:P(A;;GA;;;owner)'
    task.SetSecurityDescriptor = lambda sddl, flags: None
    calls = []
    root = SimpleNamespace(GetTask=lambda name: task)
    def register(*args):
        calls.append(args)
        task.Xml = args[1]
        return task
    root.RegisterTask = register
    service = SimpleNamespace(GetFolder=lambda path: root)
    supervisor._run_with_service = lambda callback: callback(service)
    supervisor._validate_task_security = lambda task: None
    supervisor._validate_registered_task = lambda task, plan: None
    before = supervisor.snapshot_task()
    assert before == {'exists': True, 'xml': '<Task/>', 'sddl': 'O:ownerG:groupD:P(A;;GA;;;owner)', 'enabled': False}
    task.Xml = '<Task updated="yes"/>'
    post = supervisor.snapshot_task()
    supervisor.restore_task(before, expected=post)
    assert supervisor.snapshot_task() == before
    assert calls[0][1] == '<Task/>'
    assert calls[0][-1] == before['sddl']
    foreign = deepcopy(post)
    foreign['xml'] = 'foreign'
    with pytest.raises(windows.WindowsWebUIError, match='changed'):
        supervisor.restore_task(before, expected=foreign)


def test_windows_file_custody_rejects_equal_bytes_foreign_identity():
    import factlane.webui_windows as windows
    original = {'exists': True, 'identity': [1, 10], 'content': 'YQ==', 'sddl': 'private'}
    foreign = dict(original, identity=[1, 11])
    assert windows.windows_file_custody_matches(original, dict(original))
    assert not windows.windows_file_custody_matches(original, foreign)


@pytest.mark.parametrize('damage', ['oversize', 'version', 'owner', 'target', 'extra', 'phase'])
def test_windows_journal_rejects_ambiguous_or_foreign_state(damage):
    import factlane.webui_install as install
    payload = {'version': 1, 'transaction_id': '9a0eaec9-33c8-4bd0-aef5-4755cc8ce380',
               'installation_id': 'install', 'owner_sid': 'owner', 'phase': 'PREPARED',
               'operations': []}
    if damage == 'oversize':
        raw = b' ' * (install.WINDOWS_SETUP_JOURNAL_LIMIT + 1)
    else:
        if damage == 'version': payload['version'] = 2
        if damage == 'owner': payload['owner_sid'] = 'foreign'
        if damage == 'target': payload['operations'] = [{'target': 'foreign'}]
        if damage == 'extra': payload['foreign'] = True
        if damage == 'phase': payload['phase'] = 'maybe'
        raw = json.dumps(payload).encode()
    with pytest.raises(install.WindowsWebUIError):
        install._decode_windows_setup_journal(raw, installation_id='install', owner_sid='owner', targets={})


@pytest.mark.parametrize('current_state', ['before', 'published', 'foreign'])
def test_windows_recovery_inspects_prepared_and_is_idempotent(current_state):
    import factlane.webui_install as install
    backend = object.__new__(install.ProductionWindowsInstallerBackend)
    before = {'exists': False}
    intended = {'exists': True, 'identity': [1, 2], 'content': 'eA==', 'sddl': 'private'}
    current = dict(before if current_state == 'before' else intended)
    if current_state == 'foreign':
        current['identity'] = [1, 3]
    record = {'kind': 'WRITE_SERVICE_CONFIG', 'target': 'config', 'status': 'PREPARED',
              'before': before, 'intended': intended, 'after': None, 'restore': None}
    backend._journal = {'phase': 'APPLYING', 'operations': [record]}
    backend._snapshot_target = lambda kind, target: dict(current)
    persisted = []
    backend._persist_journal = lambda: persisted.append(json.loads(json.dumps(backend._journal)))
    def restore(record, expected):
        assert expected == intended
        current.clear()
        current.update(before)
        return dict(before)
    backend._restore_record = restore
    backend._close_stages = lambda: None
    backend._assert_recovery_quiescent = lambda: None
    result = backend.rollback_setup(plan=None, completed_operations=())
    if current_state == 'foreign':
        assert result.status is InstallerStepStatus.RECOVERY_REQUIRED
        assert record['status'] == 'CONFLICT'
        assert backend._journal['phase'] == 'RECOVERY_REQUIRED'
    else:
        assert result.status is InstallerStepStatus.APPLIED
        assert current == before
        again = backend.rollback_setup(plan=None, completed_operations=())
        assert again.status is InstallerStepStatus.APPLIED
        assert record['status'] == 'RESTORED'
    assert persisted


def test_windows_partial_task_enabled_failure_is_recoverable():
    import factlane.webui_install as install
    backend = object.__new__(install.ProductionWindowsInstallerBackend)
    original = {'exists': True, 'xml': 'original', 'sddl': 'old-private', 'enabled': False}
    intended = {'exists': True, 'xml': 'transaction-postimage', 'sddl': 'new-private', 'enabled': True}
    intermediate = dict(intended, enabled=False, sddl='old-private')
    record = {'kind': 'REGISTER_WINDOWS_TASK', 'target': 'task', 'status': 'PREPARED',
              'before': original, 'intended': intended, 'after': None, 'restore': None}
    backend._journal = {'phase': 'APPLYING', 'operations': [record]}
    current = dict(intermediate)
    backend._snapshot_target = lambda kind, target: dict(current)
    backend._persist_journal = lambda: None
    backend._close_stages = lambda: None
    backend._assert_recovery_quiescent = lambda: None
    def restore(record, expected):
        assert expected == intermediate
        current.clear()
        current.update(original)
        return dict(original)
    backend._restore_record = restore
    result = backend.rollback_setup(plan=None, completed_operations=())
    assert result.status is InstallerStepStatus.APPLIED
    assert current == original


def test_task_scheduler_operations_participate_in_mutation_guard(monkeypatch):
    from contextlib import contextmanager
    import factlane.webui_windows as windows
    supervisor = object.__new__(windows.WindowsTaskSchedulerSupervisor)
    events = []
    @contextmanager
    def guard():
        events.append('locked')
        try:
            yield
        finally:
            events.append('released')
    monkeypatch.setattr(windows, 'windows_mutation_guard', guard)
    supervisor._run_with_service_unlocked = lambda operation: operation('service')
    assert supervisor._run_with_service(lambda service: events.append(service) or 42) == 42
    assert events == ['locked', 'service', 'released']


@pytest.mark.parametrize('fault', ['none', 'before', 'after', 'ack', 'ack_after'])
@pytest.mark.parametrize('kind', ['WRITE_SERVICE_CONFIG', 'REGISTER_WINDOWS_TASK', 'INSTALL_OWNER_LAUNCHER'])
@pytest.mark.parametrize('prestate', ['absent', 'preexisting', 'config-noop'])
def test_windows_setup_journal_faults_restore_exact_owned_state(tmp_path, monkeypatch, kind, fault, prestate):
    """Run the production coordinator/journal with native file and COM boundaries faked."""
    from types import SimpleNamespace
    from copy import deepcopy
    import base64
    import factlane.webui_install as install
    windows = install.windows
    import sqlite3
    monkeypatch.setattr(sqlite3, 'connect', lambda *args, **kwargs: pytest.fail('setup accessed a database'))
    backend = object.__new__(install.ProductionWindowsInstallerBackend)
    backend.request = SimpleNamespace(installation_id='install', service_config_path='config', launcher_path='launcher', state_dir='state')
    backend._paths = SimpleNamespace(root_dir='root', owner_config_home='home', runtime_dir='runtime', state_dir='state')
    backend._task_executable = 'python'
    backend._validate_launcher_target = lambda: None
    monkeypatch.setattr(windows, 'current_user_sid', lambda: 'owner')
    monkeypatch.setattr(windows, 'windows_owner_paths', lambda **kwargs: backend._paths)
    monkeypatch.setattr(install, 'validate_windows_path_no_reparse', lambda path: tmp_path)
    monkeypatch.setattr(windows, 'validate_owner_private_path', lambda *args, **kwargs: None)
    monkeypatch.setattr(windows, 'windows_mutation_guard', __import__('contextlib').nullcontext)
    managed = windows.WindowsManagedConfig(installation_id='install', origin='http://127.0.0.1:8765',
        native_control_pipe=r'\\.\pipe\factlane-owner-test', runtime_dir=r'C:\Local\FactLane\runtime',
        state_dir=r'C:\Local\FactLane\state', browser_profile_dir=r'C:\Local\FactLane\browser')
    original_config = windows.render_windows_managed_config(managed).encode()
    desired_config = windows.render_windows_managed_config(replace(managed, origin='http://127.0.0.1:8766'))
    files = {'config': {'exists': True, 'identity': [1, 1], 'content': base64.b64encode(original_config).decode(), 'sddl': 'private'},
             'launcher': {'exists': True, 'identity': [1, 2], 'content': base64.b64encode(b'old-link').decode(), 'sddl': 'private'}}
    if prestate == 'absent':
        files.clear()
    if prestate == 'config-noop':
        files['config']['content'] = base64.b64encode(desired_config.encode()).decode()
    files['unrelated-sibling'] = {'exists': True, 'identity': [1, 42], 'content': 'c2libGluZw==', 'sddl': 'private'}
    initial = deepcopy(files)
    task = ({'exists': False} if prestate == 'absent' else
            {'exists': True, 'xml': 'disabled-original', 'sddl': 'old-private', 'enabled': False})
    initial_task = deepcopy(task)
    injected = False
    events = []
    def fail(point, operation):
        nonlocal injected
        if not injected and point == fault and operation == kind:
            injected = True
            raise RuntimeError('injected ' + point)
    def snapshot(path, **kwargs):
        return deepcopy(files.get(path, {'exists': False}))
    monkeypatch.setattr(windows, 'snapshot_windows_setup_file', snapshot)
    serial = 100
    def stage(path, content, **kwargs):
        nonlocal serial
        serial += 1
        post = {'exists': True, 'identity': [1, serial], 'content': base64.b64encode(content).decode(), 'sddl': kwargs.get('sddl') or 'private'}
        return (path, None, post)
    monkeypatch.setattr(windows, 'stage_windows_setup_file', stage)
    monkeypatch.setattr(windows, 'close_windows_setup_stage', lambda *args, **kwargs: None)
    def publish(path, staged, *, expected, **kwargs):
        operation = 'WRITE_SERVICE_CONFIG' if path == 'config' else 'INSTALL_OWNER_LAUNCHER'
        assert files.get(path, {'exists': False}) == expected
        fail('before', operation)
        events.append(('publish', operation))
        files[path] = deepcopy(staged[2])
        fail('after', operation)
    monkeypatch.setattr(windows, 'publish_windows_setup_file', publish)
    def delete(path, *, expected, **kwargs):
        assert files[path] == expected
        del files[path]
    monkeypatch.setattr(windows, 'delete_windows_setup_file', delete)
    backend._stage_launcher_bytes = lambda operation: b'new-link'
    def prepare_task(executable, *, transaction_id):
        return {'exists': True, 'xml': transaction_id, 'sddl': 'private', 'enabled': True}
    def publish_task(intended, *, expected):
        assert task == expected
        fail('before', 'REGISTER_WINDOWS_TASK')
        events.append(('publish', 'REGISTER_WINDOWS_TASK'))
        task.clear()
        task.update(deepcopy(intended))
        fail('after', 'REGISTER_WINDOWS_TASK')
    def restore_task(before, *, expected):
        assert task == expected
        task.clear()
        task.update(deepcopy(before))
    backend.supervisor = SimpleNamespace(prepare_task=prepare_task, snapshot_task=lambda: deepcopy(task),
                                        publish_task=publish_task, restore_task=restore_task, verify_quiescent=lambda: None)
    backend._expected_precondition = backend.capture_precondition()
    def journal_write(path, content, **kwargs):
        payload = json.loads(content)
        assert files.get(path, {'exists': False}) == kwargs['expected_state']
        for record in payload['operations']:
            if record['status'] == 'APPLIED':
                fail('ack', record['kind'])
        files[path] = {'exists': True, 'identity': [1, 999], 'content': base64.b64encode(content.encode()).decode(), 'sddl': 'private'}
        events.append(('journal', payload['phase']))
        for record in payload['operations']:
            if record['status'] == 'APPLIED':
                fail('ack_after', record['kind'])
    monkeypatch.setattr(install, 'write_owner_private_text_atomic', journal_write)
    operations = tuple(SimpleNamespace(kind=k, target=v, desired_state=desired_config, consent_key=None)
                       for k, v in backend._targets().items())
    plan = SimpleNamespace(endpoint_state=EndpointState.PROPOSED, operations=operations, origin='http://127.0.0.1:8765')
    result = install._apply_windows_webui_setup(plan, _consent(), backend)
    assert events[0] == ('journal', 'PREPARED')
    no_op_fault = prestate == 'config-noop' and kind == 'WRITE_SERVICE_CONFIG' and fault in {'before', 'after'}
    if fault == 'none' or no_op_fault:
        assert result.status is InstallerStepStatus.APPLIED
        assert result.transaction_id == backend.transaction_id
        if prestate == 'config-noop':
            assert files['config'] == initial['config']
        assert backend._journal['phase'] == 'COMMITTED'
    else:
        assert injected
        if fault == 'ack_after':
            # A failed journal write acknowledgment cannot adopt bytes as custody.
            # Reload the durable journal as a subsequent process would do.
            assert result.status is InstallerStepStatus.RECOVERY_REQUIRED
            backend._journal_custody = snapshot(backend._journal_path)
            backend._journal = install._decode_windows_setup_journal(
                base64.b64decode(backend._journal_custody['content']),
                installation_id='install', owner_sid='owner', targets=backend._targets())
            assert backend.rollback_setup(plan=plan, completed_operations=()).status is InstallerStepStatus.APPLIED
        else:
            assert result.status is InstallerStepStatus.FAILED
        assert backend._journal['phase'] == 'ROLLED_BACK'
        for path, original in initial.items():
            assert files[path]['content'] == original['content']
            assert files[path]['sddl'] == original['sddl']
        assert task == initial_task
        for path in {'config', 'launcher'} - initial.keys():
            assert path not in files
        assert files['unrelated-sibling'] == initial['unrelated-sibling']
        assert backend.rollback_setup(plan=plan, completed_operations=()).status is InstallerStepStatus.APPLIED


def test_windows_task_recovery_preserves_stop_intent_without_replaying_service_state(monkeypatch):
    from types import SimpleNamespace
    import base64
    import factlane.webui_install as install
    backend = object.__new__(install.ProductionWindowsInstallerBackend)
    backend.request = SimpleNamespace(state_dir='state')
    backend._owner_sid = 'owner'
    restored = []
    backend.supervisor = SimpleNamespace(
        restore_task=lambda original, *, expected: restored.append((original, expected))
    )
    intent = json.dumps({'status': ServiceStatus.STOPPED_UNTIL_REBOOT.value, 'intent_revision': 99}).encode()
    monkeypatch.setattr(install.windows, 'snapshot_windows_setup_file',
                        lambda *args, **kwargs: {'exists': True, 'content': base64.b64encode(intent).decode()})
    original = {'exists': True, 'xml': 'old', 'sddl': 'private', 'enabled': True}
    current = dict(original, xml='new')
    assert backend._restore_record({'kind': 'REGISTER_WINDOWS_TASK', 'before': original}, current) == original
    assert restored == [(original, current)]


def test_windows_setup_guard_queues_lifecycle_without_lost_intent(tmp_path, monkeypatch):
    import threading
    from contextlib import nullcontext
    import factlane.webui_service as service
    import factlane.webui_windows as windows
    state = service.WebUIServiceState(tmp_path / 'service.json', boot_id=lambda: 'boot')
    initial = state.snapshot()
    monkeypatch.setattr(windows, '_native_windows_mutation_lock', lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(windows, 'windows_owner_paths', lambda: type('Paths', (), {'state_dir': r'C:\Local\FactLane\state'})())
    monkeypatch.setattr(service, 'service_state_file_guard', lambda path: windows.windows_mutation_guard())
    queued = threading.Event()
    done = threading.Event()
    failures = []
    def lifecycle():
        queued.set()
        try:
            state.request_stop('queued-stop')
        except Exception as exc:
            failures.append(exc)
        finally:
            done.set()
    with windows.windows_mutation_guard():
        worker = threading.Thread(target=lifecycle)
        worker.start()
        assert queued.wait(1)
        assert not done.wait(0.03)
        assert state.snapshot().intent_revision == initial.intent_revision
    worker.join(2)
    assert done.is_set() and not failures
    final = state.snapshot()
    assert final.status is ServiceStatus.STOPPED_UNTIL_REBOOT
    assert final.intent_revision == initial.intent_revision + 1


@pytest.mark.parametrize('code, absent', [(0x80070002, True), (0x80070005, False), (0x80004005, False)])
def test_windows_task_snapshot_only_treats_not_found_as_absence(code, absent):
    from types import SimpleNamespace
    import factlane.webui_windows as windows
    supervisor = object.__new__(windows.WindowsTaskSchedulerSupervisor)
    supervisor.task_name = windows.WINDOWS_TASK_NAME
    class ComFailure(Exception):
        hresult = code
    def get_task(name):
        raise ComFailure()
    service = SimpleNamespace(GetFolder=lambda path: SimpleNamespace(GetTask=get_task))
    supervisor._run_with_service = lambda callback: callback(service)
    if absent:
        assert supervisor.snapshot_task() == {'exists': False}
    else:
        with pytest.raises(windows.WindowsWebUIError) as error:
            supervisor.snapshot_task()
        assert error.value.code == 'WINDOWS_TASK_SNAPSHOT_UNAVAILABLE'


def test_windows_conditional_file_publication_rejects_equal_bytes_foreign_custody(monkeypatch):
    import factlane.webui_windows as windows
    owned = {'exists': True, 'identity': [1, 1], 'content': 'eA==', 'sddl': 'private'}
    foreign = dict(owned, identity=[1, 2])
    monkeypatch.setattr(windows, 'snapshot_windows_setup_file', lambda *args, **kwargs: foreign)
    monkeypatch.setattr(windows, '_publish_owner_private_temp', lambda *args, **kwargs: pytest.fail('foreign file overwritten'))
    with pytest.raises(windows.WindowsWebUIError, match='changed'):
        windows.publish_windows_setup_file('target', (None, None, owned), expected=owned, owner_sid='owner')


def test_windows_launcher_partial_staging_leaves_destination_untouched(monkeypatch):
    from types import SimpleNamespace
    import factlane.webui_install as install
    backend = object.__new__(install.ProductionWindowsInstallerBackend)
    backend.request = SimpleNamespace(state_dir='state')
    backend._owner_sid = 'owner'
    payload = install.render_windows_owner_panel_launcher(factlane_executable=r'C:\Python\python.exe', owner_config_home=r'C:\Local\FactLane\config')
    operation = SimpleNamespace(desired_state=payload, target=install.WINDOWS_OWNER_PANEL_LAUNCHER_FILENAME)
    monkeypatch.setattr(install.Path, 'is_file', lambda path: True)
    partial = {}
    def writer(path, *args):
        assert path != operation.target
        partial[path] = {'exists': True, 'identity': [1, 99], 'content': 'eA==', 'sddl': 'private'}
        raise RuntimeError('partial staging write')
    backend._shortcut_writer = writer
    monkeypatch.setattr(install.windows, 'snapshot_windows_setup_file', lambda path, **kwargs: partial.get(path, {'exists': False}))
    def delete(path, *, expected, **kwargs):
        assert path != operation.target and partial[path] == expected
        del partial[path]
    monkeypatch.setattr(install.windows, 'delete_windows_setup_file', delete)
    with pytest.raises(RuntimeError, match='partial staging'):
        backend._stage_launcher_bytes(operation)
    assert partial == {}


def test_windows_setup_refuses_unknown_config_before_journaling_credentials():
    import base64
    import factlane.webui_install as install
    snapshot = {'exists': True, 'identity': [1, 1], 'sddl': 'private',
                'content': base64.b64encode(b'{"password":"do-not-copy"}').decode()}
    with pytest.raises(install.WindowsWebUIError, match='config'):
        install._validate_windows_setup_config(snapshot, installation_id='install')


def test_windows_setup_rejects_launcher_outside_current_user_start_menu(monkeypatch):
    from types import SimpleNamespace
    import factlane.webui_install as install
    backend = object.__new__(install.ProductionWindowsInstallerBackend)
    monkeypatch.setenv('APPDATA', r'C:\Users\Alice\AppData\Roaming')
    backend.request = SimpleNamespace(launcher_path=r'C:\Other\FactLane Owner Panel.lnk')
    with pytest.raises(install.WindowsWebUIError, match='Start Menu'):
        backend._validate_launcher_target()


def test_windows_setup_precondition_change_after_consent_fails_before_prepare_mutation():
    import factlane.webui_install as install
    backend = object.__new__(install.ProductionWindowsInstallerBackend)
    expected = install.WindowsSetupPrecondition(
        owner_sid='owner',
        service_config={'exists': True, 'identity': [1, 1]},
        scheduled_task={'exists': True, 'xml': 'before'},
        launcher={'exists': True, 'identity': [1, 2]},
        service_state={'exists': True, 'identity': [1, 3]},
        setup_journal={'exists': False},
    )
    backend._expected_precondition = expected
    backend.capture_precondition = lambda: replace(
        expected,
        scheduled_task={'exists': True, 'xml': 'changed-during-consent'},
    )

    with pytest.raises(install.WindowsWebUIError) as raised:
        backend._require_precondition_unchanged()

    assert raised.value.code == 'WINDOWS_SETUP_PRECONDITION_CHANGED'


def test_windows_committed_setup_accepts_only_authenticated_memory_binding_evolution():
    import base64
    import factlane.webui_install as install
    managed = install.WindowsManagedConfig(
        installation_id='install',
        origin='http://127.0.0.1:8765',
        native_control_pipe=r'\\.\pipe\factlane-owner-test',
        runtime_dir=r'C:\Local\FactLane\runtime',
        state_dir=r'C:\Local\FactLane\state',
        browser_profile_dir=r'C:\Local\FactLane\browser',
    )
    bound = replace(managed, db=r'C:\Local\FactLane\state\memory.db', profile='nomic-768', host_id='owner-install')
    def snapshot(config):
        raw = install.render_windows_managed_config(config).encode()
        return {'exists': True, 'identity': [1, 1], 'sddl': 'private',
                'content': base64.b64encode(raw).decode()}

    assert install._authorized_windows_setup_config_evolution(
        snapshot(managed), snapshot(bound), installation_id='install'
    ) is True
    assert install._authorized_windows_setup_config_evolution(
        snapshot(managed), snapshot(replace(bound, origin='http://127.0.0.1:8766')), installation_id='install'
    ) is False


def test_windows_committed_rollback_requires_exact_transaction_identity(tmp_path, monkeypatch):
    import base64
    import uuid
    from types import SimpleNamespace
    import factlane.webui_install as install
    backend = object.__new__(install.ProductionWindowsInstallerBackend)
    backend.request = SimpleNamespace(installation_id='install', state_dir='state')
    backend._paths = SimpleNamespace(root_dir='root', owner_config_home='home', runtime_dir='runtime', state_dir='state')
    backend._validate_launcher_target = lambda: None
    backend._targets = lambda: {'WRITE_SERVICE_CONFIG': 'config'}
    transaction_id = str(uuid.uuid4())
    journal = {'transaction_id': transaction_id, 'phase': 'COMMITTED', 'operations': []}
    custody = {'exists': True, 'identity': [1, 1], 'sddl': 'private',
               'content': base64.b64encode(b'{}').decode()}
    monkeypatch.setattr(install.windows, 'current_user_sid', lambda: 'owner')
    monkeypatch.setattr(install.windows, 'windows_owner_paths', lambda **kwargs: backend._paths)
    monkeypatch.setattr(install, 'validate_windows_path_no_reparse', lambda path: tmp_path)
    monkeypatch.setattr(install.windows, 'validate_owner_private_path', lambda *args, **kwargs: None)
    monkeypatch.setattr(install.windows, 'snapshot_windows_setup_file', lambda *args, **kwargs: custody)
    monkeypatch.setattr(install, '_decode_windows_setup_journal', lambda *args, **kwargs: journal)
    calls = []
    backend.rollback_setup = lambda *, plan, completed_operations: calls.append((plan, completed_operations)) or InstallerStepResult(
        status=InstallerStepStatus.APPLIED,
        operation='ROLLBACK',
        detail='exact owned state restored',
    )

    result = backend.rollback_committed_setup(transaction_id)
    assert result.status is InstallerStepStatus.APPLIED
    assert calls == [(None, ())]

    calls.clear()
    with pytest.raises(install.WindowsWebUIError) as raised:
        backend.rollback_committed_setup(str(uuid.uuid4()))
    assert raised.value.code == 'WINDOWS_SETUP_TRANSACTION_MISMATCH'
    assert calls == []


def test_windows_task_snapshot_recognizes_wrapped_com_not_found():
    from types import SimpleNamespace
    import factlane.webui_windows as windows
    supervisor = object.__new__(windows.WindowsTaskSchedulerSupervisor)
    supervisor.task_name = windows.WINDOWS_TASK_NAME
    class ComFailure(Exception):
        hresult = -2147352567  # DISP_E_EXCEPTION from IDispatch
        excepinfo = (0, 'Task Scheduler', 'not found', None, 0, -2147024894)
    def get_task(name):
        raise ComFailure()
    service = SimpleNamespace(GetFolder=lambda path: SimpleNamespace(GetTask=get_task))
    supervisor._run_with_service = lambda callback: callback(service)
    assert supervisor.snapshot_task() == {'exists': False}


def test_windows_recovery_blocks_live_service_even_when_task_is_quiescent(monkeypatch):
    import base64
    from types import SimpleNamespace
    import factlane.webui_install as install
    backend = object.__new__(install.ProductionWindowsInstallerBackend)
    backend.request = SimpleNamespace(state_dir='state')
    backend._owner_sid = 'owner'
    backend.supervisor = SimpleNamespace(verify_quiescent=lambda: None)
    raw = base64.b64encode(json.dumps({'status': 'READY'}).encode()).decode()
    monkeypatch.setattr(install.windows, 'snapshot_windows_setup_file', lambda *args, **kwargs: {'exists': True, 'content': raw})
    with pytest.raises(install.WindowsWebUIError, match='quiescent'):
        backend._assert_recovery_quiescent()
