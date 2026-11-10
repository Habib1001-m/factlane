from __future__ import annotations

import json
import io
import os
import socket
import struct
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import pytest

if sys.platform != "linux":
    pytest.skip(
        "managed Owner WebUI production-platform qualification is Linux-only",
        allow_module_level=True,
    )

import factlane.webui_app as webui_app
import factlane.webui_platform as webui_platform_module
from factlane.owner_auth import NativeConfirmationRequest, OwnerAuthState, PendingEnrollment
from factlane.owner_native_control import OwnerNativeControlServer
from factlane.webui_install import (
    BrowserInstall,
    EndpointState,
    InstallEnvironment,
    InstallerStepStatus,
    SetupConsent,
    apply_webui_setup,
    linux_setup_journal_path,
    plan_webui_setup,
)
from factlane.webui_platform import (
    NativeLauncherClient,
    ProductionWebUIController,
    SystemdUnitObservation,
    SystemdUserUnitManager,
    WebUIPlatformError,
    ZenitySetupConsentProvider,
    build_default_setup_executor,
    build_default_setup_request,
    build_webui_doctor_report,
    default_owner_paths,
    detect_lingering_required,
)
from factlane.webui_service import ServiceStatus, WebUIServiceState
from factlane.webui_runtime import (
    BackendTransportKind,
    PlatformKind,
    RuntimeOutcome,
    RuntimeOutcomeState,
    WebUIRuntimeConfig,
)
from factlane.webui_windows import WindowsManagedConfig


class _Confirmation:
    def confirm(self, request: NativeConfirmationRequest) -> bool:
        del request
        return True


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def status(self):
        self.calls.append(("status", None))
        return {
            "status": "READY",
            "saved_endpoint": "https://factlane-test123.test:8765",
        }

    def start(self, request_id: str):
        self.calls.append(("start", request_id))
        return {"status": "STARTING", "request_id": request_id}

    def stop(self, request_id: str):
        self.calls.append(("stop", request_id))
        return {"status": "STOPPED_UNTIL_REBOOT", "request_id": request_id}

    def restart(self, request_id: str):
        self.calls.append(("restart", request_id))
        return {"status": "STARTING", "request_id": request_id}


def _write_runtime_config(tmp_path: Path) -> tuple[Path, WebUIRuntimeConfig]:
    config_home = tmp_path / "config"
    runtime_dir = tmp_path / "runtime" / "factlane"
    state_dir = tmp_path / "state" / "factlane"
    config = WebUIRuntimeConfig(
        db=None,
        profile=None,
        host_id=None,
        origin="https://factlane-test123.test:8765",
        backend_socket=str(runtime_dir / "owner-http.sock"),
        caddy_path="/usr/bin/caddy",
        caddyfile=str(config_home / "factlane" / "Caddyfile"),
        runtime_dir=str(runtime_dir),
        state_dir=str(state_dir),
    )
    path = config_home / "factlane" / "webui.json"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(json.dumps(asdict(config)), encoding="utf-8")
    path.chmod(0o600)
    return path, config


def test_default_owner_paths_follow_absolute_xdg_roots_without_creating_them(tmp_path: Path) -> None:
    config = tmp_path / "cfg"
    runtime = tmp_path / "run"
    state = tmp_path / "state"

    paths = default_owner_paths(
        environ={
            "XDG_CONFIG_HOME": str(config),
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_STATE_HOME": str(state),
        },
        home=str(tmp_path / "home"),
        owner_uid=1234,
    )

    assert paths.owner_config_home == str(config)
    assert paths.service_config_path == str(config / "factlane" / "webui.json")
    assert paths.caddy_config_path == str(config / "factlane" / "Caddyfile")
    assert paths.user_unit_path == str(config / "systemd" / "user" / "factlane-webui.service")
    assert paths.runtime_dir == str(runtime / "factlane")
    assert paths.backend_socket == str(runtime / "factlane" / "owner-webui" / "owner-http.sock")
    assert paths.native_control_socket == str(runtime / "factlane" / "owner-webui" / "owner-control.sock")
    assert paths.state_dir == str(state / "factlane")
    assert paths.caddy_state_dir == str(state / "factlane" / "caddy")
    assert paths.browser_profile_dir == str(state / "factlane" / "browser")
    assert not config.exists()
    assert not runtime.exists()
    assert not state.exists()


def test_default_owner_paths_use_home_fallbacks_and_standard_user_runtime(tmp_path: Path) -> None:
    home = tmp_path / "home"

    paths = default_owner_paths(environ={}, home=str(home), owner_uid=4321)

    assert paths.owner_config_home == str(home / ".config")
    assert paths.service_config_path == str(home / ".config" / "factlane" / "webui.json")
    assert paths.runtime_dir == "/run/user/4321/factlane"
    assert paths.backend_socket == "/run/user/4321/factlane/owner-webui/owner-http.sock"
    assert paths.native_control_socket == "/run/user/4321/factlane/owner-webui/owner-control.sock"
    assert paths.state_dir == str(home / ".local" / "state" / "factlane")


def test_doctor_configured_report_exposes_only_read_only_onboarding_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _config = _write_runtime_config(tmp_path)
    database = tmp_path / "memory.db"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "db": str(database),
            "profile": "nomic-768",
            "host_id": "owner-panel-host",
        }
    )
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    config_path.chmod(0o600)

    executable = tmp_path / "bin" / "factlane"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher = tmp_path / "data" / "applications" / "FactLane Owner Panel.desktop"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("[Desktop Entry]\n", encoding="utf-8")
    skill_root = tmp_path / "skill" / "using-factlane"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Using FactLane\n", encoding="utf-8")
    monkeypatch.setattr(
        webui_platform_module,
        "_discover_packaged_skill_root",
        lambda: skill_root,
    )

    observations: list[str] = []

    class ReadOnlyManager:
        def observe(self) -> SystemdUnitObservation:
            observations.append("observe")
            return SystemdUnitObservation(
                active=True,
                enabled=True,
                active_state="active",
                enabled_state="enabled",
            )

        def start(self) -> None:
            pytest.fail("doctor must never start the managed service")

    def snapshot() -> dict[str, bytes]:
        return {
            str(path.relative_to(tmp_path)): path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file()
        }

    before = snapshot()
    report = build_webui_doctor_report(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        home=str(tmp_path / "home"),
        owner_uid=os.getuid(),
        factlane_executable=str(executable),
        python_executable=sys.executable,
        service_manager=ReadOnlyManager(),
    )
    after = snapshot()

    assert before == after
    assert observations == ["observe"]
    assert report["status"] == "OK"
    assert report["mode"] == "READ_ONLY"
    assert report["factlane"]["executable"] == {
        "state": "RESOLVED",
        "path": str(executable),
        "present": True,
    }
    assert report["factlane"]["interpreter"]["state"] == "RESOLVED"
    assert report["managed_binding"] == {
        "state": "CONFIGURED",
        "db": str(database),
        "profile": "nomic-768",
    }
    assert report["skill"] == {
        "state": "PRESENT",
        "root": str(skill_root),
        "present": True,
    }
    assert report["panel_launcher"] == {
        "state": "PRESENT",
        "path": str(launcher),
        "present": True,
    }
    assert report["service"]["state"] == "ACTIVE"
    assert report["service"]["open_command"] == ["factlane", "open", "webui"]
    assert report["mcp"]["host_id"] == {
        "state": "EXPLICIT_REQUIRED",
        "value": None,
    }
    assert report["mcp"]["scope"] == {
        "state": "EXPLICIT_PER_REQUEST",
        "value": None,
    }
    recipe = report["mcp"]["launch_recipe"]["argv"]
    assert recipe == [
        str(executable),
        "--db",
        str(database),
        "--profile",
        "nomic-768",
        "--host-id",
        "<MCP_HOST_ID>",
        "--write-profile",
        "read-only",
    ]
    assert "owner-panel-host" not in json.dumps(report["mcp"])
    assert report["next_actions"][0]["id"] == "OPEN_OWNER_PANEL"


def test_doctor_unconfigured_report_uses_supported_profiles_and_creates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        webui_platform_module,
        "_discover_packaged_skill_root",
        lambda: None,
    )
    observations: list[str] = []

    class ReadOnlyManager:
        def observe(self) -> SystemdUnitObservation:
            observations.append("observe")
            return SystemdUnitObservation(
                active=False,
                enabled=False,
                active_state="inactive",
                enabled_state="disabled",
            )

        def start(self) -> None:
            pytest.fail("doctor must never start the managed service")

    config_root = tmp_path / "config"
    runtime_root = tmp_path / "runtime"
    state_root = tmp_path / "state"
    data_root = tmp_path / "data"
    report = build_webui_doctor_report(
        environ={
            "XDG_CONFIG_HOME": str(config_root),
            "XDG_RUNTIME_DIR": str(runtime_root),
            "XDG_STATE_HOME": str(state_root),
            "XDG_DATA_HOME": str(data_root),
        },
        home=str(tmp_path / "home"),
        owner_uid=os.getuid(),
        factlane_executable=str(tmp_path / "missing-factlane"),
        python_executable=sys.executable,
        service_manager=ReadOnlyManager(),
    )

    assert observations == ["observe"]
    assert report["managed_binding"] == {
        "state": "UNCONFIGURED",
        "db": None,
        "profile": None,
        "supported_profiles": sorted(webui_platform_module.PROFILE_DEFINITIONS),
    }
    assert report["panel_launcher"] == {
        "state": "ABSENT",
        "path": str(data_root / "applications" / "FactLane Owner Panel.desktop"),
        "present": False,
    }
    assert report["skill"] == {"state": "UNKNOWN", "root": None, "present": False}
    assert report["service"]["state"] == "INACTIVE"
    assert report["mcp"]["launch_recipe"]["argv"] == [
        "factlane",
        "--db",
        "<DB_PATH>",
        "--profile",
        "<PROFILE>",
        "--host-id",
        "<MCP_HOST_ID>",
        "--write-profile",
        "read-only",
    ]
    assert report["next_actions"][0]["id"] == "CONFIGURE_OWNER_PANEL_MEMORY"
    assert not config_root.exists()
    assert not runtime_root.exists()
    assert not state_root.exists()
    assert not data_root.exists()


def test_doctor_resolves_relative_console_script_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "venv" / "bin" / "factlane"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["venv/bin/factlane", "doctor", "webui"])
    monkeypatch.setattr(webui_platform_module.shutil, "which", lambda _name: None)

    identity = webui_platform_module._doctor_executable_identity(None)

    assert identity == {
        "state": "RESOLVED",
        "path": str(executable.resolve()),
        "present": True,
    }


def test_first_run_setup_request_uses_owner_paths_and_absent_memory_trio(tmp_path: Path) -> None:
    config = tmp_path / "config"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"

    request = build_default_setup_request(
        environ={
            "XDG_CONFIG_HOME": str(config),
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_STATE_HOME": str(state),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        home=str(tmp_path / "home"),
        owner_uid=1000,
        installing_user="alice",
        factlane_executable="/home/alice/.local/bin/factlane",
        installation_id_factory=lambda: "newinstall123",
        lingering_detector=lambda user: user == "alice",
    )

    assert request.installation_id == "newinstall123"
    assert request.saved_port == 8765
    assert request.owner_config_home == str(config)
    assert request.runtime_dir == str(runtime / "factlane")
    assert request.backend_socket == str(runtime / "factlane" / "owner-webui" / "owner-http.sock")
    assert request.state_dir == str(state / "factlane")
    assert request.launcher_path == str(
        tmp_path / "data" / "applications" / "FactLane Owner Panel.desktop"
    )
    assert request.lingering_required is True
    assert (request.db, request.profile, request.host_id) == (None, None, None)
    assert not config.exists()
    assert not runtime.exists()
    assert not state.exists()


def test_existing_owner_private_runtime_config_reuses_persistent_origin_and_memory_identity(
    tmp_path: Path,
) -> None:
    path, _config = _write_runtime_config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["backend_socket"] = str(tmp_path / "runtime" / "factlane" / "owner-http.sock")
    payload.update(
        {
            "db": str(tmp_path / "memories.db"),
            "profile": "nomic-768",
            "host_id": "owner-a",
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)

    request = build_default_setup_request(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        home=str(tmp_path / "home"),
        owner_uid=os.getuid(),
        installing_user="alice",
        factlane_executable="/home/alice/.local/bin/factlane",
        installation_id_factory=lambda: "must-not-be-used",
        lingering_detector=lambda _user: False,
    )

    assert request.installation_id == "test123"
    assert request.saved_port == 8765
    assert request.db == str(tmp_path / "memories.db")
    assert request.profile == "nomic-768"
    assert request.host_id == "owner-a"
    assert request.backend_socket == str(
        tmp_path / "runtime" / "factlane" / "owner-webui" / "owner-http.sock"
    )
    assert request.lingering_required is False


def test_browser_neutral_runtime_config_reuses_installation_id_from_claimed_setup_journal(
    tmp_path: Path,
) -> None:
    owner_uid = os.getuid()
    paths = default_owner_paths(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        home=str(tmp_path / "home"),
        owner_uid=owner_uid,
    )
    config_path = Path(paths.service_config_path)
    config_path.parent.mkdir(parents=True, mode=0o700)
    config_path.write_text(
        json.dumps(
            {
                "db": str(tmp_path / "memories.db"),
                "profile": "nomic-768",
                "host_id": "owner-a",
                "origin": "http://127.0.0.1:8765",
                "backend_socket": paths.backend_socket,
                "caddy_path": "/usr/bin/caddy",
                "caddyfile": paths.caddy_config_path,
                "runtime_dir": paths.runtime_dir,
                "state_dir": paths.state_dir,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    journal_path = linux_setup_journal_path(paths.state_dir)
    journal_path.parent.mkdir(parents=True, mode=0o700)
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "phase": "CLAIMED",
                "installation_id": "test123",
                "hostname": "factlane-test123.test",
                "installing_user": "alice",
                "browser_family": None,
                "browser_executable": None,
                "browser_profile_dir": paths.browser_profile_dir,
                "state_dir": paths.state_dir,
                "caddy_state_dir": paths.caddy_state_dir,
                "root_fingerprint_sha256": None,
                "ownership": {},
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    journal_path.chmod(0o600)

    request = build_default_setup_request(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        home=str(tmp_path / "home"),
        owner_uid=owner_uid,
        installing_user="alice",
        factlane_executable="/home/alice/.local/bin/factlane",
        installation_id_factory=lambda: "must-not-be-used",
        lingering_detector=lambda _user: False,
    )

    assert request.installation_id == "test123"
    assert request.saved_port == 8765
    assert request.db == str(tmp_path / "memories.db")
    assert request.profile == "nomic-768"
    assert request.host_id == "owner-a"


def test_zenity_setup_consent_uses_one_fixed_argv_review_and_never_auto_consents() -> None:
    calls: list[tuple[str, ...]] = []
    request = build_default_setup_request(
        environ={
            "XDG_CONFIG_HOME": "/tmp/factlane-test-config",
            "XDG_RUNTIME_DIR": "/tmp/factlane-test-runtime",
            "XDG_STATE_HOME": "/tmp/factlane-test-state",
        },
        home="/tmp/factlane-test-home",
        owner_uid=1000,
        installing_user="alice",
        factlane_executable="/usr/bin/factlane",
        installation_id_factory=lambda: "consent123",
        lingering_detector=lambda _user: True,
    )
    environment = InstallEnvironment(
        system="Linux",
        systemd=True,
        caddy_path="/usr/bin/caddy",
        zenity_path="/usr/bin/zenity",
        browser=BrowserInstall(family="firefox", executable="/usr/bin/firefox"),
        euid=1000,
    )
    provider = ZenitySetupConsentProvider(
        "/usr/bin/zenity",
        command_runner=lambda argv: calls.append(argv) or 0,
    )

    consent = provider.acquire(request, environment)

    assert consent == SetupConsent(
        hostname_mapping=False,
        browser_profile_trust=False,
        user_lingering=True,
    )
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0:3] == (
        "/usr/bin/zenity",
        "--question",
        "--title=FactLane WebUI setup",
    )
    assert argv[-2:] == (
        "--ok-label=Apply reviewed changes",
        "--cancel-label=Cancel",
    )
    text_arg = next(arg for arg in argv if arg.startswith("--text="))
    assert "user lingering" in text_arg
    assert "Firefox" not in text_arg
    assert "No browser profile" in text_arg
    assert "browser trust store" in text_arg
    assert all(token not in argv for token in ("sudo", "pkexec", "certutil"))


@pytest.mark.parametrize("failure", ["denied", "closed", "runner-error", "path-drift"])
def test_zenity_setup_consent_denial_close_failure_or_helper_drift_fails_closed(
    failure: str,
) -> None:
    request = build_default_setup_request(
        environ={
            "XDG_CONFIG_HOME": "/tmp/factlane-test-config",
            "XDG_RUNTIME_DIR": "/tmp/factlane-test-runtime",
            "XDG_STATE_HOME": "/tmp/factlane-test-state",
        },
        home="/tmp/factlane-test-home",
        owner_uid=1000,
        installing_user="alice",
        factlane_executable="/usr/bin/factlane",
        installation_id_factory=lambda: "consent123",
        lingering_detector=lambda _user: True,
    )
    environment = InstallEnvironment(
        system="Linux",
        systemd=True,
        caddy_path="/usr/bin/caddy",
        zenity_path="/opt/zenity" if failure == "path-drift" else "/usr/bin/zenity",
        browser=BrowserInstall(family="firefox", executable="/usr/bin/firefox"),
        euid=1000,
    )

    def runner(_argv: tuple[str, ...]) -> int:
        if failure == "runner-error":
            raise OSError("dialog unavailable")
        return 1 if failure in {"denied", "closed"} else 0

    provider = ZenitySetupConsentProvider("/usr/bin/zenity", command_runner=runner)

    assert provider.acquire(request, environment) == SetupConsent(
        hostname_mapping=False,
        browser_profile_trust=False,
        user_lingering=False,
    )


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected_required"),
    [
        (0, "yes\n", False),
        (0, "no\n", True),
        (0, "unknown\n", True),
        (1, "", True),
    ],
)
def test_lingering_detection_is_read_only_and_conservative(
    returncode: int,
    stdout: str,
    expected_required: bool,
) -> None:
    calls: list[tuple[str, ...]] = []

    required = detect_lingering_required(
        "alice",
        which=lambda name: "/usr/bin/loginctl" if name == "loginctl" else None,
        command_runner=lambda argv: calls.append(argv) or (returncode, stdout),
    )

    assert required is expected_required
    assert calls == [
        (
            "/usr/bin/loginctl",
            "show-user",
            "--property=Linger",
            "--value",
            "--",
            "alice",
        )
    ]


def test_lingering_detection_missing_or_failed_probe_requires_change_conservatively() -> None:
    assert detect_lingering_required(
        "alice",
        which=lambda _name: None,
        command_runner=lambda _argv: pytest.fail("runner must not be called"),
    )

    def failed(_argv: tuple[str, ...]) -> tuple[int, str]:
        raise OSError("loginctl unavailable")

    assert detect_lingering_required(
        "alice",
        which=lambda _name: "/usr/bin/loginctl",
        command_runner=failed,
    )


def test_default_setup_executor_fails_closed_when_required_linux_setup_tooling_is_missing(
    tmp_path: Path,
) -> None:
    request = build_default_setup_request(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        home=str(tmp_path / "home"),
        owner_uid=1000,
        installing_user="alice",
        factlane_executable="/usr/bin/factlane",
        installation_id_factory=lambda: "executor123",
        lingering_detector=lambda _user: False,
    )
    environment = InstallEnvironment(
        system="Linux",
        systemd=True,
        caddy_path="/usr/bin/caddy",
        zenity_path="/usr/bin/zenity",
        browser=BrowserInstall(family="firefox", executable="/usr/bin/firefox"),
        euid=1000,
    )
    consent = SetupConsent(
        hostname_mapping=True,
        browser_profile_trust=True,
        user_lingering=False,
    )
    receipt = plan_webui_setup(request, environment, consent)
    commands: list[tuple[str, ...]] = []
    state_reads: list[bool] = []
    executor = build_default_setup_executor(
        request,
        environment,
        which=lambda _name: None,
        command_runner=lambda argv: commands.append(argv) or 0,
        service_state_reader=lambda: state_reads.append(True)
        or pytest.fail("state reader must not run when setup tooling is unavailable"),
    )

    result = executor(receipt, consent)

    assert result.status is InstallerStepStatus.FAILED
    assert result.endpoint_state is EndpointState.PROPOSED
    assert result.completed_operations == ()
    assert result.detail == "required Linux managed-setup system tooling is unavailable"
    assert commands == []
    assert state_reads == []


def test_default_setup_executor_reloads_service_state_for_each_activation_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = build_default_setup_request(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        home=str(tmp_path / "home"),
        owner_uid=os.getuid(),
        installing_user="alice",
        factlane_executable="/usr/bin/factlane",
        installation_id_factory=lambda: "freshstate123",
        lingering_detector=lambda _user: False,
    )
    environment = InstallEnvironment(
        system="Linux",
        systemd=True,
        caddy_path="/usr/bin/caddy",
        zenity_path="/usr/bin/zenity",
        browser=BrowserInstall(family="firefox", executable="/usr/bin/firefox"),
        euid=os.getuid(),
    )
    captured: dict[str, object] = {}

    class Backend:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("factlane.webui_platform.ProductionLinuxInstallerBackend", Backend)
    factory_calls: list[str] = []

    class State:
        def __init__(self, value: int) -> None:
            self.value = value

        def snapshot(self) -> int:
            return self.value

    def service_state_factory(path: str) -> State:
        factory_calls.append(path)
        return State(len(factory_calls))

    build_default_setup_executor(
        request,
        environment,
        which=lambda name: (
            f"/usr/bin/{name}" if name in {"systemctl", "certutil"} else None
        ),
        service_state_factory=service_state_factory,
    )
    reader = captured["service_state_reader"]
    assert callable(reader)
    assert reader() == 1
    assert reader() == 2
    expected_state_path = str(Path(request.state_dir) / "webui-service.json")
    assert factory_calls == [expected_state_path, expected_state_path]


def test_native_launcher_client_round_trips_worker4_same_uid_socket(tmp_path: Path) -> None:
    service = _Service()
    auth = OwnerAuthState(owner_uid=os.getuid(), native_confirmation=_Confirmation())
    server = OwnerNativeControlServer(
        socket_path=tmp_path / "native" / "owner-control.sock",
        owner_uid=os.getuid(),
        auth_state=auth,
        service_control=service,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = NativeLauncherClient(str(server.socket_path), owner_uid=os.getuid())
        assert client.status()["saved_endpoint"].endswith(":8765")
        enrollment = client.begin_local_enrollment(
            device_label="FactLane local browser"
        )
        assert enrollment.locator
        assert enrollment.verification_phrase
        assert enrollment.expires_at
        with pytest.raises(WebUIPlatformError) as exc_info:
            client.approve_pending(locator=enrollment.locator)
        assert exc_info.value.code == "PENDING_NOT_CLAIMED"

        preauth = auth.issue_preauth(origin="https://owner.test")
        auth.claim_pending(
            locator=enrollment.locator,
            origin="https://owner.test",
            preauth_secret=preauth,
        )
        client.approve_pending(locator=enrollment.locator)
        credentials = auth.poll_pending(
            locator=enrollment.locator,
            origin="https://owner.test",
            preauth_secret=preauth,
        )
        assert credentials is not None
        auth.begin_sensitive_challenge(
            session_secret=credentials.session_secret,
            origin="https://owner.test",
            preview="Exact sensitive preview",
        )
        assert client.approve_pending_sensitive() is None
        assert client.start(request_id="start-1")["request_id"] == "start-1"
        assert client.stop(request_id="stop-1")["request_id"] == "stop-1"
        assert client.restart(request_id="restart-1")["request_id"] == "restart-1"
        assert service.calls == [
            ("status", None),
            ("start", "start-1"),
            ("stop", "stop-1"),
            ("restart", "restart-1"),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_native_launcher_regular_approval_uses_exact_request_and_human_confirmation_response_timeout() -> None:
    response = json.dumps(
        {"status": "OK", "locator": "locator-approval"},
        separators=(",", ":"),
    ).encode("utf-8")
    framed = struct.pack("!I", len(response)) + response

    class FakeSocket:
        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.sent: list[bytes] = []
            self.remaining = framed

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def connect(self, _path: str) -> None:
            return None

        def sendall(self, payload: bytes) -> None:
            self.sent.append(payload)

        def recv(self, length: int) -> bytes:
            chunk, self.remaining = self.remaining[:length], self.remaining[length:]
            return chunk

        def close(self) -> None:
            return None

    sock = FakeSocket()
    client = NativeLauncherClient(
        "/unused/owner-control.sock",
        owner_uid=os.getuid(),
        timeout_seconds=3.0,
        socket_factory=lambda _family, _kind: sock,
    )
    client._validate_socket_path = lambda: None  # type: ignore[method-assign]
    client._validate_server_peer = lambda _sock: None  # type: ignore[method-assign]

    assert client.approve_pending(locator="locator-approval") is None
    assert sock.timeouts == [3.0, 125.0]
    assert len(sock.sent) == 1
    length = struct.unpack("!I", sock.sent[0][:4])[0]
    assert json.loads(sock.sent[0][4 : 4 + length]) == {
        "op": "APPROVE_PENDING",
        "locator": "locator-approval",
    }


def test_native_launcher_sensitive_op_uses_exact_request_and_only_widens_response_timeout() -> None:
    response = json.dumps(
        {"status": "OK", "state": "SENSITIVE_APPROVED"},
        separators=(",", ":"),
    ).encode("utf-8")
    framed = struct.pack("!I", len(response)) + response

    class FakeSocket:
        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.sent: list[bytes] = []
            self.remaining = framed

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def connect(self, _path: str) -> None:
            return None

        def sendall(self, payload: bytes) -> None:
            self.sent.append(payload)

        def recv(self, length: int) -> bytes:
            chunk, self.remaining = self.remaining[:length], self.remaining[length:]
            return chunk

        def close(self) -> None:
            return None

    sock = FakeSocket()
    client = NativeLauncherClient(
        "/unused/owner-control.sock",
        owner_uid=os.getuid(),
        timeout_seconds=3.0,
        socket_factory=lambda _family, _kind: sock,
    )
    client._validate_socket_path = lambda: None  # type: ignore[method-assign]
    client._validate_server_peer = lambda _sock: None  # type: ignore[method-assign]

    assert client.approve_pending_sensitive() is None
    assert sock.timeouts == [3.0, 125.0]
    assert len(sock.sent) == 1
    length = struct.unpack("!I", sock.sent[0][:4])[0]
    assert json.loads(sock.sent[0][4 : 4 + length]) == {"op": "APPROVE_PENDING_SENSITIVE"}


def test_native_launcher_sensitive_op_rejects_nonexact_success_and_never_retries() -> None:
    response = json.dumps(
        {"status": "OK", "state": "SENSITIVE_APPROVED", "challenge_id": "must-not-cross"},
        separators=(",", ":"),
    ).encode("utf-8")
    framed = struct.pack("!I", len(response)) + response

    class FakeSocket:
        def __init__(self) -> None:
            self.remaining = framed
            self.sent = 0

        def settimeout(self, _timeout: float) -> None:
            return None

        def connect(self, _path: str) -> None:
            return None

        def sendall(self, _payload: bytes) -> None:
            self.sent += 1

        def recv(self, length: int) -> bytes:
            chunk, self.remaining = self.remaining[:length], self.remaining[length:]
            return chunk

        def close(self) -> None:
            return None

    sock = FakeSocket()
    client = NativeLauncherClient(
        "/unused/owner-control.sock",
        owner_uid=os.getuid(),
        socket_factory=lambda _family, _kind: sock,
    )
    client._validate_socket_path = lambda: None  # type: ignore[method-assign]
    client._validate_server_peer = lambda _sock: None  # type: ignore[method-assign]

    with pytest.raises(WebUIPlatformError) as exc_info:
        client.approve_pending_sensitive()
    assert exc_info.value.code == "CONTROL_RESPONSE_INVALID"
    assert sock.sent == 1


def test_native_launcher_sensitive_timeout_never_retries_and_normal_ops_keep_short_timeout() -> None:
    status_response = json.dumps(
        {"status": "OK", "service": {"state": "READY"}},
        separators=(",", ":"),
    ).encode("utf-8")
    status_frame = struct.pack("!I", len(status_response)) + status_response

    class StatusSocket:
        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.remaining = status_frame

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def connect(self, _path: str) -> None:
            return None

        def sendall(self, _payload: bytes) -> None:
            return None

        def recv(self, length: int) -> bytes:
            chunk, self.remaining = self.remaining[:length], self.remaining[length:]
            return chunk

        def close(self) -> None:
            return None

    normal_sock = StatusSocket()
    normal_client = NativeLauncherClient(
        "/unused/owner-control.sock",
        owner_uid=os.getuid(),
        timeout_seconds=4.0,
        socket_factory=lambda _family, _kind: normal_sock,
    )
    normal_client._validate_socket_path = lambda: None  # type: ignore[method-assign]
    normal_client._validate_server_peer = lambda _sock: None  # type: ignore[method-assign]
    assert normal_client.status() == {"state": "READY"}
    assert normal_sock.timeouts == [4.0]

    class TimeoutSocket:
        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.sent = 0

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def connect(self, _path: str) -> None:
            return None

        def sendall(self, _payload: bytes) -> None:
            self.sent += 1

        def recv(self, _length: int) -> bytes:
            raise socket.timeout("human confirmation response timed out")

        def close(self) -> None:
            return None

    regular_sock = TimeoutSocket()
    regular_client = NativeLauncherClient(
        "/unused/owner-control.sock",
        owner_uid=os.getuid(),
        timeout_seconds=4.0,
        socket_factory=lambda _family, _kind: regular_sock,
    )
    regular_client._validate_socket_path = lambda: None  # type: ignore[method-assign]
    regular_client._validate_server_peer = lambda _sock: None  # type: ignore[method-assign]
    with pytest.raises(WebUIPlatformError) as regular_exc:
        regular_client.approve_pending(locator="locator-timeout")
    assert regular_exc.value.code == "CONTROL_UNAVAILABLE"
    assert regular_sock.sent == 1
    assert regular_sock.timeouts == [4.0, 125.0]

    sensitive_sock = TimeoutSocket()
    sensitive_client = NativeLauncherClient(
        "/unused/owner-control.sock",
        owner_uid=os.getuid(),
        timeout_seconds=4.0,
        socket_factory=lambda _family, _kind: sensitive_sock,
    )
    sensitive_client._validate_socket_path = lambda: None  # type: ignore[method-assign]
    sensitive_client._validate_server_peer = lambda _sock: None  # type: ignore[method-assign]
    with pytest.raises(WebUIPlatformError) as exc_info:
        sensitive_client.approve_pending_sensitive()
    assert exc_info.value.code == "CONTROL_UNAVAILABLE"
    assert sensitive_sock.sent == 1
    assert sensitive_sock.timeouts == [4.0, 125.0]


def test_native_launcher_all_ordinary_ops_keep_short_response_timeout() -> None:
    cases = [
        ("status", {}, "SERVICE_STATUS", {"status": "OK", "service": {"state": "READY"}}),
        ("start", {"request_id": "req-start"}, "SERVICE_START", {"status": "OK", "service": {"state": "STARTING"}}),
        ("stop", {"request_id": "req-stop"}, "SERVICE_STOP", {"status": "OK", "service": {"state": "STOPPED"}}),
        ("restart", {"request_id": "req-restart"}, "SERVICE_RESTART", {"status": "OK", "service": {"state": "STARTING"}}),
        (
            "begin_local_enrollment",
            {"device_label": "FactLane local browser"},
            "BEGIN_LOCAL_ENROLLMENT",
            {
                "status": "OK",
                "locator": "ordinary-locator",
                "verification_phrase": "amber cedar river willow zephyr",
                "expires_at": 12345.0,
            },
        ),
    ]

    for method_name, kwargs, expected_op, response in cases:
        payload = json.dumps(response, separators=(",", ":")).encode("utf-8")
        framed = struct.pack("!I", len(payload)) + payload

        class OrdinarySocket:
            def __init__(self) -> None:
                self.timeouts: list[float] = []
                self.sent: list[bytes] = []
                self.remaining = framed

            def settimeout(self, timeout: float) -> None:
                self.timeouts.append(timeout)

            def connect(self, _path: str) -> None:
                return None

            def sendall(self, data: bytes) -> None:
                self.sent.append(data)

            def recv(self, length: int) -> bytes:
                chunk, self.remaining = self.remaining[:length], self.remaining[length:]
                return chunk

            def close(self) -> None:
                return None

        sock = OrdinarySocket()
        client = NativeLauncherClient(
            "/unused/owner-control.sock",
            owner_uid=os.getuid(),
            timeout_seconds=4.0,
            socket_factory=lambda _family, _kind, sock=sock: sock,
        )
        client._validate_socket_path = lambda: None  # type: ignore[method-assign]
        client._validate_server_peer = lambda _sock: None  # type: ignore[method-assign]

        getattr(client, method_name)(**kwargs)

        assert sock.timeouts == [4.0], method_name
        assert len(sock.sent) == 1, method_name
        request_length = struct.unpack("!I", sock.sent[0][:4])[0]
        request = json.loads(sock.sent[0][4 : 4 + request_length])
        assert request["op"] == expected_op, method_name


def test_native_launcher_client_rejects_non_private_socket_before_connect(tmp_path: Path) -> None:
    path = tmp_path / "owner-control.sock"
    path.write_text("not-a-socket", encoding="utf-8")

    client = NativeLauncherClient(str(path), owner_uid=os.getuid())
    with pytest.raises(WebUIPlatformError, match="socket"):
        client.status()


def test_systemd_user_manager_uses_only_fixed_exact_unit_argv() -> None:
    calls: list[tuple[str, ...]] = []
    responses = {
        (
            "/usr/bin/systemctl",
            "--user",
            "is-active",
            "factlane-webui.service",
        ): (3, "inactive\n"),
        (
            "/usr/bin/systemctl",
            "--user",
            "is-enabled",
            "factlane-webui.service",
        ): (0, "enabled\n"),
        (
            "/usr/bin/systemctl",
            "--user",
            "start",
            "factlane-webui.service",
        ): (0, ""),
    }
    manager = SystemdUserUnitManager(
        "/usr/bin/systemctl",
        command_runner=lambda argv: calls.append(argv) or responses[argv],
    )

    observed = manager.observe()
    manager.start()

    assert observed == SystemdUnitObservation(
        active=False,
        enabled=True,
        active_state="inactive",
        enabled_state="enabled",
    )
    assert calls == list(responses)


def _persistent_service_state(tmp_path: Path) -> WebUIServiceState:
    return WebUIServiceState(
        tmp_path / "state" / "webui-service.json",
        boot_id=lambda: "boot-test",
    )


class _UnitManager:
    def __init__(
        self,
        *,
        active: bool,
        enabled: bool = True,
        on_start=None,
    ) -> None:
        self.active = active
        self.enabled = enabled
        self.on_start = on_start
        self.starts = 0
        self.observations = 0

    def observe(self) -> SystemdUnitObservation:
        self.observations += 1
        return SystemdUnitObservation(
            active=self.active,
            enabled=self.enabled,
            active_state="active" if self.active else "inactive",
            enabled_state="enabled" if self.enabled else "disabled",
        )

    def start(self) -> None:
        self.starts += 1
        if self.on_start is not None:
            self.on_start()
        self.active = True


def test_offline_status_combines_unit_truth_with_persistent_service_snapshot(
    tmp_path: Path,
) -> None:
    service_state = _persistent_service_state(tmp_path)
    service_state.claim_endpoint("https://factlane-test123.test:8765")
    stopped = service_state.request_stop("stop-before-status")
    assert stopped.status is ServiceStatus.STOPPED_UNTIL_REBOOT
    unit = _UnitManager(active=False, enabled=True)
    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={
                "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
                "XDG_STATE_HOME": str(tmp_path / "state-root"),
            },
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: service_state,
        systemd_manager=unit,
        control_client_factory=lambda _path, _uid: pytest.fail(
            "offline status must not require native control"
        ),
    )

    result = controller.dispatch("status")

    assert result["status"] == "OK"
    assert result["unit"] == {
        "active": False,
        "enabled": True,
        "active_state": "inactive",
        "enabled_state": "enabled",
    }
    assert result["service"]["status"] == "STOPPED_UNTIL_REBOOT"
    assert result["service"]["saved_endpoint"] == "https://factlane-test123.test:8765"


def test_offline_start_records_start_intent_before_exact_unit_start_and_waits_ready(
    tmp_path: Path,
) -> None:
    service_state = _persistent_service_state(tmp_path)
    service_state.claim_endpoint("https://factlane-test123.test:8765")
    service_state.request_stop("stop-before-start")
    order: list[str] = []

    def on_start() -> None:
        snapshot = service_state.snapshot()
        assert snapshot.status is ServiceStatus.STARTING
        assert snapshot.stopped_boot_id is None
        order.append("systemd-start")
        service_state.mark_ready()

    unit = _UnitManager(active=False, on_start=on_start)
    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_STATE_HOME": str(tmp_path / "state-root")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: service_state,
        systemd_manager=unit,
        control_client_factory=lambda _path, _uid: pytest.fail(
            "offline start must not require native control"
        ),
        request_id_factory=lambda: "start-fixed",
        sleep=lambda _seconds: pytest.fail("ready state should be immediate"),
    )

    result = controller.dispatch("start")

    assert result["status"] == "OK"
    assert result["service"]["status"] == "READY"
    assert result["service"]["saved_endpoint"] == "https://factlane-test123.test:8765"
    assert result["unit"]["active"] is True
    assert unit.starts == 1
    assert order == ["systemd-start"]


def test_offline_start_default_budget_allows_observed_fifteen_second_cold_start(
    tmp_path: Path,
) -> None:
    service_state = _persistent_service_state(tmp_path)
    service_state.claim_endpoint("http://127.0.0.1:8765")
    service_state.request_stop("stop-before-cold-start")
    unit = _UnitManager(active=False)

    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += seconds
            if self.value >= 15.0 and service_state.snapshot().status is ServiceStatus.STARTING:
                service_state.mark_ready()

    clock = Clock()
    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_STATE_HOME": str(tmp_path / "state-root")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: service_state,
        systemd_manager=unit,
        control_client_factory=lambda _path, _uid: pytest.fail(
            "offline start must not require native control"
        ),
        request_id_factory=lambda: "cold-start-default-budget",
        clock=clock,
        sleep=clock.sleep,
        startup_poll_interval=5.0,
    )

    result = controller.dispatch("start")

    assert result["status"] == "OK"
    assert result["service"]["status"] == "READY"
    assert result["unit"]["active"] is True
    assert unit.starts == 1
    assert clock.value == pytest.approx(15.0)


def test_offline_restart_from_stopped_uses_explicit_start_semantics(
    tmp_path: Path,
) -> None:
    service_state = _persistent_service_state(tmp_path)
    service_state.request_stop("stop-before-restart")

    def on_start() -> None:
        snapshot = service_state.snapshot()
        assert snapshot.status is ServiceStatus.STARTING
        assert snapshot.stopped_boot_id is None
        service_state.mark_ready()

    unit = _UnitManager(active=False, on_start=on_start)
    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_STATE_HOME": str(tmp_path / "state-root")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: service_state,
        systemd_manager=unit,
        control_client_factory=lambda _path, _uid: pytest.fail(
            "offline restart must not require native control"
        ),
        request_id_factory=lambda: "restart-fixed",
        sleep=lambda _seconds: pytest.fail("ready state should be immediate"),
    )

    result = controller.dispatch("restart")

    assert result["status"] == "OK"
    assert result["service"]["status"] == "READY"
    assert service_state.snapshot().stopped_boot_id is None
    assert unit.starts == 1


def test_offline_start_does_not_accept_stale_ready_snapshot_from_inactive_unit(
    tmp_path: Path,
) -> None:
    service_state = _persistent_service_state(tmp_path)
    service_state.claim_endpoint("https://factlane-test123.test:8765")
    service_state.mark_ready()
    observed_before_systemd: list[ServiceStatus] = []

    def on_start() -> None:
        observed_before_systemd.append(service_state.snapshot().status)
        service_state.mark_ready()

    unit = _UnitManager(active=False, on_start=on_start)
    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_STATE_HOME": str(tmp_path / "state-root")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: service_state,
        systemd_manager=unit,
        control_client_factory=lambda _path, _uid: pytest.fail(
            "offline start must not require native control"
        ),
        request_id_factory=lambda: "stale-ready-start",
        sleep=lambda _seconds: pytest.fail("ready state should be immediate"),
    )

    result = controller.dispatch("start")

    assert result["status"] == "OK"
    assert observed_before_systemd == [ServiceStatus.STARTING]
    assert result["service"]["status"] == "READY"


def test_offline_start_accepts_recovery_required_shell_when_unit_becomes_active(
    tmp_path: Path,
) -> None:
    service_state = _persistent_service_state(tmp_path)
    service_state.mark_recovery_required()
    unit = _UnitManager(active=False)
    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_STATE_HOME": str(tmp_path / "state-root")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: service_state,
        systemd_manager=unit,
        control_client_factory=lambda _path, _uid: pytest.fail(
            "offline recovery start must not require native control"
        ),
        request_id_factory=lambda: "recovery-shell-start",
        sleep=lambda _seconds: pytest.fail("recovery state should be immediate"),
    )

    result = controller.dispatch("start")

    assert result["status"] == "OK"
    assert result["service"]["status"] == "RECOVERY_REQUIRED"
    assert result["unit"]["active"] is True


def test_offline_stop_is_idempotent_and_persists_stop_intent_without_native_socket(
    tmp_path: Path,
) -> None:
    service_state = _persistent_service_state(tmp_path)
    unit = _UnitManager(active=False)
    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_STATE_HOME": str(tmp_path / "state-root")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: service_state,
        systemd_manager=unit,
        control_client_factory=lambda _path, _uid: pytest.fail(
            "inactive stop must not require native control"
        ),
        request_id_factory=lambda: "stop-fixed",
    )

    first = controller.dispatch("stop")
    second = controller.dispatch("stop")

    assert first["status"] == "OK"
    assert second["status"] == "OK"
    assert first["service"]["status"] == "STOPPED_UNTIL_REBOOT"
    assert second["service"]["status"] == "STOPPED_UNTIL_REBOOT"
    assert unit.starts == 0


@pytest.mark.parametrize("action", ["stop", "restart"])
def test_active_stop_or_restart_keeps_native_graceful_control(
    tmp_path: Path,
    action: str,
) -> None:
    unit = _UnitManager(active=True)
    calls: list[tuple[str, str]] = []

    class Client:
        def stop(self, *, request_id: str):
            calls.append(("stop", request_id))
            return {"status": "STOPPED_UNTIL_REBOOT"}

        def restart(self, *, request_id: str):
            calls.append(("restart", request_id))
            return {"status": "STARTING"}

    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_STATE_HOME": str(tmp_path / "state-root")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: pytest.fail(
            "active graceful control must remain native-owned"
        ),
        systemd_manager=unit,
        control_client_factory=lambda _path, _uid: Client(),
        request_id_factory=lambda: "native-fixed",
    )

    result = controller.dispatch(action)

    assert result["status"] == "OK"
    assert calls == [(action, "native-fixed")]
    assert unit.starts == 0


@pytest.mark.parametrize("action", ["stop", "restart"])
def test_active_unit_with_missing_native_socket_fails_closed_without_blind_systemd_action(
    tmp_path: Path,
    action: str,
) -> None:
    unit = _UnitManager(active=True)

    class MissingClient:
        def stop(self, *, request_id: str):
            del request_id
            raise WebUIPlatformError("CONTROL_UNAVAILABLE", "native socket missing")

        def restart(self, *, request_id: str):
            del request_id
            raise WebUIPlatformError("CONTROL_UNAVAILABLE", "native socket missing")

    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_STATE_HOME": str(tmp_path / "state-root")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: pytest.fail(
            "active missing socket must not mutate persistent state offline"
        ),
        systemd_manager=unit,
        control_client_factory=lambda _path, _uid: MissingClient(),
        request_id_factory=lambda: "native-missing",
    )

    result = controller.dispatch(action)

    assert result["status"] == "ERROR"
    assert result["error_code"] == "CONTROL_UNAVAILABLE"
    assert unit.starts == 0


def test_offline_start_fails_visibly_when_unit_never_reaches_ready_or_recovery_shell(
    tmp_path: Path,
) -> None:
    service_state = _persistent_service_state(tmp_path)
    service_state.request_stop("stop-before-timeout")
    unit = _UnitManager(active=False)

    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += seconds

    clock = Clock()
    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_STATE_HOME": str(tmp_path / "state-root")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: service_state,
        systemd_manager=unit,
        control_client_factory=lambda _path, _uid: pytest.fail(
            "offline start must not require native control"
        ),
        request_id_factory=lambda: "start-timeout",
        clock=clock,
        sleep=clock.sleep,
        startup_timeout=0.2,
        startup_poll_interval=0.1,
    )

    result = controller.dispatch("start")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "SERVICE_START_TIMEOUT"
    assert result["service"]["status"] == "STARTING"
    assert result["unit"]["active"] is True


def test_serve_loads_owner_private_default_config_and_runs_runtime_with_supplied_factories(
    tmp_path: Path,
) -> None:
    path, expected_config = _write_runtime_config(tmp_path)
    calls: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, config, service_state, *, backend_factory, process_factory):
            calls["config"] = config
            calls["service_state"] = service_state
            calls["backend_factory"] = backend_factory
            calls["process_factory"] = process_factory

        def run(self):
            calls["run"] = True
            return RuntimeOutcome(RuntimeOutcomeState.SHELL_READY)

    backend_factory = object()
    process_factory = object()
    service_state = object()

    def service_state_factory(state_path):
        calls["state_path"] = state_path
        return service_state

    controller = ProductionWebUIController(
        environ={"XDG_CONFIG_HOME": str(tmp_path / "config")},
        home=str(tmp_path / "home"),
        owner_uid=os.getuid(),
        runtime_runner_factory=FakeRunner,
        backend_factory=backend_factory,  # type: ignore[arg-type]
        process_factory=process_factory,  # type: ignore[arg-type]
        service_state_factory=service_state_factory,
        control_client_factory=lambda _path, _uid: pytest.fail("control client not used"),
        browser_launcher=lambda _origin: pytest.fail("browser not used"),
    )

    result = controller.dispatch("serve")

    assert result["status"] == "OK"
    assert result["state"] == "SHELL_READY"
    assert calls["config"] == expected_config
    assert calls["backend_factory"] is backend_factory
    assert calls["process_factory"] is process_factory
    assert calls["run"] is True
    assert str(calls["state_path"]).endswith("/state/factlane/webui-service.json")
    assert path.exists()


def test_browser_neutral_serve_binds_legacy_transition_to_setup_installation_identity(
    tmp_path: Path,
) -> None:
    config_path, _legacy_config = _write_runtime_config(tmp_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["origin"] = "http://127.0.0.1:8765"
    config_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    journal_path = linux_setup_journal_path(payload["state_dir"])
    journal_path.parent.mkdir(parents=True, mode=0o700)
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "phase": "CLAIMED",
                "installation_id": "test123",
                "hostname": "factlane-test123.test",
                "installing_user": "owner",
                "browser_family": None,
                "browser_executable": None,
                "browser_profile_dir": str(journal_path.parent / "browser"),
                "state_dir": payload["state_dir"],
                "caddy_state_dir": str(journal_path.parent / "caddy"),
                "root_fingerprint_sha256": None,
                "ownership": {},
            },
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n",
        encoding="utf-8",
    )
    journal_path.chmod(0o600)
    calls: dict[str, object] = {}

    class FakeRunner:
        def __init__(
            self,
            _config,
            _service_state,
            *,
            backend_factory,
            process_factory,
            legacy_endpoint_hostname,
        ):
            calls["backend_factory"] = backend_factory
            calls["process_factory"] = process_factory
            calls["legacy_endpoint_hostname"] = legacy_endpoint_hostname

        def run(self):
            return RuntimeOutcome(RuntimeOutcomeState.SHELL_READY)

    controller = ProductionWebUIController(
        environ={"XDG_CONFIG_HOME": str(tmp_path / "config")},
        home=str(tmp_path / "home"),
        owner_uid=os.getuid(),
        runtime_runner_factory=FakeRunner,
        backend_factory=object(),  # type: ignore[arg-type]
        process_factory=object(),  # type: ignore[arg-type]
        service_state_factory=lambda _path: object(),
        control_client_factory=lambda _path, _uid: pytest.fail("control client not used"),
        browser_launcher=lambda _origin: pytest.fail("browser not used"),
    )

    result = controller.dispatch("serve")

    assert result == {"status": "OK", "state": "SHELL_READY"}
    assert calls["legacy_endpoint_hostname"] == "factlane-test123.test"


def test_serve_missing_default_config_fails_with_concrete_config_error_without_factories(
    tmp_path: Path,
) -> None:
    invoked: list[str] = []
    controller = ProductionWebUIController(
        environ={"XDG_CONFIG_HOME": str(tmp_path / "missing-config")},
        home=str(tmp_path / "home"),
        owner_uid=os.getuid(),
        runtime_runner_factory=lambda *_a, **_kw: invoked.append("runner"),
        backend_factory=lambda _spec, _service_state: invoked.append("backend"),  # type: ignore[arg-type]
        process_factory=lambda _config, _argv: invoked.append("process"),  # type: ignore[arg-type]
        service_state_factory=lambda _path: invoked.append("state"),
        control_client_factory=lambda _path, _uid: pytest.fail("control client not used"),
        browser_launcher=lambda _origin: pytest.fail("browser not used"),
    )

    result = controller.dispatch("serve")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "RUNTIME_CONFIG_ERROR"
    assert "unavailable" in result["message"]
    assert invoked == []


def test_serve_existing_config_loads_production_app_composition(
    tmp_path: Path,
) -> None:
    _write_runtime_config(tmp_path)
    calls: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, _config, _service_state, *, backend_factory, process_factory):
            calls["backend_factory"] = backend_factory
            calls["process_factory"] = process_factory

        def run(self):
            return RuntimeOutcome(RuntimeOutcomeState.SHELL_READY)

    controller = ProductionWebUIController(
        environ={"XDG_CONFIG_HOME": str(tmp_path / "config")},
        home=str(tmp_path / "home"),
        owner_uid=os.getuid(),
        runtime_runner_factory=FakeRunner,
        control_client_factory=lambda _path, _uid: pytest.fail("control client not used"),
    )

    result = controller.dispatch("serve")

    assert result == {"status": "OK", "state": "SHELL_READY"}
    assert calls["backend_factory"] is webui_app.create_owner_runtime_backend
    assert calls["process_factory"] is webui_app.create_caddy_process


def test_windows_serve_composes_backend_owned_https_and_ensures_tls_before_runtime_on_linux_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = default_owner_paths(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        home=str(tmp_path / "home"),
        owner_uid=os.getuid(),
    )
    managed = WindowsManagedConfig(
        installation_id="winserve123",
        origin="https://factlane-winserve123.test:8765",
        native_control_pipe=r"\\.\pipe\factlane-owner-test123",
        runtime_dir=r"C:\Users\Owner\AppData\Local\FactLane\runtime",
        state_dir=r"C:\Users\Owner\AppData\Local\FactLane\state",
        browser_profile_dir=r"C:\Users\Owner\AppData\Local\FactLane\browser",
    )
    service_state = object()
    calls: dict[str, object] = {}
    events: list[str] = []

    monkeypatch.setattr(
        webui_platform_module,
        "load_windows_managed_config",
        lambda path: calls.setdefault("managed_config_path", path) and managed,
    )

    def ensure_tls(root, hostname):
        events.append("ensure_tls")
        calls["tls_root"] = root
        calls["tls_hostname"] = hostname
        return object()

    monkeypatch.setattr(webui_platform_module, "ensure_windows_tls_material", ensure_tls)

    class FakeRunner:
        def __init__(
            self,
            config,
            exact_service_state,
            *,
            backend_factory,
            process_factory,
            external_gateway_required,
        ):
            events.append("runner_init")
            calls["config"] = config
            calls["service_state"] = exact_service_state
            calls["backend_factory"] = backend_factory
            calls["process_factory"] = process_factory
            calls["external_gateway_required"] = external_gateway_required

        def run(self):
            events.append("runner_run")
            return RuntimeOutcome(RuntimeOutcomeState.STOPPED)

    def service_state_factory(path):
        calls["service_state_path"] = path
        return service_state

    controller = ProductionWebUIController(
        paths=paths,
        platform_system="Windows",
        runtime_runner_factory=FakeRunner,
        service_state_factory=service_state_factory,
    )

    result = controller._serve_windows()

    assert result == {"status": "OK", "state": "STOPPED"}
    config = calls["config"]
    assert isinstance(config, WebUIRuntimeConfig)
    assert config.platform_kind is PlatformKind.WINDOWS
    assert config.backend_transport is BackendTransportKind.WINDOWS_HTTPS
    assert config.backend_socket is None
    assert config.caddy_path is None
    assert config.caddyfile is None
    assert config.origin == managed.origin
    assert config.runtime_dir == managed.runtime_dir
    assert config.state_dir == managed.state_dir
    assert calls["backend_factory"] is webui_app.create_owner_runtime_backend
    assert calls["process_factory"] is None
    assert calls["external_gateway_required"] is False
    assert calls["service_state"] is service_state
    assert calls["tls_root"] == Path(managed.state_dir) / "tls"
    assert calls["tls_hostname"] == "factlane-winserve123.test"
    assert events == ["ensure_tls", "runner_init", "runner_run"]


def test_active_service_mutations_use_native_while_status_is_offline(
    tmp_path: Path,
) -> None:
    paths = default_owner_paths(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        home=str(tmp_path / "home"),
        owner_uid=1000,
    )
    calls: list[tuple[str, str | None]] = []
    service_state = _persistent_service_state(tmp_path)
    service_state.claim_endpoint("https://factlane-test123.test:8765")
    service_state.mark_ready()
    unit = _UnitManager(active=True)

    class Client:
        def status(self):
            calls.append(("status", None))
            return {
                "status": "READY",
                "saved_endpoint": "https://factlane-test123.test:8765",
            }

        def approve_pending_sensitive(self):
            calls.append(("sensitive", None))
            raise WebUIPlatformError(
                "SENSITIVE_CHALLENGE_NONE",
                "no pending sensitive challenge",
            )

        def start(self, *, request_id: str):
            calls.append(("start", request_id))
            return {"status": "STARTING"}

        def stop(self, *, request_id: str):
            calls.append(("stop", request_id))
            return {"status": "STOPPED_UNTIL_REBOOT"}

        def restart(self, *, request_id: str):
            calls.append(("restart", request_id))
            return {"status": "STARTING"}

    controller = ProductionWebUIController(
        paths=paths,
        owner_uid=1000,
        runtime_runner_factory=lambda *_a, **_kw: pytest.fail("runner not used"),
        backend_factory=lambda _spec, _service_state: pytest.fail("backend not used"),
        process_factory=lambda _config, _argv: pytest.fail("process not used"),
        service_state_factory=lambda _path: service_state,
        systemd_manager=unit,
        control_client_factory=lambda path, uid: (
            pytest.fail("wrong socket") if path != paths.native_control_socket else Client()
        ),
        request_id_factory=lambda: "request-fixed",
    )

    assert controller.dispatch("status")["status"] == "OK"
    assert controller.dispatch("start")["status"] == "OK"
    assert controller.dispatch("stop")["status"] == "OK"
    assert controller.dispatch("restart")["status"] == "OK"
    assert calls == [
        ("start", "request-fixed"),
        ("stop", "request-fixed"),
        ("restart", "request-fixed"),
    ]


def test_legacy_linux_runtime_config_keeps_native_control_reachable_until_setup_migrates_it(
    tmp_path: Path,
) -> None:
    owner_uid = os.getuid()
    paths = default_owner_paths(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        home=str(tmp_path / "home"),
        owner_uid=owner_uid,
    )
    legacy_backend = tmp_path / "runtime" / "factlane" / "owner-http.sock"
    config_path = Path(paths.service_config_path)
    config_path.parent.mkdir(parents=True, mode=0o700)
    config_path.write_text(
        json.dumps(
            {
                "db": None,
                "profile": None,
                "host_id": None,
                "origin": "https://factlane-test123.test:8765",
                "backend_socket": str(legacy_backend),
                "caddy_path": "/usr/bin/caddy",
                "caddyfile": paths.caddy_config_path,
                "runtime_dir": str(tmp_path / "runtime" / "factlane"),
                "state_dir": str(tmp_path / "state" / "factlane"),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    expected_control = str(legacy_backend.parent / "owner-control.sock")
    seen_paths: list[str] = []
    service_state = _persistent_service_state(tmp_path)
    service_state.claim_endpoint("https://factlane-test123.test:8765")
    service_state.mark_ready()
    unit = _UnitManager(active=True)

    class Client:
        def status(self):
            return {
                "status": "STARTING",
                "saved_endpoint": "https://factlane-test123.test:8765",
            }

        def start(self, *, request_id: str):
            return {"status": "STARTING", "request_id": request_id}

    def client_factory(path: str, _uid: int):
        seen_paths.append(path)
        return Client()

    controller = ProductionWebUIController(
        paths=paths,
        owner_uid=owner_uid,
        service_state_factory=lambda _path: service_state,
        systemd_manager=unit,
        control_client_factory=client_factory,
        request_id_factory=lambda: "legacy-request",
    )

    assert controller.dispatch("start")["status"] == "OK"
    opened = controller.dispatch("open")
    assert opened["status"] == "ERROR"
    assert opened["error_code"] == "WEBUI_NOT_RUNNING"
    assert seen_paths == [expected_control, expected_control]


def test_open_creates_local_enrollment_presents_loopback_locator_once_before_waiting_for_claim_approval(
    tmp_path: Path,
) -> None:
    paths = default_owner_paths(
        environ={"XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
        home=str(tmp_path / "home"),
        owner_uid=1000,
    )

    calls: list[tuple[str, object]] = []
    presented: list[str] = []

    class Clock:
        def __init__(self) -> None:
            self.value = 100.0

        def __call__(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += seconds

    clock = Clock()

    class Client:
        def status(self):
            calls.append(("status", None))
            return {
                "status": "READY",
                "saved_endpoint": "http://127.0.0.1:8765",
            }

        def approve_pending_sensitive(self):
            calls.append(("sensitive", None))
            raise WebUIPlatformError(
                "SENSITIVE_CHALLENGE_NONE",
                "no pending sensitive challenge",
            )

        def begin_local_enrollment(self, *, device_label: str):
            calls.append(("begin", device_label))
            return PendingEnrollment(
                locator="loc a/b?c",
                verification_phrase="amber cedar river willow zephyr",
                expires_at=200.0,
            )

        def approve_pending(self, *, locator: str):
            calls.append(("approve", locator))
            attempts = sum(1 for action, _value in calls if action == "approve")
            if attempts < 3:
                raise WebUIPlatformError(
                    "PENDING_NOT_CLAIMED",
                    "browser has not claimed enrollment yet",
                    retryable=True,
                )
            return None

    controller = ProductionWebUIController(
        paths=paths,
        owner_uid=1000,
        runtime_runner_factory=lambda *_a, **_kw: pytest.fail("runner not used"),
        backend_factory=lambda _spec, _service_state: pytest.fail("backend not used"),
        process_factory=lambda _config, _argv: pytest.fail("process not used"),
        service_state_factory=lambda _path: pytest.fail("state not used"),
        control_client_factory=lambda _path, _uid: Client(),
        open_link_presenter=lambda url: (
            calls.append(("present", url)),
            presented.append(url),
        ),
        clock=clock,
        sleep=clock.sleep,
        enrollment_timeout=30.0,
        enrollment_poll_interval=0.25,
    )

    result = controller.dispatch("open")

    assert result == {
        "status": "OK",
        "state": "OPENED",
        "origin": "http://127.0.0.1:8765",
    }
    assert presented == [
        "http://127.0.0.1:8765#connect=loc%20a%2Fb%3Fc"
    ]
    parsed = urlsplit(presented[0])
    assert parsed.scheme == "http"
    assert parsed.hostname == "127.0.0.1"
    assert parsed.fragment == "connect=loc%20a%2Fb%3Fc"
    assert parsed.query == ""
    assert "amber" not in presented[0]
    assert "csrf" not in presented[0].casefold()
    assert "session" not in presented[0].casefold()
    assert calls == [
        ("status", None),
        ("sensitive", None),
        ("begin", "FactLane local browser"),
        ("present", "http://127.0.0.1:8765#connect=loc%20a%2Fb%3Fc"),
        ("approve", "loc a/b?c"),
        ("approve", "loc a/b?c"),
        ("approve", "loc a/b?c"),
    ]
    assert clock.value == 100.5


def test_open_fails_closed_on_non_retryable_native_approval_error(tmp_path: Path) -> None:
    presented: list[str] = []

    class Client:
        def status(self):
            return {
                "status": "RECOVERY_REQUIRED",
                "saved_endpoint": "http://127.0.0.1:8765",
            }

        def approve_pending_sensitive(self):
            raise WebUIPlatformError(
                "SENSITIVE_CHALLENGE_NONE",
                "no pending sensitive challenge",
            )

        def begin_local_enrollment(self, *, device_label: str):
            assert device_label == "FactLane local browser"
            return PendingEnrollment(
                locator="locator-1",
                verification_phrase="amber cedar river willow zephyr",
                expires_at=1_000_000_000_000.0,
            )

        def approve_pending(self, *, locator: str):
            assert locator == "locator-1"
            raise WebUIPlatformError(
                "NATIVE_CONFIRMATION_DENIED",
                "Owner denied the enrollment request",
            )

    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: pytest.fail("state not used"),
        control_client_factory=lambda _path, _uid: Client(),
        open_link_presenter=presented.append,
    )

    result = controller.dispatch("open")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "NATIVE_CONFIRMATION_DENIED"
    assert presented == [
        "http://127.0.0.1:8765#connect=locator-1"
    ]


def test_open_does_not_retry_ambiguous_native_approval_transport_timeout(tmp_path: Path) -> None:
    approvals: list[str] = []
    presented: list[str] = []

    class Client:
        def status(self):
            return {
                "status": "READY",
                "saved_endpoint": "http://127.0.0.1:8765",
            }

        def approve_pending_sensitive(self):
            raise WebUIPlatformError(
                "SENSITIVE_CHALLENGE_NONE",
                "no pending sensitive challenge",
            )

        def begin_local_enrollment(self, *, device_label: str):
            assert device_label == "FactLane local browser"
            return PendingEnrollment(
                locator="locator-timeout",
                verification_phrase="amber cedar river willow zephyr",
                expires_at=1_000_000_000_000.0,
            )

        def approve_pending(self, *, locator: str):
            approvals.append(locator)
            raise WebUIPlatformError(
                "CONTROL_UNAVAILABLE",
                "native control response timed out",
            )

    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: pytest.fail("state not used"),
        control_client_factory=lambda _path, _uid: Client(),
        open_link_presenter=presented.append,
    )

    result = controller.dispatch("open")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "CONTROL_UNAVAILABLE"
    assert approvals == ["locator-timeout"]
    assert presented == ["http://127.0.0.1:8765#connect=locator-timeout"]


def test_open_link_presentation_failure_fails_closed_before_native_approval(tmp_path: Path) -> None:
    approvals: list[str] = []

    class Client:
        def status(self):
            return {
                "status": "READY",
                "saved_endpoint": "http://127.0.0.1:8765",
            }

        def approve_pending_sensitive(self):
            raise WebUIPlatformError(
                "SENSITIVE_CHALLENGE_NONE",
                "no pending sensitive challenge",
            )

        def begin_local_enrollment(self, *, device_label: str):
            assert device_label == "FactLane local browser"
            return PendingEnrollment(
                locator="locator-no-browser",
                verification_phrase="amber cedar river willow zephyr",
                expires_at=1_000_000_000_000.0,
            )

        def approve_pending(self, *, locator: str):
            approvals.append(locator)
            pytest.fail("approval must not run when link presentation failed")

    def fail_presenter(_url: str) -> None:
        raise OSError("stderr unavailable")

    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        control_client_factory=lambda _path, _uid: Client(),
        open_link_presenter=fail_presenter,
    )

    result = controller.dispatch("open")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "OPEN_LINK_PRESENTATION_FAILED"
    assert approvals == []


def test_open_times_out_waiting_for_browser_claim_without_approving_or_starting_service(
    tmp_path: Path,
) -> None:
    class Clock:
        def __init__(self) -> None:
            self.value = 10.0

        def __call__(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += seconds

    clock = Clock()
    approvals: list[str] = []
    presented: list[str] = []

    class Client:
        def status(self):
            return {
                "status": "READY",
                "saved_endpoint": "http://127.0.0.1:8765",
            }

        def approve_pending_sensitive(self):
            raise WebUIPlatformError(
                "SENSITIVE_CHALLENGE_NONE",
                "no pending sensitive challenge",
            )

        def begin_local_enrollment(self, *, device_label: str):
            assert device_label == "FactLane local browser"
            return PendingEnrollment(
                locator="locator-timeout",
                verification_phrase="amber cedar river willow zephyr",
                expires_at=10.3,
            )

        def approve_pending(self, *, locator: str):
            approvals.append(locator)
            raise WebUIPlatformError(
                "PENDING_NOT_CLAIMED",
                "browser has not claimed enrollment yet",
                retryable=True,
            )

        def start(self, *, request_id: str):
            pytest.fail(f"open must never start service: {request_id}")

    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        service_state_factory=lambda _path: pytest.fail("state not used"),
        control_client_factory=lambda _path, _uid: Client(),
        open_link_presenter=presented.append,
        clock=clock,
        sleep=clock.sleep,
        enrollment_timeout=30.0,
        enrollment_poll_interval=0.1,
    )

    result = controller.dispatch("open")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "PENDING_CLAIM_TIMEOUT"
    assert len(approvals) >= 2
    assert presented == ["http://127.0.0.1:8765#connect=locator-timeout"]
    assert clock.value == pytest.approx(10.3)


@pytest.mark.parametrize("service_state", ["READY", "RECOVERY_REQUIRED"])
def test_open_approves_pending_sensitive_before_enrollment_and_returns_without_presenting_url(
    tmp_path: Path,
    service_state: str,
) -> None:
    calls: list[str] = []

    class Client:
        def status(self):
            calls.append("status")
            return {
                "status": service_state,
                "saved_endpoint": "http://127.0.0.1:8765",
            }

        def approve_pending_sensitive(self):
            calls.append("sensitive")
            return None

        def begin_local_enrollment(self, *, device_label: str):
            pytest.fail(f"sensitive approval must not enroll: {device_label}")

    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        control_client_factory=lambda _path, _uid: Client(),
        open_link_presenter=lambda _url: pytest.fail("sensitive approval must not present a URL"),
    )

    assert controller.dispatch("open") == {
        "status": "OK",
        "state": "SENSITIVE_APPROVED",
        "origin": "http://127.0.0.1:8765",
    }
    assert calls == ["status", "sensitive"]


@pytest.mark.parametrize(
    "code",
    [
        "SENSITIVE_CHALLENGE_AMBIGUOUS",
        "NATIVE_CONFIRMATION_DENIED",
        "SESSION_EXPIRED",
        "SENSITIVE_CHALLENGE_INVALID",
        "CONTROL_UNAVAILABLE",
    ],
)
def test_open_sensitive_errors_fail_closed_without_enrollment_or_url_presentation(
    tmp_path: Path,
    code: str,
) -> None:
    calls: list[str] = []

    class Client:
        def status(self):
            calls.append("status")
            return {
                "status": "READY",
                "saved_endpoint": "http://127.0.0.1:8765",
            }

        def approve_pending_sensitive(self):
            calls.append("sensitive")
            raise WebUIPlatformError(code, "sensitive approval failed")

        def begin_local_enrollment(self, *, device_label: str):
            pytest.fail(f"sensitive failure must not enroll: {device_label}")

    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        control_client_factory=lambda _path, _uid: Client(),
        open_link_presenter=lambda _url: pytest.fail("sensitive failure must not present a URL"),
    )

    result = controller.dispatch("open")
    assert result["status"] == "ERROR"
    assert result["error_code"] == code
    assert calls == ["status", "sensitive"]


def test_open_stopped_service_fails_without_beginning_enrollment_or_starting(tmp_path: Path) -> None:
    calls: list[str] = []

    class Client:
        def status(self):
            calls.append("status")
            return {
                "status": "STOPPED_UNTIL_REBOOT",
                "saved_endpoint": "http://127.0.0.1:8765",
            }

        def begin_local_enrollment(self, *, device_label: str):
            pytest.fail(f"stopped service must not enroll: {device_label}")

        def start(self, *, request_id: str):
            pytest.fail(f"open must not start stopped service: {request_id}")

    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        control_client_factory=lambda _path, _uid: Client(),
        open_link_presenter=lambda _url: pytest.fail("stopped service must not present a URL"),
    )

    result = controller.dispatch("open")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "WEBUI_NOT_RUNNING"
    assert calls == ["status"]


def test_default_platform_controller_binds_exact_stderr_link_presenter_and_flushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FlushTrackingStream(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flushes = 0

        def flush(self) -> None:
            self.flushes += 1
            super().flush()

    stream = FlushTrackingStream()
    sentinel = object()

    def controller_factory(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(webui_platform_module, "ProductionWebUIController", controller_factory)
    monkeypatch.setattr(webui_platform_module.sys, "stderr", stream)

    assert webui_platform_module.build_default_platform_controller() is sentinel
    presenter = captured["open_link_presenter"]
    assert callable(presenter)

    url = "http://127.0.0.1:8765#connect=locator-1"
    presenter(url)

    assert stream.getvalue() == (
        "Open this URL in any browser:\n"
        f"{url}\n"
        "Waiting for browser connection and local approval...\n"
    )
    assert stream.flushes == 1
