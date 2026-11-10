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
    WindowsWebUIError,
    WindowsTaskObservation,
    WindowsTaskSchedulerSupervisor,
    current_user_sid,
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
    ):
        validate_owner_private_path(directory, owner_sid=sid, directory=True)
    assert not Path(paths.browser_profile_dir).exists(), (
        "browser-neutral managed setup must not create a dedicated browser profile"
    )


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
    assert receipt.plan is not None
    assert receipt.plan.browser is None
    assert receipt.plan.origin == "http://127.0.0.1:8765"
    kinds = [operation.kind for operation in receipt.plan.operations]
    assert kinds == [
        "ENSURE_WINDOWS_OWNER_STATE",
        "WRITE_SERVICE_CONFIG",
        "REGISTER_WINDOWS_TASK",
        "INSTALL_OWNER_LAUNCHER",
    ]
    serialized = repr(receipt.plan)
    for forbidden in (
        "/etc/hosts",
        "systemd",
        "zenity",
        "ENABLE_USER_LINGERING",
        "WRITE_CADDY_CONFIG",
        "FIREFOX",
        "TLS",
    ):
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
    assert config.origin == "http://127.0.0.1:8765"
    validate_owner_private_path(request.service_config_path, owner_sid=current_user_sid(), directory=False)
    assert not Path(request.browser_profile_dir).exists()
    assert not (Path(request.state_dir) / "tls").exists()
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
        browser=None,
        euid=-1,
    )
    denied = SetupConsent(False, False, False, windows_native_setup=False)
    receipt = plan_webui_setup(request, environment, denied)
    assert receipt.status == "BLOCKED"
    assert receipt.plan is None
    assert any("explicit consent" in reason for reason in receipt.blocked_reasons)


def test_windows_managed_setup_is_browser_neutral_even_when_non_firefox_is_present(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    request = build_default_setup_request(
        environ={
            "LOCALAPPDATA": str(tmp_path / "LocalAppData"),
            "APPDATA": str(tmp_path / "RoamingAppData"),
        },
        installing_user="test-user",
        factlane_executable=executable,
        installation_id_factory=lambda: "browserneutral123",
    )
    environment = InstallEnvironment(
        system="Windows",
        systemd=False,
        caddy_path=None,
        zenity_path=None,
        browser=webui_windows_module.WindowsBrowserInstall(family="chromium", executable=executable),
        euid=-1,
    )

    receipt = plan_webui_setup(
        request,
        environment,
        SetupConsent(False, False, False, windows_native_setup=True),
    )

    assert receipt.status == "READY_TO_APPLY"
    assert receipt.blocked_reasons == ()
    assert receipt.plan is not None
    assert receipt.plan.browser is None
    assert receipt.plan.origin == "http://127.0.0.1:8765"
    serialized = repr(receipt.plan)
    assert "FIREFOX" not in serialized
    assert "BROWSER_PROFILE_TRUST" not in serialized


def test_windows_legacy_https_config_remains_loadable_but_new_plan_canonicalizes_to_http(
    tmp_path: Path,
) -> None:
    environ = {
        "LOCALAPPDATA": str(tmp_path / "LocalAppData"),
        "APPDATA": str(tmp_path / "RoamingAppData"),
    }
    native = windows_owner_paths(environ=environ)
    ensure_windows_owner_paths(native)
    legacy = webui_windows_module.WindowsManagedConfig(
        installation_id="legacy123",
        origin="https://factlane-legacy123.test:8765",
        native_control_pipe=native.native_control_socket,
        runtime_dir=native.runtime_dir,
        state_dir=native.state_dir,
        browser_profile_dir=native.browser_profile_dir,
    )
    write_owner_private_text_atomic(
        native.service_config_path,
        webui_windows_module.render_windows_managed_config(legacy),
    )

    loaded = load_windows_managed_config(native.service_config_path)
    assert loaded == legacy

    request = build_default_setup_request(
        environ=environ,
        installing_user="test-user",
        factlane_executable=_factlane_executable(),
        installation_id_factory=lambda: pytest.fail("legacy config must preserve installation identity"),
    )
    receipt = plan_webui_setup(
        request,
        InstallEnvironment(
            system="Windows",
            systemd=False,
            caddy_path=None,
            zenity_path=None,
            browser=None,
            euid=-1,
        ),
        SetupConsent(False, False, False, windows_native_setup=True),
    )

    assert request.installation_id == "legacy123"
    assert receipt.status == "READY_TO_APPLY"
    assert receipt.plan is not None
    assert receipt.plan.origin == "http://127.0.0.1:8765"


def test_windows_open_presents_exact_loopback_connect_url_without_resolving_or_launching_browser(
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
    origin = "http://127.0.0.1:8765"
    presented: list[str] = []

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

    def browser_resolution_must_not_run(**_kwargs):
        pytest.fail("browser resolution must not run from open")

    monkeypatch.setattr(
        webui_platform_module,
        "_resolve_production_browser_launcher",
        browser_resolution_must_not_run,
    )
    controller = ProductionWebUIController(
        paths=paths,
        platform_system="Windows",
        service_manager=_FakeSupervisor(identity),
        windows_control_client_factory=lambda _path, _process: Client(),
        browser_launcher=lambda _url: pytest.fail("browser launcher must not run from open"),
        open_link_presenter=presented.append,
    )

    result = controller.dispatch("open")

    assert result == {"status": "OK", "state": "OPENED", "origin": origin}
    assert presented == [f"{origin}#connect=local-locator"]


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
    origin = "http://127.0.0.1:8765"
    presented: list[str] = []
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
        browser_launcher=lambda _url: pytest.fail("browser launcher must not run from open"),
        open_link_presenter=presented.append,
        clock=clock,
    )

    result = controller.dispatch("open")

    assert result == {"status": "OK", "state": "OPENED", "origin": origin}
    assert approvals == ["delayed-locator"]
    assert clock.value == 111.0
    assert presented == [f"{origin}#connect=delayed-locator"]


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
    origin = "http://127.0.0.1:8765"
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
        browser_launcher=lambda _url: pytest.fail("browser launcher must not run from open"),
        open_link_presenter=lambda _url: None,
    )

    result = controller.dispatch("open")

    assert result["status"] == "ERROR"
    assert result["error_code"] == "CONTROL_UNAVAILABLE"
    assert approvals == ["timeout-locator"]


def test_windows_serve_route_runs_native_backend_owned_http_without_tls_or_caddy(
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
        browser=None,
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

    monkeypatch.setattr(
        webui_platform_module,
        "ensure_windows_tls_material",
        lambda *_args, **_kwargs: pytest.fail("new WINDOWS_HTTP serve must not provision TLS"),
    )

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
            legacy_endpoint_hostname: str,
        ) -> None:
            events.append("runner_init")
            calls["config"] = config
            calls["service_state"] = service_state
            calls["backend_factory"] = backend_factory
            calls["process_factory"] = process_factory
            calls["external_gateway_required"] = external_gateway_required
            calls["legacy_endpoint_hostname"] = legacy_endpoint_hostname

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
    assert runtime_config.backend_transport is BackendTransportKind.WINDOWS_HTTP
    assert runtime_config.backend_socket is None
    assert runtime_config.caddy_path is None
    assert runtime_config.caddyfile is None
    assert runtime_config.origin == "http://127.0.0.1:8765"
    assert calls["backend_factory"] is backend_factory
    assert calls["process_factory"] is None
    assert calls["external_gateway_required"] is False
    assert calls["legacy_endpoint_hostname"] == "factlane-a2serve123.test"
    assert events == ["runner_init", "runner_run"]
