from __future__ import annotations

import json
import os
import socket
import struct
import threading
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import factlane.webui_app as webui_app
from factlane.owner_auth import NativeConfirmationRequest, OwnerAuthState, PendingEnrollment
from factlane.owner_native_control import OwnerNativeControlServer
from factlane.webui_install import (
    BrowserInstall,
    EndpointState,
    InstallEnvironment,
    InstallerStepStatus,
    SetupConsent,
    apply_webui_setup,
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
    default_owner_paths,
    detect_lingering_required,
)
from factlane.webui_service import ServiceStatus, WebUIServiceState
from factlane.webui_runtime import RuntimeOutcome, RuntimeOutcomeState, WebUIRuntimeConfig


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
    assert paths.backend_socket == str(runtime / "factlane" / "owner-http.sock")
    assert paths.native_control_socket == str(runtime / "factlane" / "owner-control.sock")
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
    assert paths.state_dir == str(home / ".local" / "state" / "factlane")


def test_first_run_setup_request_uses_owner_paths_and_absent_memory_trio(tmp_path: Path) -> None:
    config = tmp_path / "config"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"

    request = build_default_setup_request(
        environ={
            "XDG_CONFIG_HOME": str(config),
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_STATE_HOME": str(state),
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
    assert request.state_dir == str(state / "factlane")
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
    assert request.lingering_required is False


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
        browser=BrowserInstall(family="chromium", executable="/usr/bin/chromium"),
        euid=1000,
    )
    provider = ZenitySetupConsentProvider(
        "/usr/bin/zenity",
        command_runner=lambda argv: calls.append(argv) or 0,
    )

    consent = provider.acquire(request, environment)

    assert consent == SetupConsent(
        hostname_mapping=True,
        browser_profile_trust=True,
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
    assert "exact local hostname mapping" in text_arg
    assert "isolated FactLane browser profile" in text_arg
    assert "user lingering" in text_arg
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


def test_default_setup_executor_composes_existing_safe_backend_and_preserves_privilege_blocker(
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
        browser=BrowserInstall(family="chromium", executable="/usr/bin/chromium"),
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
        which=lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
        command_runner=lambda argv: commands.append(argv) or 0,
        service_state_reader=lambda: state_reads.append(True)
        or pytest.fail("state reader must not run before privilege boundary"),
    )

    result = executor(receipt, consent)

    assert result.status is InstallerStepStatus.NEEDS_PRIVILEGED_INSTALLER
    assert result.endpoint_state is EndpointState.PROPOSED
    assert result.completed_operations == ()
    assert commands == []
    assert state_reads == []


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
    assert sock.timeouts[0] == 3.0
    assert sock.timeouts[1] >= 125.0
    assert len(sock.timeouts) == 2
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
    assert sensitive_sock.timeouts[0] == 4.0
    assert sensitive_sock.timeouts[1] >= 125.0


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


def test_open_v01_creates_local_enrollment_launches_locator_only_and_waits_for_claim_approval(
    tmp_path: Path,
) -> None:
    paths = default_owner_paths(
        environ={"XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
        home=str(tmp_path / "home"),
        owner_uid=1000,
    )

    calls: list[tuple[str, object]] = []
    opened: list[str] = []

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
                "saved_endpoint": "https://factlane-test123.test:8765",
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
        browser_launcher=lambda url: opened.append(url) or True,
        clock=clock,
        sleep=clock.sleep,
        enrollment_timeout=30.0,
        enrollment_poll_interval=0.25,
    )

    result = controller.dispatch("open")

    assert result == {
        "status": "OK",
        "state": "OPENED",
        "origin": "https://factlane-test123.test:8765",
    }
    assert opened == [
        "https://factlane-test123.test:8765#connect=loc%20a%2Fb%3Fc"
    ]
    parsed = urlsplit(opened[0])
    assert parsed.scheme == "https"
    assert parsed.fragment == "connect=loc%20a%2Fb%3Fc"
    assert parsed.query == ""
    assert "amber" not in opened[0]
    assert "csrf" not in opened[0].casefold()
    assert "session" not in opened[0].casefold()
    assert calls == [
        ("status", None),
        ("sensitive", None),
        ("begin", "FactLane local browser"),
        ("approve", "loc a/b?c"),
        ("approve", "loc a/b?c"),
        ("approve", "loc a/b?c"),
    ]
    assert clock.value == 100.5


def test_open_fails_closed_on_non_retryable_native_approval_error(tmp_path: Path) -> None:
    opened: list[str] = []

    class Client:
        def status(self):
            return {
                "status": "RECOVERY_REQUIRED",
                "saved_endpoint": "https://factlane-test123.test:8765",
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
        browser_launcher=lambda url: opened.append(url) or True,
    )

    result = controller.dispatch("open")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "NATIVE_CONFIRMATION_DENIED"
    assert opened == [
        "https://factlane-test123.test:8765#connect=locator-1"
    ]


def test_open_without_isolated_profile_launcher_fails_closed_after_safe_locator_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approvals: list[str] = []

    class Client:
        def status(self):
            return {
                "status": "READY",
                "saved_endpoint": "https://factlane-test123.test:8765",
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
            pytest.fail("approval must not run when browser launch is unavailable")

    monkeypatch.setattr(
        "factlane.webui_platform._resolve_production_browser_launcher",
        lambda: (_ for _ in ()).throw(
            WebUIPlatformError(
                "BROWSER_LAUNCH_UNAVAILABLE",
                "isolated launcher unavailable",
            )
        ),
    )
    controller = ProductionWebUIController(
        paths=default_owner_paths(
            environ={"XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
            home=str(tmp_path / "home"),
            owner_uid=1000,
        ),
        owner_uid=1000,
        control_client_factory=lambda _path, _uid: Client(),
        browser_launcher=None,
    )

    result = controller.dispatch("open")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "BROWSER_LAUNCH_UNAVAILABLE"
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

    class Client:
        def status(self):
            return {
                "status": "READY",
                "saved_endpoint": "https://factlane-test123.test:8765",
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
        browser_launcher=lambda _url: True,
        clock=clock,
        sleep=clock.sleep,
        enrollment_timeout=30.0,
        enrollment_poll_interval=0.1,
    )

    result = controller.dispatch("open")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "PENDING_CLAIM_TIMEOUT"
    assert len(approvals) >= 2
    assert clock.value == pytest.approx(10.3)


@pytest.mark.parametrize("service_state", ["READY", "RECOVERY_REQUIRED"])
def test_open_approves_pending_sensitive_before_enrollment_and_returns_without_browser(
    tmp_path: Path,
    service_state: str,
) -> None:
    calls: list[str] = []

    class Client:
        def status(self):
            calls.append("status")
            return {
                "status": service_state,
                "saved_endpoint": "https://factlane-test123.test:8765",
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
        browser_launcher=lambda _url: pytest.fail("sensitive approval must not open browser"),
    )

    assert controller.dispatch("open") == {
        "status": "OK",
        "state": "SENSITIVE_APPROVED",
        "origin": "https://factlane-test123.test:8765",
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
def test_open_sensitive_errors_fail_closed_without_enrollment_or_browser(
    tmp_path: Path,
    code: str,
) -> None:
    calls: list[str] = []

    class Client:
        def status(self):
            calls.append("status")
            return {
                "status": "READY",
                "saved_endpoint": "https://factlane-test123.test:8765",
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
        browser_launcher=lambda _url: pytest.fail("sensitive failure must not open browser"),
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
                "saved_endpoint": "https://factlane-test123.test:8765",
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
        browser_launcher=lambda _url: pytest.fail("browser must not open"),
    )

    result = controller.dispatch("open")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "WEBUI_NOT_RUNNING"
    assert calls == ["status"]
