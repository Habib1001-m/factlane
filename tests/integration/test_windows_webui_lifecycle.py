from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows managed-WebUI lifecycle qualification only")

import factlane.webui_platform as webui_platform_module
import factlane.webui_windows as webui_windows_module
from factlane.owner_auth import PendingEnrollment
from factlane.webui_install import (
    BrowserInstall,
    EndpointState,
    InstallEnvironment,
    InstallerStepStatus,
    SetupConsent,
    plan_webui_setup,
)
from factlane.webui_platform import (
    OwnerPaths,
    ProductionWebUIController,
    WebUIPlatformError,
    build_default_setup_executor,
    build_default_setup_request,
    default_owner_paths,
)
from factlane.webui_service import ServiceStateError, ServiceStatus, WebUIServiceState
from factlane.webui_runtime import (
    BackendTransportKind,
    PlatformKind,
    RuntimeOutcome,
    RuntimeOutcomeState,
    WebUIRuntimeConfig,
)
from factlane.webui_windows import (
    WindowsBrowserInstall,
    WindowsBrowserLauncher,
    WindowsWebUIError,
    WindowsTaskObservation,
    WindowsTaskSchedulerSupervisor,
    current_user_sid,
    discover_windows_browser,
    ensure_windows_owner_paths,
    load_windows_managed_config,
    process_identity,
    validate_owner_private_path,
    write_owner_private_text_atomic,
    windows_boot_id,
    windows_managed_python_arguments,
    windows_managed_python_executables,
    windows_owner_paths,
)
from factlane.windows_security import ensure_owner_private_directory


def _factlane_executable() -> str:
    candidate = Path(sys.executable).resolve()
    if candidate.is_file():
        return str(candidate)
    pytest.skip("native Windows Python executable is unavailable")


def test_default_windows_task_supervisor_uses_base_python_with_current_environment_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_base, expected_environment = windows_managed_python_executables()
    monkeypatch.setattr(webui_platform_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(webui_platform_module.sys, "argv", [r".\.venv\Scripts\factlane.exe", "status", "webui"])

    supervisor = webui_platform_module._default_windows_task_supervisor()

    assert str(supervisor.expected_executable).casefold() == expected_base.casefold()
    assert str(supervisor.environment_executable).casefold() == expected_environment.casefold()


def test_windows_managed_python_bootstrap_sets_venv_prefix_before_site_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_root = tmp_path / "future-venv"
    scripts = environment_root / "Scripts"
    scripts.mkdir(parents=True)
    environment_python = scripts / "python.exe"
    environment_python.touch()
    (environment_root / "pyvenv.cfg").write_text("home = C:\\Python\n", encoding="utf-8")

    observed: dict[str, object] = {}
    fake_site = ModuleType("site")

    def site_main() -> None:
        # Python 3.14+ site no longer performs virtual-environment prefix
        # selection. The bootstrap must establish it before site.main().
        observed["prefix_at_site"] = sys.prefix
        observed["exec_prefix_at_site"] = sys.exec_prefix
        observed["executable_at_site"] = sys.executable

    fake_site.main = site_main  # type: ignore[attr-defined]
    fake_cli = ModuleType("factlane.cli")

    def cli_main(argv) -> int:
        observed["cli_argv"] = tuple(argv)
        return 0

    fake_cli.main = cli_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "site", fake_site)
    monkeypatch.setitem(sys.modules, "factlane.cli", fake_cli)
    monkeypatch.setattr(sys, "argv", ["-c", str(environment_python), "serve", "webui"])
    monkeypatch.setattr(sys, "executable", r"C:\Python\python.exe")
    monkeypatch.setattr(sys, "prefix", r"C:\Python")
    monkeypatch.setattr(sys, "exec_prefix", r"C:\Python")

    with pytest.raises(SystemExit) as caught:
        exec(webui_windows_module._WINDOWS_MANAGED_PYTHON_BOOTSTRAP, {})

    assert caught.value.code == 0
    assert observed["prefix_at_site"] == str(environment_root)
    assert observed["exec_prefix_at_site"] == str(environment_root)
    assert observed["executable_at_site"] == str(environment_python)
    assert observed["cli_argv"] == ("serve", "webui")


def test_default_windows_setup_request_uses_current_python_when_console_launcher_argv_is_relative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = str(Path(sys.executable).resolve())
    assert Path(expected).is_file()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setattr(webui_platform_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(webui_platform_module.sys, "argv", [r".\.venv\Scripts\factlane.exe", "setup", "webui"])

    request = build_default_setup_request()

    assert request.factlane_executable.casefold() == expected.casefold()
    _assert_native_windows_path(request.factlane_executable)


def _assert_native_windows_path(value: str) -> None:
    folded = value.replace("/", "\\").casefold()
    assert ":\\" in value
    assert "/home/" not in value.casefold()
    assert "/mnt/" not in value.casefold()
    assert "\\\\wsl" not in folded


def _create_junction(junction: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr or completed.stdout}")


def test_windows_owner_paths_use_localappdata_and_owner_sid_custody(tmp_path: Path) -> None:
    local_appdata = tmp_path / "LocalAppData"
    paths = windows_owner_paths(environ={"LOCALAPPDATA": str(local_appdata)})

    for value in (
        paths.root_dir,
        paths.owner_config_home,
        paths.service_config_path,
        paths.runtime_dir,
        paths.state_dir,
        paths.browser_profile_dir,
    ):
        _assert_native_windows_path(value)
        assert str(local_appdata).casefold() in value.casefold()
    assert paths.native_control_socket.startswith(r"\\.\pipe\factlane-owner-")
    assert paths.backend_endpoint.startswith(r"\\.\pipe\factlane-owner-http-")

    ensure_windows_owner_paths(paths)
    sid = current_user_sid()
    for directory in (
        paths.root_dir,
        paths.owner_config_home,
        paths.runtime_dir,
        paths.state_dir,
        paths.browser_profile_dir,
    ):
        validate_owner_private_path(directory, owner_sid=sid, directory=True)


def test_windows_boot_identity_is_stable_and_service_state_is_owner_private(tmp_path: Path) -> None:
    first = windows_boot_id()
    second = windows_boot_id()
    assert first == second
    assert first.startswith("windows:")

    state_path = tmp_path / "FactLane" / "state" / "webui-service.json"
    service = WebUIServiceState(state_path)
    assert service._current_boot_id() == first
    stopped = service.request_stop("stop-a2")
    assert stopped.status is ServiceStatus.STOPPED_UNTIL_REBOOT
    sid = current_user_sid()
    validate_owner_private_path(state_path.parent, owner_sid=sid, directory=True)
    validate_owner_private_path(state_path, owner_sid=sid, directory=False)

    reopened = WebUIServiceState(state_path)
    assert reopened.snapshot().status is ServiceStatus.STOPPED_UNTIL_REBOOT
    assert reopened.snapshot().stopped_boot_id == first


def test_windows_boot_identity_is_available_from_background_worker_thread() -> None:
    first = windows_boot_id()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        second = executor.submit(windows_boot_id).result(timeout=5.0)
    assert second == first


def test_windows_boot_identity_does_not_uninitialize_caller_owned_mta_apartment() -> None:
    import ctypes
    import pythoncom

    def probe() -> tuple[str, int]:
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        try:
            boot_id = windows_boot_id()
            ole32 = ctypes.windll.ole32
            ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            ole32.CoInitializeEx.restype = ctypes.c_long
            hresult = int(ole32.CoInitializeEx(None, pythoncom.COINIT_MULTITHREADED))
            try:
                return boot_id, hresult
            finally:
                ole32.CoUninitialize()
        finally:
            pythoncom.CoUninitialize()

    first = windows_boot_id()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        second, hresult = executor.submit(probe).result(timeout=5.0)
    assert second == first
    assert hresult == 1  # S_FALSE: the caller-owned MTA apartment was still initialized.


def test_windows_config_and_service_state_validate_owner_dacl_before_first_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import win32file

    sid = current_user_sid()
    original_validate = webui_windows_module.validate_owner_only_kernel_object
    original_write = win32file.WriteFile
    validated_temp_handles: set[str] = set()
    temp_writes: list[str] = []

    def validate_before_write(handle, owner_sid: str) -> None:
        original_validate(handle, owner_sid)
        final_path = str(win32file.GetFinalPathNameByHandle(handle, 0)).casefold()
        if ".tmp" in final_path:
            validated_temp_handles.add(final_path)

    def write_after_validation(handle, payload: bytes):
        final_path = str(win32file.GetFinalPathNameByHandle(handle, 0)).casefold()
        if ".tmp" in final_path:
            assert final_path in validated_temp_handles
            temp_writes.append(final_path)
        return original_write(handle, payload)

    monkeypatch.setattr(webui_windows_module, "validate_owner_only_kernel_object", validate_before_write)
    monkeypatch.setattr(win32file, "WriteFile", write_after_validation)

    config_path = tmp_path / "FactLane" / "config" / "webui.json"
    write_owner_private_text_atomic(str(config_path), '{"probe":"config"}\n', owner_sid=sid)
    validate_owner_private_path(config_path, owner_sid=sid, directory=False)

    service_path = tmp_path / "FactLane" / "state" / "webui-service.json"
    WebUIServiceState(service_path, boot_id=lambda: "windows:dacl-test")
    validate_owner_private_path(service_path, owner_sid=sid, directory=False)

    assert len(temp_writes) >= 2
    assert set(temp_writes).issubset(validated_temp_handles)


def test_windows_atomic_writer_rejects_parent_junction_substitution_before_create_without_redirected_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = current_user_sid()
    parent = tmp_path / "FactLane" / "config"
    target = parent / "webui.json"
    redirect_parent = tmp_path / "redirect" / "config"
    ensure_owner_private_directory(redirect_parent, owner_sid=sid)
    original_open = webui_windows_module._open_windows_parent_guard
    moved_parent = tmp_path / "original-config"
    injected = False

    def substitute_then_open(candidate: Path, *, owner_sid: str):
        nonlocal injected
        if not injected and Path(candidate) == parent:
            injected = True
            os.rename(parent, moved_parent)
            _create_junction(parent, redirect_parent)
        return original_open(candidate, owner_sid=owner_sid)

    monkeypatch.setattr(webui_windows_module, "_open_windows_parent_guard", substitute_then_open)
    try:
        with pytest.raises(WindowsWebUIError) as caught:
            write_owner_private_text_atomic(str(target), "blocked\n", owner_sid=sid)
        assert caught.value.code == "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE"
        assert injected is True
        assert list(redirect_parent.iterdir()) == []
    finally:
        if parent.exists():
            os.rmdir(parent)
        if moved_parent.exists() and not parent.exists():
            os.rename(moved_parent, parent)


def test_windows_atomic_writer_rejects_target_junction_substitution_before_publish_without_redirected_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = current_user_sid()
    target = tmp_path / "FactLane" / "config" / "webui.json"
    redirect = tmp_path / "redirect-target"
    redirect.mkdir()
    original_publish = webui_windows_module._publish_owner_private_temp
    injected = False

    def substitute_then_publish(guard, temporary, destination, *, owner_sid: str) -> None:
        nonlocal injected
        if not injected:
            injected = True
            _create_junction(Path(destination), redirect)
        original_publish(
            guard,
            temporary,
            destination,
            owner_sid=owner_sid,
        )

    monkeypatch.setattr(webui_windows_module, "_publish_owner_private_temp", substitute_then_publish)
    try:
        with pytest.raises(WindowsWebUIError) as caught:
            write_owner_private_text_atomic(str(target), "blocked\n", owner_sid=sid)
        assert caught.value.code == "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE"
        assert injected is True
        assert list(redirect.iterdir()) == []
        assert not any(path.name.endswith(".tmp") for path in target.parent.iterdir())
    finally:
        if target.exists():
            os.rmdir(target)


def test_windows_atomic_writer_retains_exact_temp_handle_and_rejects_leaf_substitution_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import win32api
    import win32con

    sid = current_user_sid()
    target = tmp_path / "FactLane" / "config" / "webui.json"
    attacker = tmp_path / "attacker.json"
    attacker.write_text("attacker\n", encoding="utf-8")
    original_publish = webui_windows_module._publish_owner_private_temp
    attempted_temp: list[Path] = []

    def attack_then_fail_closed(guard, temporary, destination, *, owner_sid: str) -> None:
        attempted_temp.append(temporary.path)
        assert temporary.handle is not None
        with pytest.raises(Exception) as caught:
            win32api.MoveFileEx(
                str(attacker),
                str(temporary.path),
                int(getattr(win32con, "MOVEFILE_REPLACE_EXISTING", 0x1)),
            )
        assert getattr(caught.value, "winerror", None) in {5, 32, 33}
        retained = webui_windows_module._windows_handle_identity(
            temporary.handle,
            expected_path=str(temporary.path),
            directory=False,
        )
        assert webui_windows_module._same_windows_file(retained, temporary.identity)
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "injected temp-leaf substitution was blocked",
        )

    monkeypatch.setattr(webui_windows_module, "_publish_owner_private_temp", attack_then_fail_closed)
    with pytest.raises(WindowsWebUIError) as caught:
        write_owner_private_text_atomic(str(target), "trusted\n", owner_sid=sid)

    assert caught.value.code == "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE"
    assert len(attempted_temp) == 1
    assert attacker.read_text(encoding="utf-8") == "attacker\n"
    assert not target.exists()
    assert not attempted_temp[0].exists()

    monkeypatch.setattr(webui_windows_module, "_publish_owner_private_temp", original_publish)


def test_windows_service_state_parent_junction_substitution_fails_closed_without_redirected_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = current_user_sid()
    parent = tmp_path / "FactLane" / "state"
    state_path = parent / "webui-service.json"
    redirect_parent = tmp_path / "redirect-state"
    ensure_owner_private_directory(redirect_parent, owner_sid=sid)
    original_open = webui_windows_module._open_windows_parent_guard
    moved_parent = tmp_path / "original-state"
    injected = False

    def substitute_then_open(candidate: Path, *, owner_sid: str):
        nonlocal injected
        if not injected and Path(candidate) == parent:
            injected = True
            os.rename(parent, moved_parent)
            _create_junction(parent, redirect_parent)
        return original_open(candidate, owner_sid=owner_sid)

    monkeypatch.setattr(webui_windows_module, "_open_windows_parent_guard", substitute_then_open)
    try:
        with pytest.raises(ServiceStateError, match="persisted safely"):
            WebUIServiceState(state_path, boot_id=lambda: "windows:race-test")
        assert injected is True
        assert list(redirect_parent.iterdir()) == []
    finally:
        if parent.exists():
            os.rmdir(parent)
        if moved_parent.exists() and not parent.exists():
            os.rename(moved_parent, parent)


class _Collection:
    def __init__(self, values):
        self._values = list(values)
        self.Count = len(self._values)

    def Item(self, index: int):
        return self._values[index - 1]


class _FakeTaskRoot:
    def __init__(self, task) -> None:
        self.task = task

    def GetTask(self, _name: str):
        return self.task


class _FakeTaskService:
    def __init__(self, task) -> None:
        self.root = _FakeTaskRoot(task)
        self.connected = False

    def Connect(self) -> None:
        self.connected = True

    def GetFolder(self, path: str):
        assert path == "\\"
        return self.root


def test_windows_scheduler_observation_binds_engine_pid_and_process_identity() -> None:
    sid = current_user_sid()
    identity = process_identity(os.getpid())
    executable, environment_executable = windows_managed_python_executables()
    user_id = os.environ.get("USERNAME") or "moham"
    arguments = windows_managed_python_arguments(environment_executable, "serve")
    action = SimpleNamespace(Type=0, Path=executable, Arguments=arguments)
    trigger = SimpleNamespace(Type=9, UserId=user_id, Enabled=True)
    settings = SimpleNamespace(Enabled=True, AllowDemandStart=True, MultipleInstances=2, ExecutionTimeLimit="PT0S")
    principal = SimpleNamespace(UserId=user_id, LogonType=3, RunLevel=0)
    definition = SimpleNamespace(
        Principal=principal,
        Actions=_Collection([action]),
        Triggers=_Collection([trigger]),
        Settings=settings,
    )
    task = SimpleNamespace(
        Definition=definition,
        State=4,
        Enabled=True,
        GetInstances=lambda _flags: [SimpleNamespace(EnginePID=os.getpid())],
        GetSecurityDescriptor=lambda _flags: f"O:{sid}D:P(A;;GA;;;{sid})(A;;GA;;;SY)",
    )
    supervisor = WindowsTaskSchedulerSupervisor(
        task_name="FactLane A2 Observation",
        expected_executable=executable,
        environment_executable=environment_executable,
        owner_sid=sid,
        user_id=user_id,
        service_factory=lambda: _FakeTaskService(task),
    )

    observed = supervisor.observe()
    assert observed.active is True
    assert observed.engine_pid == os.getpid()
    assert observed.process_identity == identity


def test_windows_scheduler_rejects_tampered_same_name_task_executable() -> None:
    sid = current_user_sid()
    executable, environment_executable = windows_managed_python_executables()
    user_id = os.environ.get("USERNAME") or "moham"
    arguments = windows_managed_python_arguments(environment_executable, "serve")
    action = SimpleNamespace(Type=0, Path=r"C:\Windows\System32\cmd.exe", Arguments=arguments)
    trigger = SimpleNamespace(Type=9, UserId=user_id, Enabled=True)
    settings = SimpleNamespace(Enabled=True, AllowDemandStart=True, MultipleInstances=2, ExecutionTimeLimit="PT0S")
    principal = SimpleNamespace(UserId=user_id, LogonType=3, RunLevel=0)
    task = SimpleNamespace(
        Definition=SimpleNamespace(
            Principal=principal,
            Actions=_Collection([action]),
            Triggers=_Collection([trigger]),
            Settings=settings,
        ),
        State=3,
        Enabled=True,
        GetInstances=lambda _flags: [],
        GetSecurityDescriptor=lambda _flags: f"O:{sid}D:P(A;;GA;;;{sid})(A;;GA;;;SY)",
    )
    supervisor = WindowsTaskSchedulerSupervisor(
        task_name="FactLane A2 Tampered",
        expected_executable=executable,
        environment_executable=environment_executable,
        owner_sid=sid,
        user_id=user_id,
        service_factory=lambda: _FakeTaskService(task),
    )

    with pytest.raises(WindowsWebUIError) as caught:
        supervisor.observe()

    assert caught.value.code == "WINDOWS_TASK_DEFINITION_UNSAFE"


def test_windows_scheduler_registers_exact_disposable_current_user_task_and_deletes_it() -> None:
    task_name = f"FactLane A2 Disposable {os.getpid()} {time.time_ns()}"
    executable, environment_executable = windows_managed_python_executables()
    supervisor = WindowsTaskSchedulerSupervisor(
        task_name=task_name,
        expected_executable=executable,
        environment_executable=environment_executable,
    )
    try:
        plan = supervisor.register_or_update(executable)
        assert plan.arguments == windows_managed_python_arguments(environment_executable, "serve")
        assert plan.logon_type == 3
        assert plan.run_level == 0
        assert plan.multiple_instances == 2
        assert plan.execution_time_limit == "PT0S"
        observed = supervisor.observe()
        assert observed.enabled is True
        assert observed.active is False
        assert observed.engine_pid is None
    finally:
        supervisor.delete()


def test_windows_scheduler_observe_is_com_safe_from_background_thread() -> None:
    task_name = f"FactLane A2 Background COM {os.getpid()} {time.time_ns()}"
    executable, environment_executable = windows_managed_python_executables()
    supervisor = WindowsTaskSchedulerSupervisor(
        task_name=task_name,
        expected_executable=executable,
        environment_executable=environment_executable,
    )
    try:
        supervisor.register_or_update(executable)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            observations = [executor.submit(supervisor.observe).result() for _ in range(3)]
        assert all(observation.enabled is True for observation in observations)
        assert all(observation.active is False for observation in observations)
        assert all(observation.engine_pid is None for observation in observations)
    finally:
        supervisor.delete()


def test_windows_scheduler_failure_releases_callback_frame_before_owned_com_uninitialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pythoncom

    events: list[str] = []
    monkeypatch.setattr(
        pythoncom,
        "CoInitializeEx",
        lambda _mode: events.append("initialize"),
    )
    monkeypatch.setattr(
        pythoncom,
        "CoUninitialize",
        lambda: events.append("uninitialize"),
    )

    class Service:
        def Connect(self) -> None:
            events.append("connect")

    class Proxy:
        def __del__(self) -> None:
            events.append("proxy_released")

    executable, environment_executable = windows_managed_python_executables()
    supervisor = WindowsTaskSchedulerSupervisor(
        task_name=f"FactLane A2 COM Failure {os.getpid()} {time.time_ns()}",
        expected_executable=executable,
        environment_executable=environment_executable,
        service_factory=Service,
    )

    def fail_with_proxy(_service) -> None:
        proxy = Proxy()
        assert proxy is not None
        raise WindowsWebUIError(
            "WINDOWS_TASK_DEFINITION_UNSAFE",
            "scheduled task definition is unsafe",
        )

    with pytest.raises(WindowsWebUIError) as caught:
        supervisor._run_with_service(fail_with_proxy)

    assert caught.value.code == "WINDOWS_TASK_DEFINITION_UNSAFE"
    assert events.index("proxy_released") < events.index("uninitialize")


def test_default_windows_supervisor_fails_typed_when_factlane_executable_is_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webui_platform_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(webui_platform_module.sys, "argv", ["factlane-unresolved"])
    monkeypatch.setattr(webui_platform_module.sys, "executable", r"C:\FactLane-Does-Not-Exist\python.exe")

    with pytest.raises(WindowsWebUIError) as caught:
        webui_platform_module._default_windows_task_supervisor()

    assert caught.value.code == "WINDOWS_FACTLANE_EXECUTABLE_UNAVAILABLE"


def test_windows_controller_dispatch_preserves_typed_supervisor_failure(tmp_path: Path) -> None:
    native = windows_owner_paths(environ={"LOCALAPPDATA": str(tmp_path / "LocalAppData")})
    paths = OwnerPaths(
        owner_config_home=native.owner_config_home,
        service_config_path=native.service_config_path,
        caddy_config_path=str(Path(native.owner_config_home) / "transport.json"),
        user_unit_path=native.task_receipt_path,
        runtime_dir=native.runtime_dir,
        backend_socket=native.backend_endpoint,
        native_control_socket=native.native_control_socket,
        state_dir=native.state_dir,
        caddy_state_dir=str(Path(native.state_dir) / "transport"),
        browser_profile_dir=native.browser_profile_dir,
    )

    class BrokenSupervisor:
        def observe(self):
            raise WindowsWebUIError(
                "WINDOWS_TASK_DEFINITION_UNSAFE",
                "scheduled task executable or arguments changed",
            )

        def start(self):
            pytest.fail("start must not be reached")

    controller = ProductionWebUIController(
        paths=paths,
        platform_system="Windows",
        service_manager=BrokenSupervisor(),  # type: ignore[arg-type]
    )

    result = controller.dispatch("status")

    assert result == {
        "status": "ERROR",
        "error_code": "WINDOWS_TASK_DEFINITION_UNSAFE",
        "message": "scheduled task executable or arguments changed",
    }


class _FakeSupervisor:
    def __init__(self, identity, service_state: WebUIServiceState | None = None) -> None:
        self.identity = identity
        self.active = True
        self.starts = 0
        self.service_state = service_state

    def observe(self):
        return WindowsTaskObservation(
            active=self.active,
            enabled=True,
            active_state="running" if self.active else "ready",
            enabled_state="enabled",
            engine_pid=self.identity.pid if self.active else None,
            process_identity=self.identity if self.active else None,
        )

    def start(self) -> None:
        self.starts += 1
        self.active = True
        if self.service_state is not None:
            self.service_state.mark_ready()


def test_windows_controller_binds_live_control_to_scheduler_identity_and_restart_is_graceful(tmp_path: Path) -> None:
    identity = process_identity(os.getpid())
    native = windows_owner_paths(environ={"LOCALAPPDATA": str(tmp_path / "LocalAppData")})
    paths = OwnerPaths(
        owner_config_home=native.owner_config_home,
        service_config_path=native.service_config_path,
        caddy_config_path=str(Path(native.owner_config_home) / "transport.json"),
        user_unit_path=native.task_receipt_path,
        runtime_dir=native.runtime_dir,
        backend_socket=native.backend_endpoint,
        native_control_socket=native.native_control_socket,
        state_dir=native.state_dir,
        caddy_state_dir=str(Path(native.state_dir) / "transport"),
        browser_profile_dir=native.browser_profile_dir,
    )
    service_state = WebUIServiceState(
        Path(paths.state_dir) / "webui-service.json",
        boot_id=lambda: "windows:test-boot",
    )
    service_state.mark_ready()
    supervisor = _FakeSupervisor(identity, service_state)
    calls: list[tuple[str, object]] = []

    class Client:
        def start(self, *, request_id: str):
            calls.append(("start", request_id))
            return {"status": "READY"}

        def stop(self, *, request_id: str):
            calls.append(("stop", request_id))
            service_state.request_stop(request_id)
            supervisor.active = False
            return {"status": "STOPPED_UNTIL_REBOOT"}

        def restart(self, *, request_id: str):
            pytest.fail(f"Windows restart must not use in-process restart RPC: {request_id}")

        def status(self):
            return {"status": "READY", "saved_endpoint": "https://factlane-a2.test:8765"}

        def approve_pending_sensitive(self):
            return None

    bound: list[object] = []
    controller = ProductionWebUIController(
        paths=paths,
        platform_system="Windows",
        service_manager=supervisor,
        service_state_factory=lambda _path: service_state,
        windows_control_client_factory=lambda _path, process: bound.append(process) or Client(),
        request_id_factory=iter(["ignored", "stop-restart", "start-restart"]).__next__,
        startup_timeout=1.0,
        startup_poll_interval=0.01,
    )

    result = controller.dispatch("restart")
    assert result["status"] == "OK"
    assert bound == [identity]
    assert calls == [("stop", "stop-restart")]
    assert supervisor.starts == 1
    assert supervisor.active is True
    assert service_state.snapshot().status is ServiceStatus.READY


def test_windows_setup_plan_and_apply_have_no_linux_caddy_hosts_or_lingering_requirements(tmp_path: Path) -> None:
    local_appdata = tmp_path / "LocalAppData"
    appdata = tmp_path / "RoamingAppData"
    executable = _factlane_executable()
    request = build_default_setup_request(
        environ={"LOCALAPPDATA": str(local_appdata), "APPDATA": str(appdata)},
        installing_user="test-user",
        factlane_executable=executable,
        installation_id_factory=lambda: "a2native123",
    )
    browser = WindowsBrowserInstall(family="firefox", executable=executable)
    environment = InstallEnvironment(
        system="Windows",
        systemd=False,
        caddy_path=None,
        zenity_path=None,
        browser=browser,
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
    assert receipt.plan is not None
    kinds = [operation.kind for operation in receipt.plan.operations]
    assert kinds == [
        "ENSURE_WINDOWS_OWNER_STATE",
        "WRITE_SERVICE_CONFIG",
        "REGISTER_WINDOWS_TASK",
        "INSTALL_OWNER_LAUNCHER",
    ]
    serialized = repr(receipt.plan)
    for forbidden in ("/etc/hosts", "systemd", "zenity", "ENABLE_USER_LINGERING", "WRITE_CADDY_CONFIG"):
        assert forbidden not in serialized

    registered: list[str] = []

    class Supervisor:
        def register_or_update(self, factlane_executable: str):
            registered.append(factlane_executable)
            return None

    assert request.launcher_path is not None
    Path(request.launcher_path).parent.mkdir(parents=True, exist_ok=True)
    executor = build_default_setup_executor(
        request,
        environment,
        windows_supervisor=Supervisor(),  # type: ignore[arg-type]
    )
    applied = executor(receipt, consent)
    assert applied.status is InstallerStepStatus.APPLIED
    assert applied.endpoint_state is EndpointState.PROPOSED
    expected_base, _environment = windows_managed_python_executables(environment_executable=executable)
    assert registered == [expected_base]
    config = load_windows_managed_config(request.service_config_path)
    assert config is not None
    assert config.origin == "https://factlane-a2native123.test:8765"
    validate_owner_private_path(request.service_config_path, owner_sid=current_user_sid(), directory=False)
    assert request.launcher_path is not None
    launcher = Path(request.launcher_path)
    assert launcher.is_file()
    assert launcher.name == "FactLane Owner Panel.lnk"


def test_windows_setup_requires_explicit_native_confirmation(tmp_path: Path) -> None:
    executable = _factlane_executable()
    request = build_default_setup_request(
        environ={
            "LOCALAPPDATA": str(tmp_path / "LocalAppData"),
            "APPDATA": str(tmp_path / "RoamingAppData"),
        },
        installing_user="test-user",
        factlane_executable=executable,
        installation_id_factory=lambda: "a2denied123",
    )
    environment = InstallEnvironment(
        system="Windows",
        systemd=False,
        caddy_path=None,
        zenity_path=None,
        browser=WindowsBrowserInstall(family="firefox", executable=executable),
        euid=-1,
    )
    denied = SetupConsent(False, False, False, windows_native_setup=False)
    receipt = plan_webui_setup(request, environment, denied)
    assert receipt.status == "BLOCKED"
    assert receipt.plan is None
    assert any("explicit consent" in reason for reason in receipt.blocked_reasons)


def test_windows_browser_launcher_uses_exact_isolated_firefox_profile_without_bypass(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class Process:
        def poll(self):
            return None

        def wait(self, *, timeout):
            assert timeout > 0
            return 0

    def launch(argv, **kwargs):
        calls.append((tuple(argv), dict(kwargs)))
        return Process()

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=launch,
        profile_active_probe=lambda: True,
    )
    assert launcher("https://factlane-a2native123.test:8765#connect=locator") is True
    assert len(calls) == 2
    probe_argv, probe_kwargs = calls[0]
    connect_argv, connect_kwargs = calls[1]
    assert probe_argv == (
        executable,
        "-no-remote",
        "-profile",
        str(profile),
        "-new-window",
        "about:blank",
    )
    assert connect_argv == (
        executable,
        "-no-remote",
        "-profile",
        str(profile),
        "-new-window",
        "https://factlane-a2native123.test:8765#connect=locator",
    )
    lowered = " ".join(probe_argv + connect_argv).casefold()
    assert "ignore-certificate" not in lowered
    assert "accept-insecure" not in lowered
    assert probe_kwargs["shell"] is False
    assert connect_kwargs["shell"] is False
    validate_owner_private_path(profile, owner_sid=current_user_sid(), directory=True)


def test_windows_browser_launcher_cold_profile_warms_probes_then_delivers_connect_once(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []
    states = iter((False, False, True, True))

    class Process:
        def __init__(self, *, wait_code: int = 0) -> None:
            self.wait_code = wait_code

        def poll(self):
            return None

        def wait(self, *, timeout):
            assert 0 < timeout <= 10.0
            return self.wait_code

    def launch(argv, **kwargs):
        assert kwargs["shell"] is False
        calls.append(tuple(argv))
        return Process()

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=launch,
        profile_active_probe=lambda: next(states),
        sleep=lambda _seconds: None,
    )

    connect = "https://factlane-coldready123.test:8765#connect=single-locator"
    assert launcher(connect) is True
    assert calls == [
        (executable, "-no-remote", "-profile", str(profile), "-new-window", "about:blank"),
        (executable, "-no-remote", "-profile", str(profile), "-new-window", "about:blank"),
        (executable, "-no-remote", "-profile", str(profile), "-new-window", connect),
    ]
    assert sum("#connect=" in " ".join(argv) for argv in calls) == 1


def test_windows_browser_launcher_readiness_timeout_never_delivers_locator(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []

    class Clock:
        def __init__(self) -> None:
            self.value = 50.0

        def __call__(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += seconds

    class Process:
        def poll(self):
            return None

    clock = Clock()

    def launch(argv, **_kwargs):
        calls.append(tuple(argv))
        return Process()

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=launch,
        profile_active_probe=lambda: False,
        clock=clock,
        sleep=clock.sleep,
        readiness_timeout=1.0,
        readiness_poll_interval=0.2,
    )

    connect = "https://factlane-timeout123.test:8765#connect=must-not-appear"
    assert launcher(connect) is False
    assert len(calls) == 1
    assert calls[0][-1] == "about:blank"
    assert all("#connect=" not in " ".join(argv) for argv in calls)


@pytest.mark.parametrize("probe_exit", [1, 7])
def test_windows_browser_launcher_probe_failure_never_delivers_locator(tmp_path: Path, probe_exit: int) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []
    states = iter((False, True))

    class Process:
        def __init__(self, role: str) -> None:
            self.role = role

        def poll(self):
            return None

        def wait(self, *, timeout):
            assert timeout > 0
            return probe_exit if self.role == "probe" else 0

    def launch(argv, **_kwargs):
        calls.append(tuple(argv))
        return Process("warm" if len(calls) == 1 else "probe")

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=launch,
        profile_active_probe=lambda: next(states),
        sleep=lambda _seconds: None,
    )

    assert launcher("https://factlane-probefail123.test:8765#connect=must-not-appear") is False
    assert [argv[-1] for argv in calls] == ["about:blank", "about:blank"]
    assert all("#connect=" not in " ".join(argv) for argv in calls)


def test_windows_browser_launcher_lock_loss_after_probe_never_delivers_locator(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []
    states = iter((False, True, False))

    class Process:
        def poll(self):
            return None

        def wait(self, *, timeout):
            assert timeout > 0
            return 0

    def launch(argv, **_kwargs):
        calls.append(tuple(argv))
        return Process()

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=launch,
        profile_active_probe=lambda: next(states),
        sleep=lambda _seconds: None,
    )

    assert launcher("https://factlane-lockloss123.test:8765#connect=must-not-appear") is False
    assert [argv[-1] for argv in calls] == ["about:blank", "about:blank"]


def test_windows_browser_launcher_allows_clean_warm_handoff_before_profile_lock(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []
    states = iter((False, False, True, True))

    class Process:
        def __init__(self, role: str) -> None:
            self.role = role

        def poll(self):
            return 0 if self.role == "warm" else None

        def wait(self, *, timeout):
            assert timeout > 0
            return 0

    def launch(argv, **_kwargs):
        calls.append(tuple(argv))
        role = "warm" if len(calls) == 1 else "probe" if len(calls) == 2 else "connect"
        return Process(role)

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=launch,
        profile_active_probe=lambda: next(states),
        sleep=lambda _seconds: None,
    )

    connect = "https://factlane-cleanhandoff123.test:8765#connect=single-locator"
    assert launcher(connect) is True
    assert [argv[-1] for argv in calls] == ["about:blank", "about:blank", connect]
    assert sum("#connect=" in " ".join(argv) for argv in calls) == 1


def test_windows_browser_launcher_allows_clean_warm_handoff_after_profile_lock(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []
    states = iter((False, True, True))
    warm_polls = iter((None, 0, 0, 0))

    class Process:
        def __init__(self, role: str) -> None:
            self.role = role

        def poll(self):
            if self.role == "warm":
                return next(warm_polls)
            return None

        def wait(self, *, timeout):
            assert timeout > 0
            return 0

    def launch(argv, **_kwargs):
        calls.append(tuple(argv))
        role = "warm" if len(calls) == 1 else "probe" if len(calls) == 2 else "connect"
        return Process(role)

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=launch,
        profile_active_probe=lambda: next(states),
    )

    connect = "https://factlane-postlockhandoff123.test:8765#connect=single-locator"
    assert launcher(connect) is True
    assert [argv[-1] for argv in calls] == ["about:blank", "about:blank", connect]


@pytest.mark.parametrize("exit_code", [1, 9])
def test_windows_browser_launcher_nonzero_warm_exit_never_delivers_locator(
    tmp_path: Path,
    exit_code: int,
) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []

    class Process:
        def poll(self):
            return exit_code

    def launch(argv, **_kwargs):
        calls.append(tuple(argv))
        return Process()

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=launch,
        profile_active_probe=lambda: False,
    )

    assert launcher("https://factlane-warmfail123.test:8765#connect=must-not-appear") is False
    assert [argv[-1] for argv in calls] == ["about:blank"]


def test_windows_browser_launcher_probe_timeout_never_delivers_locator(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []
    states = iter((False, True))

    class Process:
        def poll(self):
            return None

        def wait(self, *, timeout):
            raise subprocess.TimeoutExpired("firefox", timeout)

    def launch(argv, **_kwargs):
        calls.append(tuple(argv))
        return Process()

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=launch,
        profile_active_probe=lambda: next(states),
    )

    assert launcher("https://factlane-probetimeout123.test:8765#connect=must-not-appear") is False
    assert [argv[-1] for argv in calls] == ["about:blank", "about:blank"]


def test_windows_browser_launcher_ambiguous_profile_state_never_spawns_browser(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []

    def probe():
        raise WindowsWebUIError("WINDOWS_BROWSER_PROFILE_STATE_UNAVAILABLE", "ambiguous")

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=lambda argv, **_kwargs: calls.append(tuple(argv)),
        profile_active_probe=probe,
    )

    assert launcher("https://factlane-ambiguous123.test:8765#connect=must-not-appear") is False
    assert calls == []


def test_windows_browser_launcher_spawn_failure_never_retries_locator(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []

    class Probe:
        def poll(self):
            return None

        def wait(self, *, timeout):
            assert timeout > 0
            return 0

    def launch(argv, **_kwargs):
        calls.append(tuple(argv))
        if len(calls) == 1:
            return Probe()
        raise OSError("spawn failed")

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=launch,
        profile_active_probe=lambda: True,
    )

    assert launcher("https://factlane-spawnfail123.test:8765#connect=single-locator") is False
    assert [argv[-1] for argv in calls] == ["about:blank", "https://factlane-spawnfail123.test:8765#connect=single-locator"]
    assert sum("#connect=" in " ".join(argv) for argv in calls) == 1


def test_windows_browser_launcher_expiry_after_probe_never_delivers_locator(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []

    class Clock:
        def __init__(self) -> None:
            self.value = 100.0

        def __call__(self) -> float:
            return self.value

    class Probe:
        def poll(self):
            return None

        def wait(self, *, timeout):
            assert timeout == pytest.approx(1.0)
            clock.value += timeout
            return 0

    clock = Clock()

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=lambda argv, **_kwargs: calls.append(tuple(argv)) or Probe(),
        profile_active_probe=lambda: True,
        clock=clock,
        readiness_timeout=1.0,
    )

    assert launcher("https://factlane-expiry123.test:8765#connect=must-not-appear") is False
    assert [argv[-1] for argv in calls] == ["about:blank"]


def test_windows_browser_launcher_clean_warm_exit_without_profile_lock_times_out_without_locator(
    tmp_path: Path,
) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []

    class Clock:
        def __init__(self) -> None:
            self.value = 200.0

        def __call__(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += seconds

    class WarmProcess:
        def poll(self):
            return 0

    clock = Clock()

    def launch(argv, **_kwargs):
        calls.append(tuple(argv))
        return WarmProcess()

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=launch,
        profile_active_probe=lambda: False,
        clock=clock,
        sleep=clock.sleep,
        readiness_timeout=1.0,
        readiness_poll_interval=0.25,
    )

    connect = "https://factlane-cleanexit-timeout123.test:8765#connect=must-not-appear"
    assert launcher(connect) is False
    assert clock.value == pytest.approx(201.0)
    assert [argv[-1] for argv in calls] == ["about:blank"]
    assert all("#connect=" not in " ".join(argv) for argv in calls)


def test_windows_browser_launcher_warmup_consumes_deadline_and_probe_gets_only_remaining_budget(
    tmp_path: Path,
) -> None:
    executable = str(Path(sys.executable).resolve())
    profile = tmp_path / "FactLane" / "browser"
    calls: list[tuple[str, ...]] = []
    states = iter((False, False, True, True))
    probe_timeouts: list[float] = []

    class Clock:
        def __init__(self) -> None:
            self.value = 300.0

        def __call__(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.value += seconds

    class Process:
        def __init__(self, role: str) -> None:
            self.role = role

        def poll(self):
            return None

        def wait(self, *, timeout):
            assert self.role == "probe"
            probe_timeouts.append(timeout)
            return 0

    clock = Clock()

    def launch(argv, **_kwargs):
        calls.append(tuple(argv))
        role = "warm" if len(calls) == 1 else "probe" if len(calls) == 2 else "connect"
        return Process(role)

    launcher = WindowsBrowserLauncher(
        WindowsBrowserInstall(family="firefox", executable=executable),
        str(profile),
        process_factory=launch,
        profile_active_probe=lambda: next(states),
        clock=clock,
        sleep=clock.sleep,
        readiness_timeout=1.0,
        readiness_poll_interval=0.4,
    )

    connect = "https://factlane-remaining-budget123.test:8765#connect=single-locator"
    assert launcher(connect) is True
    assert clock.value == pytest.approx(300.4)
    assert probe_timeouts == [pytest.approx(0.6)]
    assert [argv[-1] for argv in calls] == ["about:blank", "about:blank", connect]
    assert sum("#connect=" in " ".join(argv) for argv in calls) == 1


def test_windows_managed_setup_rejects_chromium_even_when_present(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    request = build_default_setup_request(
        environ={
            "LOCALAPPDATA": str(tmp_path / "LocalAppData"),
            "APPDATA": str(tmp_path / "RoamingAppData"),
        },
        installing_user="test-user",
        factlane_executable=executable,
        installation_id_factory=lambda: "firefoxrequired123",
    )
    environment = InstallEnvironment(
        system="Windows",
        systemd=False,
        caddy_path=None,
        zenity_path=None,
        browser=WindowsBrowserInstall(family="chromium", executable=executable),
        euid=-1,
    )

    receipt = plan_webui_setup(
        request,
        environment,
        SetupConsent(False, False, False, windows_native_setup=True),
    )

    assert receipt.status == "BLOCKED"
    assert receipt.plan is None
    assert receipt.blocked_reasons == ("Mozilla Firefox is required for managed Windows WebUI",)


def test_windows_browser_discovery_does_not_fall_back_to_edge_or_chrome() -> None:
    executable = str(Path(sys.executable).resolve())

    browser = discover_windows_browser(
        environ={},
        which=lambda name: executable if name in {"msedge.exe", "chrome.exe"} else None,
    )

    assert browser is None


def test_windows_production_browser_resolver_loads_live_tls_and_provisions_firefox_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = str(Path(sys.executable).resolve())
    state_dir = tmp_path / "FactLane" / "state"
    profile_dir = tmp_path / "FactLane" / "browser"
    material = object()
    calls: list[tuple[str, object]] = []
    browser = WindowsBrowserInstall(family="firefox", executable=executable)

    monkeypatch.setattr(webui_platform_module, "discover_windows_browser", lambda **_kwargs: browser)
    monkeypatch.setattr(
        webui_platform_module,
        "load_windows_tls_material",
        lambda root, hostname: calls.append(("load", (Path(root), hostname))) or material,
    )
    monkeypatch.setattr(
        webui_platform_module,
        "ensure_firefox_profile_network_policy",
        lambda **kwargs: calls.append(("network", kwargs)) or Path(kwargs["profile_dir"]) / "user.js",
    )
    monkeypatch.setattr(
        webui_platform_module,
        "ensure_firefox_profile_trust",
        lambda **kwargs: calls.append(("trust", kwargs)) or object(),
    )

    launcher = webui_platform_module._resolve_production_browser_launcher(
        origin="https://factlane-resolver123.test:8765",
        state_dir=str(state_dir),
        profile_dir=str(profile_dir),
    )

    assert isinstance(launcher, WindowsBrowserLauncher)
    assert launcher.browser == browser
    assert launcher.profile_dir == str(profile_dir)
    assert calls[0] == ("load", (state_dir / "tls", "factlane-resolver123.test"))
    assert calls[1] == (
        "network",
        {"profile_dir": str(profile_dir), "hostname": "factlane-resolver123.test"},
    )
    assert calls[2][0] == "trust"
    assert calls[2][1]["firefox_executable"] == executable
    assert calls[2][1]["profile_dir"] == str(profile_dir)
    assert calls[2][1]["tls_material"] is material


def test_windows_open_passes_exact_origin_state_and_profile_to_production_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = process_identity(os.getpid())
    native = windows_owner_paths(environ={"LOCALAPPDATA": str(tmp_path / "LocalAppData")})
    paths = OwnerPaths(
        owner_config_home=native.owner_config_home,
        service_config_path=native.service_config_path,
        caddy_config_path=str(Path(native.owner_config_home) / "transport.json"),
        user_unit_path=native.task_receipt_path,
        runtime_dir=native.runtime_dir,
        backend_socket=native.backend_endpoint,
        native_control_socket=native.native_control_socket,
        state_dir=native.state_dir,
        caddy_state_dir=str(Path(native.state_dir) / "transport"),
        browser_profile_dir=native.browser_profile_dir,
    )
    origin = "https://factlane-open123.test:8765"
    resolver_calls: list[dict[str, str | None]] = []
    opened: list[str] = []

    class Client:
        def status(self):
            return {"status": "READY", "saved_endpoint": origin}

        def approve_pending_sensitive(self):
            raise WebUIPlatformError("SENSITIVE_CHALLENGE_NONE", "none")

        def begin_local_enrollment(self, *, device_label: str):
            assert device_label == "FactLane local browser"
            return PendingEnrollment(
                locator="local-locator",
                verification_phrase="amber cedar river willow zephyr",
                expires_at=time.monotonic() + 30.0,
            )

        def approve_pending(self, *, locator: str):
            assert locator == "local-locator"
            return None

    def resolve(**kwargs):
        resolver_calls.append(dict(kwargs))
        return lambda url: opened.append(url) or True

    monkeypatch.setattr(webui_platform_module, "_resolve_production_browser_launcher", resolve)
    controller = ProductionWebUIController(
        paths=paths,
        platform_system="Windows",
        service_manager=_FakeSupervisor(identity),
        windows_control_client_factory=lambda _path, _process: Client(),
    )

    result = controller.dispatch("open")

    assert result == {"status": "OK", "state": "OPENED", "origin": origin}
    assert resolver_calls == [
        {
            "origin": origin,
            "state_dir": paths.state_dir,
            "profile_dir": paths.browser_profile_dir,
        }
    ]
    assert opened == [f"{origin}#connect=local-locator"]


def test_windows_open_accepts_inflight_human_approval_after_more_than_ten_seconds(
    tmp_path: Path,
) -> None:
    identity = process_identity(os.getpid())
    native = windows_owner_paths(environ={"LOCALAPPDATA": str(tmp_path / "LocalAppData")})
    paths = OwnerPaths(
        owner_config_home=native.owner_config_home,
        service_config_path=native.service_config_path,
        caddy_config_path=str(Path(native.owner_config_home) / "transport.json"),
        user_unit_path=native.task_receipt_path,
        runtime_dir=native.runtime_dir,
        backend_socket=native.backend_endpoint,
        native_control_socket=native.native_control_socket,
        state_dir=native.state_dir,
        caddy_state_dir=str(Path(native.state_dir) / "transport"),
        browser_profile_dir=native.browser_profile_dir,
    )
    origin = "https://factlane-delay123.test:8765"
    opened: list[str] = []
    approvals: list[str] = []

    class Clock:
        def __init__(self) -> None:
            self.value = 100.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()

    class Client:
        def status(self):
            return {"status": "READY", "saved_endpoint": origin}

        def approve_pending_sensitive(self):
            raise WebUIPlatformError("SENSITIVE_CHALLENGE_NONE", "none")

        def begin_local_enrollment(self, *, device_label: str):
            assert device_label == "FactLane local browser"
            return PendingEnrollment(
                locator="delayed-locator",
                verification_phrase="amber cedar river willow zephyr",
                expires_at=clock.value + 30.0,
            )

        def approve_pending(self, *, locator: str):
            approvals.append(locator)
            clock.value += 11.0
            return None

    controller = ProductionWebUIController(
        paths=paths,
        platform_system="Windows",
        service_manager=_FakeSupervisor(identity),
        windows_control_client_factory=lambda _path, _process: Client(),
        browser_launcher=lambda url: opened.append(url) or True,
        clock=clock,
    )

    result = controller.dispatch("open")

    assert result == {"status": "OK", "state": "OPENED", "origin": origin}
    assert approvals == ["delayed-locator"]
    assert clock.value == 111.0
    assert opened == [f"{origin}#connect=delayed-locator"]


def test_windows_open_does_not_retry_ambiguous_approval_timeout(
    tmp_path: Path,
) -> None:
    identity = process_identity(os.getpid())
    native = windows_owner_paths(environ={"LOCALAPPDATA": str(tmp_path / "LocalAppData")})
    paths = OwnerPaths(
        owner_config_home=native.owner_config_home,
        service_config_path=native.service_config_path,
        caddy_config_path=str(Path(native.owner_config_home) / "transport.json"),
        user_unit_path=native.task_receipt_path,
        runtime_dir=native.runtime_dir,
        backend_socket=native.backend_endpoint,
        native_control_socket=native.native_control_socket,
        state_dir=native.state_dir,
        caddy_state_dir=str(Path(native.state_dir) / "transport"),
        browser_profile_dir=native.browser_profile_dir,
    )
    origin = "https://factlane-timeout123.test:8765"
    approvals: list[str] = []

    class Client:
        def status(self):
            return {"status": "READY", "saved_endpoint": origin}

        def approve_pending_sensitive(self):
            raise WebUIPlatformError("SENSITIVE_CHALLENGE_NONE", "none")

        def begin_local_enrollment(self, *, device_label: str):
            assert device_label == "FactLane local browser"
            return PendingEnrollment(
                locator="timeout-locator",
                verification_phrase="amber cedar river willow zephyr",
                expires_at=time.monotonic() + 30.0,
            )

        def approve_pending(self, *, locator: str):
            approvals.append(locator)
            raise WebUIPlatformError(
                "CONTROL_UNAVAILABLE",
                "native control response timed out",
            )

    controller = ProductionWebUIController(
        paths=paths,
        platform_system="Windows",
        service_manager=_FakeSupervisor(identity),
        windows_control_client_factory=lambda _path, _process: Client(),
        browser_launcher=lambda _url: True,
    )

    result = controller.dispatch("open")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "CONTROL_UNAVAILABLE"
    assert approvals == ["timeout-locator"]


def test_windows_serve_route_runs_native_backend_owned_https_without_caddy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_appdata = tmp_path / "LocalAppData"
    appdata = tmp_path / "RoamingAppData"
    executable = _factlane_executable()
    request = build_default_setup_request(
        environ={"LOCALAPPDATA": str(local_appdata), "APPDATA": str(appdata)},
        installing_user="test-user",
        factlane_executable=executable,
        installation_id_factory=lambda: "a2serve123",
    )
    environment = InstallEnvironment(
        system="Windows",
        systemd=False,
        caddy_path=None,
        zenity_path=None,
        browser=WindowsBrowserInstall(family="firefox", executable=executable),
        euid=-1,
    )
    consent = SetupConsent(False, False, False, windows_native_setup=True)
    receipt = plan_webui_setup(request, environment, consent)

    class Supervisor:
        def register_or_update(self, _executable: str):
            return None

    assert request.launcher_path is not None
    Path(request.launcher_path).parent.mkdir(parents=True, exist_ok=True)
    result = build_default_setup_executor(
        request,
        environment,
        windows_supervisor=Supervisor(),  # type: ignore[arg-type]
    )(receipt, consent)
    assert result.status is InstallerStepStatus.APPLIED

    paths = default_owner_paths(environ={"LOCALAPPDATA": str(local_appdata)})
    calls: dict[str, object] = {}
    events: list[str] = []

    def ensure_tls(root, hostname):
        events.append("ensure_tls")
        calls["tls_root"] = root
        calls["tls_hostname"] = hostname
        return object()

    monkeypatch.setattr(webui_platform_module, "ensure_windows_tls_material", ensure_tls)

    backend_factory = lambda _spec, _service_state: pytest.fail("fake runner must not launch backend")

    class FakeRunner:
        def __init__(
            self,
            config,
            service_state,
            *,
            backend_factory: object,
            process_factory: object,
            external_gateway_required: bool,
        ) -> None:
            events.append("runner_init")
            calls["config"] = config
            calls["service_state"] = service_state
            calls["backend_factory"] = backend_factory
            calls["process_factory"] = process_factory
            calls["external_gateway_required"] = external_gateway_required

        def run(self):
            events.append("runner_run")
            return RuntimeOutcome(RuntimeOutcomeState.STOPPED)

    controller = ProductionWebUIController(
        paths=paths,
        environ={"LOCALAPPDATA": str(local_appdata)},
        platform_system="Windows",
        service_manager=_FakeSupervisor(process_identity(os.getpid())),
        runtime_runner_factory=FakeRunner,
        backend_factory=backend_factory,
    )
    served = controller.dispatch("serve")
    assert served == {"status": "OK", "state": "STOPPED"}
    runtime_config = calls["config"]
    assert isinstance(runtime_config, WebUIRuntimeConfig)
    assert runtime_config.platform_kind is PlatformKind.WINDOWS
    assert runtime_config.backend_transport is BackendTransportKind.WINDOWS_HTTPS
    assert runtime_config.backend_socket is None
    assert runtime_config.caddy_path is None
    assert runtime_config.caddyfile is None
    assert runtime_config.origin == "https://factlane-a2serve123.test:8765"
    assert calls["backend_factory"] is backend_factory
    assert calls["process_factory"] is None
    assert calls["external_gateway_required"] is False
    assert calls["tls_root"] == Path(paths.state_dir) / "tls"
    assert calls["tls_hostname"] == "factlane-a2serve123.test"
    assert events == ["ensure_tls", "runner_init", "runner_run"]
