from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

if sys.platform != "linux":
    pytest.skip(
        "managed Owner WebUI runtime qualification is Linux-only",
        allow_module_level=True,
    )

from factlane.webui_runtime import (
    BackendTransportKind,
    BackendMode,
    PlatformKind,
    RuntimeConfigError,
    RuntimeOutcomeState,
    WebUIRuntimeConfig,
    WebUIRuntimeRunner,
    default_runtime_config_path,
    load_runtime_config,
)
from factlane.webui_service import EndpointUnavailable, ServiceStatus, WebUIServiceState


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class _Backend:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.alive = True
        self.start_calls = 0
        self.drain_calls = 0
        self.wait_timeouts: list[float] = []
        self.wait_result = True
        self.wait_advance = 0.0
        self.clock: _Clock | None = None

    def start(self) -> None:
        self.start_calls += 1

    def is_ready(self) -> bool:
        return self.ready

    def is_alive(self) -> bool:
        return self.alive

    def begin_drain(self) -> None:
        self.drain_calls += 1

    def wait_stopped(self, timeout: float) -> bool:
        self.wait_timeouts.append(timeout)
        if self.clock is not None:
            self.clock.value += min(timeout, self.wait_advance)
        return self.wait_result


class _Process:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.wait_timeouts: list[float] = []
        self.wait_result = True
        self.wait_advance = 0.0
        self.clock: _Clock | None = None

    def is_ready(self) -> bool:
        return self.ready

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1

    def wait_stopped(self, timeout: float) -> bool:
        self.wait_timeouts.append(timeout)
        if self.clock is not None:
            self.clock.value += min(timeout, self.wait_advance)
        return self.wait_result


def _payload(tmp_path: Path, *, memory_configured: bool = True) -> dict[str, object]:
    runtime_dir = tmp_path / "run" / "factlane"
    state_dir = tmp_path / "state" / "factlane"
    return {
        "db": str(tmp_path / "memories.db") if memory_configured else None,
        "profile": "nomic-768" if memory_configured else None,
        "host_id": "owner-host-a" if memory_configured else None,
        "origin": "https://factlane-a1b2c3.test:8767",
        "backend_socket": str(runtime_dir / "owner-http.sock"),
        "caddy_path": "/usr/bin/caddy",
        "caddyfile": str(tmp_path / "config" / "Caddyfile"),
        "runtime_dir": str(runtime_dir),
        "state_dir": str(state_dir),
    }


def _write_config(
    tmp_path: Path,
    *,
    extra: dict[str, object] | None = None,
    memory_configured: bool = True,
) -> Path:
    payload: dict[str, object] = dict(
        _payload(tmp_path, memory_configured=memory_configured)
    )
    if extra:
        payload.update(extra)
    path = tmp_path / "webui.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def _windows_payload(*, memory_configured: bool = True) -> dict[str, object]:
    return {
        "db": r"C:\Users\Owner\AppData\Local\FactLane\memory.db" if memory_configured else None,
        "profile": "nomic-768" if memory_configured else None,
        "host_id": "owner-host-win" if memory_configured else None,
        "origin": "http://127.0.0.1:8767",
        "backend_socket": None,
        "caddy_path": None,
        "caddyfile": None,
        "runtime_dir": r"C:\Users\Owner\AppData\Local\FactLane\runtime",
        "state_dir": r"C:\Users\Owner\AppData\Local\FactLane\state",
        "platform_kind": "WINDOWS",
        "backend_transport": "WINDOWS_HTTP",
    }


def _write_windows_config(tmp_path: Path, *, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "windows-webui.json"
    path.write_text(json.dumps(_windows_payload() if payload is None else payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def _service(tmp_path: Path, clock: _Clock) -> WebUIServiceState:
    return WebUIServiceState(
        tmp_path / "service-state" / "webui.json",
        clock=clock,
        boot_id=lambda: "boot-a",
    )


def _runner(
    tmp_path: Path,
    *,
    clock: _Clock | None = None,
    backend: _Backend | None = None,
    process: _Process | None = None,
    memory_configured: bool = True,
):
    clock = clock or _Clock()
    backend = backend or _Backend()
    process = process or _Process()
    backend.clock = clock
    process.clock = clock
    config = load_runtime_config(
        _write_config(tmp_path, memory_configured=memory_configured)
    )
    backend_specs = []
    process_argv = []
    service_state = _service(tmp_path, clock)

    def backend_factory(spec, exact_service_state):
        assert exact_service_state is service_state
        backend_specs.append(spec)
        return backend

    def process_factory(exact_config, argv):
        assert exact_config is config
        process_argv.append(argv)
        return process

    runner = WebUIRuntimeRunner(
        config,
        service_state,
        backend_factory=backend_factory,
        process_factory=process_factory,
        clock=clock,
        sleep=clock.sleep,
        poll_interval=1.0,
    )
    return runner, backend, process, backend_specs, process_argv, clock


def test_config_loader_accepts_only_exact_owner_private_regular_json(tmp_path: Path) -> None:
    config = load_runtime_config(_write_config(tmp_path))

    assert config.db == str(tmp_path / "memories.db")
    assert config.profile == "nomic-768"
    assert config.host_id == "owner-host-a"
    assert config.origin == "https://factlane-a1b2c3.test:8767"
    assert config.backend_socket.endswith("/run/factlane/owner-http.sock")

    with pytest.raises(RuntimeConfigError, match="unknown"):
        load_runtime_config(_write_config(tmp_path, extra={"surprise": True}))


def test_config_loader_accepts_bounded_public_origin_allowlist_and_runner_threads_it(tmp_path: Path) -> None:
    public_origins = [
        "https://panel.example.com",
        "https://owner.example.net:9443",
    ]
    config = load_runtime_config(
        _write_config(tmp_path, extra={"public_origins": public_origins})
    )
    assert config.public_origins == tuple(public_origins)

    clock = _Clock()
    backend = _Backend()
    backend.clock = clock
    service_state = _service(tmp_path, clock)
    backend_specs = []
    runner = WebUIRuntimeRunner(
        config,
        service_state,
        backend_factory=lambda spec, exact_service_state: (
            pytest.fail("runner passed a different service-state instance")
            if exact_service_state is not service_state
            else backend_specs.append(spec) or backend
        ),
        process_factory=None,
        external_gateway_required=False,
        clock=clock,
        sleep=clock.sleep,
        poll_interval=1.0,
    )

    assert runner.start().state is RuntimeOutcomeState.READY
    assert backend_specs[0].public_origins == tuple(public_origins)
    assert runner.service_state.snapshot().saved_endpoint == config.origin
    assert runner.stop() is True


@pytest.mark.parametrize(
    "public_origins",
    [
        ["http://panel.example.com"],
        ["https://panel.example.com/path"],
        ["https://Panel.example.com"],
        ["https://factlane-a1b2c3.test:8767"],
        ["https://panel.example.com", "https://panel.example.com"],
        [f"https://panel-{index}.example.com" for index in range(9)],
    ],
)
def test_config_loader_rejects_unbounded_or_noncanonical_public_origins(
    tmp_path: Path,
    public_origins: list[str],
) -> None:
    with pytest.raises(RuntimeConfigError, match="public_origins|public origin"):
        load_runtime_config(
            _write_config(tmp_path, extra={"public_origins": public_origins})
        )


def test_legacy_linux_config_schema_remains_explicitly_linux_af_unix_without_host_inference(tmp_path: Path) -> None:
    config = load_runtime_config(_write_config(tmp_path))

    assert config.platform_kind is PlatformKind.LINUX
    assert config.backend_transport is BackendTransportKind.AF_UNIX
    assert config.public_origins == ()


def test_windows_native_config_constructs_and_loads_with_browser_neutral_loopback_http_on_linux_host(tmp_path: Path) -> None:
    payload = _windows_payload()
    direct = WebUIRuntimeConfig(**payload)  # type: ignore[arg-type]
    loaded = load_runtime_config(_write_windows_config(tmp_path, payload=payload))

    for config in (direct, loaded):
        assert config.platform_kind is PlatformKind.WINDOWS
        assert config.backend_transport is BackendTransportKind.WINDOWS_HTTP
        assert config.db == r"C:\Users\Owner\AppData\Local\FactLane\memory.db"
        assert config.runtime_dir == r"C:\Users\Owner\AppData\Local\FactLane\runtime"
        assert config.state_dir == r"C:\Users\Owner\AppData\Local\FactLane\state"
        assert config.backend_socket is None
        assert config.caddy_path is None
        assert config.caddyfile is None


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("db", r"\\wsl.localhost\Ubuntu-22.04\home\owner\memory.db"),
        ("db", r"\\server\share\memory.db"),
        ("db", r"relative\memory.db"),
        ("db", r"C:\FactLane\..\memory.db"),
        ("db", "C:/FactLane/memory.db"),
        ("db", "/home/owner/memory.db"),
        ("runtime_dir", r"\\server\share\runtime"),
        ("runtime_dir", r"C:\FactLane\..\runtime"),
        ("state_dir", r"state\FactLane"),
        ("state_dir", "C:/FactLane/state"),
    ],
)
def test_windows_native_config_rejects_wsl_unc_relative_parent_and_mixed_paths(
    tmp_path: Path,
    field: str,
    invalid: str,
) -> None:
    payload = _windows_payload()
    payload[field] = invalid

    with pytest.raises(RuntimeConfigError, match="Windows path"):
        load_runtime_config(_write_windows_config(tmp_path, payload=payload))


def test_windows_native_config_requires_explicit_complete_platform_transport_contract(tmp_path: Path) -> None:
    missing_transport = _windows_payload()
    missing_transport.pop("backend_transport")
    with pytest.raises(RuntimeConfigError, match="missing required fields"):
        load_runtime_config(_write_windows_config(tmp_path, payload=missing_transport))

    fake_unix = _windows_payload()
    fake_unix["backend_transport"] = "AF_UNIX"
    with pytest.raises(RuntimeConfigError, match="managed loopback"):
        load_runtime_config(_write_windows_config(tmp_path, payload=fake_unix))

    fake_socket = _windows_payload()
    fake_socket["backend_socket"] = r"C:\FactLane\owner-http.sock"
    with pytest.raises(RuntimeConfigError, match="must be null"):
        load_runtime_config(_write_windows_config(tmp_path, payload=fake_socket))

    wrong_transport_origin = _windows_payload()
    wrong_transport_origin["backend_transport"] = "WINDOWS_HTTPS"
    with pytest.raises(RuntimeConfigError, match="WINDOWS_HTTPS"):
        load_runtime_config(_write_windows_config(tmp_path, payload=wrong_transport_origin))


def test_windows_legacy_https_runtime_config_remains_loadable_for_governed_migration(tmp_path: Path) -> None:
    payload = _windows_payload()
    payload["origin"] = "https://factlane-a1b2c3.test:8767"
    payload["backend_transport"] = "WINDOWS_HTTPS"

    loaded = load_runtime_config(_write_windows_config(tmp_path, payload=payload))

    assert loaded.backend_transport is BackendTransportKind.WINDOWS_HTTPS
    assert loaded.origin == "https://factlane-a1b2c3.test:8767"


def test_config_loader_accepts_absent_memory_selection_as_one_null_trio(tmp_path: Path) -> None:
    config = load_runtime_config(_write_config(tmp_path, memory_configured=False))

    assert config.db is None
    assert config.profile is None
    assert config.host_id is None
    assert config.memory_configured is False


@pytest.mark.parametrize(
    "override",
    [
        {"db": None},
        {"profile": None},
        {"host_id": None},
        {"db": None, "profile": None},
        {"db": None, "host_id": None},
        {"profile": None, "host_id": None},
    ],
)
def test_config_loader_rejects_partial_memory_selection_trio(
    tmp_path: Path,
    override: dict[str, object],
) -> None:
    with pytest.raises(RuntimeConfigError, match="all configured or all null"):
        load_runtime_config(_write_config(tmp_path, extra=override))


def test_config_loader_rejects_null_outside_memory_selection_trio(tmp_path: Path) -> None:
    with pytest.raises(RuntimeConfigError, match="must be a string"):
        load_runtime_config(_write_config(tmp_path, extra={"origin": None}))


def test_runtime_config_constructor_rejects_non_string_configured_memory_identity(
    tmp_path: Path,
) -> None:
    payload = _payload(tmp_path)
    payload["profile"] = 123
    with pytest.raises(RuntimeConfigError, match="strings when configured"):
        WebUIRuntimeConfig(**payload)  # type: ignore[arg-type]


def test_default_runtime_config_path_uses_absolute_xdg_config_home() -> None:
    path = default_runtime_config_path(
        environ={"XDG_CONFIG_HOME": "/home/alice/.owner-config"},
        home="/home/alice",
    )

    assert path == Path("/home/alice/.owner-config/factlane/webui.json")


def test_default_runtime_config_path_falls_back_to_owner_home_for_missing_or_relative_xdg() -> None:
    missing = default_runtime_config_path(environ={}, home="/home/alice")
    relative = default_runtime_config_path(
        environ={"XDG_CONFIG_HOME": "relative-config"},
        home="/home/alice",
    )

    assert missing == Path("/home/alice/.config/factlane/webui.json")
    assert relative == missing


def test_default_runtime_config_path_rejects_non_absolute_owner_home() -> None:
    with pytest.raises(RuntimeConfigError, match="owner home"):
        default_runtime_config_path(environ={}, home="relative-home")


def test_config_loader_rejects_symlink_non_owner_and_group_or_world_access(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    symlink = tmp_path / "linked.json"
    symlink.symlink_to(config_path)
    with pytest.raises(RuntimeConfigError, match="symlink"):
        load_runtime_config(symlink)

    with pytest.raises(RuntimeConfigError, match="owner"):
        load_runtime_config(config_path, owner_uid=os.getuid() + 1)

    config_path.chmod(0o640)
    with pytest.raises(RuntimeConfigError, match="private"):
        load_runtime_config(config_path)
    config_path.chmod(0o604)
    with pytest.raises(RuntimeConfigError, match="private"):
        load_runtime_config(config_path)


def test_runner_launches_unix_backend_and_exact_caddy_argv_then_marks_ready(tmp_path: Path) -> None:
    runner, backend, process, backend_specs, process_argv, _clock = _runner(tmp_path)

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.READY
    assert backend.start_calls == 1
    assert process_argv == [
        (
            "/usr/bin/caddy",
            "run",
            "--config",
            str(tmp_path / "config" / "Caddyfile"),
            "--adapter",
            "caddyfile",
        )
    ]
    spec = backend_specs[0]
    assert spec.socket_path == str(tmp_path / "run" / "factlane" / "owner-http.sock")
    assert spec.expected_origin == "https://factlane-a1b2c3.test:8767"
    assert spec.mode is BackendMode.NORMAL
    assert spec.normal_adapter_required is True
    assert not hasattr(spec, "bind_host")
    assert not hasattr(spec, "bind_port")
    assert runner.service_state.snapshot().status is ServiceStatus.READY
    assert runner.service_state.snapshot().saved_endpoint == "https://factlane-a1b2c3.test:8767"
    assert process.poll() is None


def test_runner_can_supervise_backend_owned_transport_without_external_gateway(tmp_path: Path) -> None:
    clock = _Clock()
    backend = _Backend()
    backend.clock = clock
    config = load_runtime_config(_write_config(tmp_path))
    service_state = _service(tmp_path, clock)
    backend_specs = []

    def backend_factory(spec, exact_service_state):
        assert exact_service_state is service_state
        backend_specs.append(spec)
        return backend

    runner = WebUIRuntimeRunner(
        config,
        service_state,
        backend_factory=backend_factory,
        process_factory=None,
        external_gateway_required=False,
        clock=clock,
        sleep=clock.sleep,
        poll_interval=1.0,
    )

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.READY
    assert backend.start_calls == 1
    assert len(backend_specs) == 1
    assert runner.service_state.snapshot().saved_endpoint == config.origin
    assert runner.stop() is True
    assert backend.drain_calls == 1


def test_runner_launches_windows_backend_owned_loopback_http_without_caddy_or_fake_unix_socket(tmp_path: Path) -> None:
    clock = _Clock()
    backend = _Backend()
    backend.clock = clock
    config = load_runtime_config(_write_windows_config(tmp_path))
    service_state = _service(tmp_path, clock)
    backend_specs = []

    runner = WebUIRuntimeRunner(
        config,
        service_state,
        backend_factory=lambda spec, exact_service_state: (
            pytest.fail("runner passed a different service-state instance")
            if exact_service_state is not service_state
            else backend_specs.append(spec) or backend
        ),
        process_factory=None,
        external_gateway_required=False,
        clock=clock,
        sleep=clock.sleep,
        poll_interval=1.0,
    )

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.READY
    assert len(backend_specs) == 1
    spec = backend_specs[0]
    assert spec.platform_kind is PlatformKind.WINDOWS
    assert spec.backend_transport is BackendTransportKind.WINDOWS_HTTP
    assert spec.socket_path is None
    assert spec.runtime_dir == config.runtime_dir
    assert spec.state_dir == config.state_dir
    assert spec.expected_origin == config.origin
    assert spec.normal_adapter_required is True
    assert runner.stop() is True


def test_runner_rejects_config_saved_endpoint_mismatch_before_launch(tmp_path: Path) -> None:
    clock = _Clock()
    service = _service(tmp_path, clock)
    service.claim_endpoint("https://factlane-a1b2c3.test:8766")
    backend = _Backend()
    process = _Process()
    backend_specs = []
    process_argv = []
    config = load_runtime_config(_write_config(tmp_path))
    runner = WebUIRuntimeRunner(
        config,
        service,
        backend_factory=lambda spec, exact_service_state: (
            pytest.fail("runner passed a different service-state instance")
            if exact_service_state is not service
            else backend_specs.append(spec) or backend
        ),
        process_factory=lambda exact_config, argv: (
            pytest.fail("runner passed a different runtime config instance")
            if exact_config is not config
            else process_argv.append(argv) or process
        ),
        clock=clock,
        sleep=clock.sleep,
        poll_interval=1.0,
    )

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.FAILED
    assert "saved endpoint" in (outcome.detail or "")
    assert backend_specs == []
    assert process_argv == []
    assert service.snapshot().saved_endpoint == "https://factlane-a1b2c3.test:8766"


def test_runner_canonicalizes_same_port_legacy_https_state_after_loopback_http_is_ready(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    service = _service(tmp_path, clock)
    service.claim_endpoint("https://factlane-a1b2c3.test:8767")
    service.mark_ready()
    backend = _Backend()
    process = _Process()
    backend.clock = clock
    process.clock = clock
    config = load_runtime_config(
        _write_config(tmp_path, extra={"origin": "http://127.0.0.1:8767"})
    )
    runner = WebUIRuntimeRunner(
        config,
        service,
        legacy_endpoint_hostname="factlane-a1b2c3.test",
        backend_factory=lambda _spec, exact_service_state: (
            pytest.fail("runner passed a different service-state instance")
            if exact_service_state is not service
            else backend
        ),
        process_factory=lambda exact_config, _argv: (
            pytest.fail("runner passed a different runtime config instance")
            if exact_config is not config
            else process
        ),
        clock=clock,
        sleep=clock.sleep,
        poll_interval=1.0,
    )

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.READY
    assert backend.start_calls == 1
    assert service.snapshot().saved_endpoint == "http://127.0.0.1:8767"
    assert service.snapshot().status is ServiceStatus.READY


def test_windows_runner_canonicalizes_same_port_legacy_https_state_to_loopback_http(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    service = _service(tmp_path, clock)
    service.claim_endpoint("https://factlane-legacy123.test:8767")
    service.mark_ready()
    backend = _Backend()
    backend.clock = clock
    config = load_runtime_config(_write_windows_config(tmp_path))
    runner = WebUIRuntimeRunner(
        config,
        service,
        legacy_endpoint_hostname="factlane-legacy123.test",
        backend_factory=lambda _spec, exact_service_state: (
            pytest.fail("runner passed a different service-state instance")
            if exact_service_state is not service
            else backend
        ),
        process_factory=None,
        external_gateway_required=False,
        clock=clock,
        sleep=clock.sleep,
        poll_interval=1.0,
    )

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.READY
    assert backend.start_calls == 1
    assert service.snapshot().saved_endpoint == "http://127.0.0.1:8767"
    assert service.snapshot().status is ServiceStatus.READY


def test_runner_rejects_same_port_legacy_endpoint_from_foreign_installation_before_launch(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    service = _service(tmp_path, clock)
    service.claim_endpoint("https://factlane-foreign.test:8767")
    backend = _Backend()
    process = _Process()
    config = load_runtime_config(
        _write_config(tmp_path, extra={"origin": "http://127.0.0.1:8767"})
    )
    runner = WebUIRuntimeRunner(
        config,
        service,
        legacy_endpoint_hostname="factlane-a1b2c3.test",
        backend_factory=lambda _spec, _service_state: backend,
        process_factory=lambda _config, _argv: process,
        clock=clock,
        sleep=clock.sleep,
        poll_interval=1.0,
    )

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.FAILED
    assert "explicit recovery" in (outcome.detail or "")
    assert backend.start_calls == 0
    assert service.snapshot().saved_endpoint == "https://factlane-foreign.test:8767"


def test_runner_does_not_canonicalize_legacy_endpoint_while_recovery_is_required(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    service = _service(tmp_path, clock)
    service.claim_endpoint("https://factlane-a1b2c3.test:8767")
    service.mark_recovery_required()
    backend = _Backend()
    process = _Process()
    config = load_runtime_config(
        _write_config(tmp_path, extra={"origin": "http://127.0.0.1:8767"})
    )
    runner = WebUIRuntimeRunner(
        config,
        service,
        legacy_endpoint_hostname="factlane-a1b2c3.test",
        backend_factory=lambda _spec, _service_state: backend,
        process_factory=lambda _config, _argv: process,
        clock=clock,
        sleep=clock.sleep,
        poll_interval=1.0,
    )

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.FAILED
    assert "explicit recovery" in (outcome.detail or "")
    assert backend.start_calls == 0
    snapshot = service.snapshot()
    assert snapshot.status is ServiceStatus.RECOVERY_REQUIRED
    assert snapshot.saved_endpoint == "https://factlane-a1b2c3.test:8767"


def test_runner_claim_failure_after_readiness_drains_and_returns_failed(tmp_path: Path) -> None:
    class _RejectingState:
        def __init__(self, wrapped: WebUIServiceState) -> None:
            self.wrapped = wrapped
            self.claims: list[str] = []

        def snapshot(self):
            return self.wrapped.snapshot()

        def mark_starting(self):
            return self.wrapped.mark_starting()

        def claim_endpoint(self, origin: str):
            self.claims.append(origin)
            raise EndpointUnavailable("explicit recovery required")

        def mark_failed(self):
            return self.wrapped.mark_failed()

        def record_crash(self):
            return self.wrapped.record_crash()

        def observe_ready(self):
            return self.wrapped.observe_ready()

    clock = _Clock()
    wrapped = _service(tmp_path, clock)
    state = _RejectingState(wrapped)
    backend = _Backend()
    process = _Process()
    backend.clock = clock
    process.clock = clock
    config = load_runtime_config(_write_config(tmp_path))
    runner = WebUIRuntimeRunner(
        config,
        state,  # type: ignore[arg-type]
        backend_factory=lambda _spec, exact_service_state: (
            pytest.fail("runner passed a different service-state instance")
            if exact_service_state is not state
            else backend
        ),
        process_factory=lambda exact_config, _argv: (
            pytest.fail("runner passed a different runtime config instance")
            if exact_config is not config
            else process
        ),
        clock=clock,
        sleep=clock.sleep,
        poll_interval=1.0,
    )

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.FAILED
    assert state.claims == [config.origin]
    assert backend.drain_calls == 1
    assert process.terminate_calls == 1
    assert wrapped.snapshot().status is ServiceStatus.FAILED


def test_normal_start_without_memory_selection_opens_setup_shell_without_normal_adapter(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    backend = _Backend()
    process = _Process()
    backend.clock = clock
    process.clock = clock
    config = load_runtime_config(_write_config(tmp_path, memory_configured=False))
    backend_specs = []
    service_state = _service(tmp_path, clock)
    runner = WebUIRuntimeRunner(
        config,
        service_state,
        backend_factory=lambda spec, exact_service_state: (
            pytest.fail("runner passed a different service-state instance")
            if exact_service_state is not service_state
            else backend_specs.append(spec) or backend
        ),
        process_factory=lambda exact_config, _argv: (
            pytest.fail("runner passed a different runtime config instance")
            if exact_config is not config
            else process
        ),
        clock=clock,
        sleep=clock.sleep,
        poll_interval=1.0,
    )

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.SHELL_READY
    assert backend_specs[0].mode is BackendMode.SETUP_SHELL
    assert backend_specs[0].normal_adapter_required is False
    assert backend_specs[0].db is None
    assert backend_specs[0].profile is None
    assert backend_specs[0].host_id is None
    assert not (tmp_path / "memories.db").exists()
    assert runner.service_state.snapshot().status is ServiceStatus.READY


def test_readiness_deadline_is_ten_seconds_and_failure_enters_crash_retry_policy(tmp_path: Path) -> None:
    clock = _Clock()
    backend = _Backend(ready=False)
    process = _Process(ready=False)
    runner, _backend, _process, _specs, _argv, clock = _runner(
        tmp_path,
        clock=clock,
        backend=backend,
        process=process,
    )

    outcome = runner.start()

    assert clock.value == pytest.approx(10.0)
    assert outcome.state is RuntimeOutcomeState.RETRY_SCHEDULED
    assert outcome.retry_at == pytest.approx(15.0)
    assert runner.service_state.snapshot().crash_attempts == 1
    assert backend.drain_calls == 1
    assert process.terminate_calls == 1
    assert runner.service_state.snapshot().saved_endpoint is None


def test_unclaimed_setup_gateway_exit_does_not_consume_service_crash_budget(
    tmp_path: Path,
) -> None:
    process = _Process()
    process.returncode = 98
    runner, backend, process, _specs, _argv, _clock = _runner(
        tmp_path,
        process=process,
        memory_configured=False,
    )

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.FAILED
    assert outcome.detail == "setup gateway exited before endpoint claim"
    snapshot = runner.service_state.snapshot()
    assert snapshot.saved_endpoint is None
    assert snapshot.crash_attempts == 0
    assert snapshot.next_retry_at is None
    assert backend.drain_calls == 1
    assert process.terminate_calls == 0


def test_normal_runtime_gateway_exit_during_readiness_still_consumes_crash_budget(
    tmp_path: Path,
) -> None:
    process = _Process()
    process.returncode = 98
    runner, backend, process, _specs, _argv, _clock = _runner(
        tmp_path,
        process=process,
    )

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.RETRY_SCHEDULED
    assert runner.service_state.snapshot().crash_attempts == 1
    assert backend.drain_calls == 1
    assert process.terminate_calls == 0


@pytest.mark.parametrize(
    ("backend_ready", "caddy_ready"),
    [(True, False), (False, True)],
)
def test_endpoint_is_not_claimed_until_both_private_backend_and_caddy_are_ready(
    tmp_path: Path,
    backend_ready: bool,
    caddy_ready: bool,
) -> None:
    clock = _Clock()
    runner, _backend, _process, _specs, _argv, _clock = _runner(
        tmp_path,
        clock=clock,
        backend=_Backend(ready=backend_ready),
        process=_Process(ready=caddy_ready),
    )

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.RETRY_SCHEDULED
    assert runner.service_state.snapshot().saved_endpoint is None


@pytest.mark.parametrize("failed_component", ["backend", "caddy"])
def test_supervision_treats_either_component_exit_as_one_crash_incident(
    tmp_path: Path,
    failed_component: str,
) -> None:
    runner, backend, process, _specs, _argv, _clock = _runner(tmp_path)
    assert runner.start().state is RuntimeOutcomeState.READY
    if failed_component == "backend":
        backend.alive = False
    else:
        process.returncode = 7

    outcome = runner.supervise_once()

    assert outcome.state is RuntimeOutcomeState.RETRY_SCHEDULED
    assert runner.service_state.snapshot().crash_attempts == 1
    assert backend.drain_calls == 1
    assert process.terminate_calls == (0 if failed_component == "caddy" else 1)


def test_current_boot_stop_marker_prevents_backend_or_caddy_launch(tmp_path: Path) -> None:
    runner, backend, _process, backend_specs, process_argv, _clock = _runner(tmp_path)
    runner.service_state.request_stop("owner-stop")

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.STOPPED
    assert backend.start_calls == 0
    assert backend_specs == []
    assert process_argv == []


def test_runtime_failure_observation_cannot_hide_owner_stop_before_launch(tmp_path: Path) -> None:
    runner, backend, _process, backend_specs, process_argv, _clock = _runner(tmp_path)
    runner.service_state.request_stop("owner-stop")
    failed = runner.service_state.mark_failed()

    outcome = runner.start()

    assert failed.status is ServiceStatus.STOPPED_UNTIL_REBOOT
    assert outcome.state is RuntimeOutcomeState.STOPPED
    assert backend.start_calls == 0
    assert backend_specs == []
    assert process_argv == []


def test_safe_stop_shares_one_thirty_second_deadline_and_never_forces_kill(tmp_path: Path) -> None:
    clock = _Clock()
    backend = _Backend()
    process = _Process()
    process.wait_advance = 20.0
    backend.wait_advance = 10.0
    backend.wait_result = False
    runner, backend, process, _specs, _argv, clock = _runner(
        tmp_path,
        clock=clock,
        backend=backend,
        process=process,
    )
    assert runner.start().state is RuntimeOutcomeState.READY

    stopped = runner.stop()

    assert stopped is False
    assert process.terminate_calls == 1
    assert process.wait_timeouts == [pytest.approx(30.0)]
    assert backend.drain_calls == 1
    assert backend.wait_timeouts == [pytest.approx(10.0)]
    assert clock.value == pytest.approx(30.0)
    assert not hasattr(process, "kill_calls")


def test_recovery_state_launches_provider_independent_recovery_shell(tmp_path: Path) -> None:
    runner, _backend, _process, backend_specs, _argv, _clock = _runner(tmp_path)
    runner.service_state.mark_recovery_required()

    outcome = runner.start()

    assert outcome.state is RuntimeOutcomeState.RECOVERY_READY
    assert backend_specs[0].mode is BackendMode.RECOVERY_SHELL
    assert backend_specs[0].normal_adapter_required is False
    assert runner.service_state.snapshot().status is ServiceStatus.RECOVERY_REQUIRED


def test_setup_shell_is_explicitly_representable_without_normal_adapter(tmp_path: Path) -> None:
    runner, _backend, _process, backend_specs, _argv, _clock = _runner(tmp_path)

    outcome = runner.start(mode=BackendMode.SETUP_SHELL)

    assert outcome.state is RuntimeOutcomeState.SHELL_READY
    assert backend_specs[0].mode is BackendMode.SETUP_SHELL
    assert backend_specs[0].normal_adapter_required is False
