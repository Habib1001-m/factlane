from __future__ import annotations

import ctypes
import base64
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import threading
from contextlib import contextmanager
import uuid
from dataclasses import dataclass
from ctypes import wintypes
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .windows_security import (
    WindowsProcessIdentity,
    WindowsSecurityError,
    current_user_sid,
    ensure_owner_private_directory,
    owner_only_security_attributes,
    process_identity,
    validate_owner_controlled_kernel_object,
    validate_owner_only_kernel_object,
    validate_owner_private_path,
    validate_windows_path_no_reparse,
)


WINDOWS_MANAGED_CONFIG_VERSION = 1
WINDOWS_TASK_NAME = "FactLane Owner WebUI"
_WINDOWS_MANAGED_PYTHON_BOOTSTRAP = (
    "import os,sys;"
    "_e=os.path.abspath(sys.argv[1]);"
    "_d=os.path.dirname(_e);"
    "_v=(os.path.dirname(_d) if os.path.basename(_d).casefold()=='scripts' "
    "and os.path.isfile(os.path.join(os.path.dirname(_d),'pyvenv.cfg')) else None);"
    "sys.executable=_e;"
    "sys.prefix=_v or sys.prefix;"
    "sys.exec_prefix=_v or sys.exec_prefix;"
    "import site;"
    "site.main();"
    "from factlane.cli import main as _factlane_main;"
    "raise SystemExit(_factlane_main(sys.argv[2:]))"
)
_TASK_TRIGGER_LOGON = 9
_TASK_ACTION_EXEC = 0
_TASK_CREATE_OR_UPDATE = 6
_TASK_LOGON_INTERACTIVE_TOKEN = 3
_TASK_RUNLEVEL_LUA = 0
_TASK_INSTANCES_IGNORE_NEW = 2
_TASK_STATE_DISABLED = 1
_TASK_STATE_QUEUED = 2
_TASK_STATE_READY = 3
_TASK_STATE_RUNNING = 4
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_SAFE_TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}")
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33
_FIREFOX_READINESS_TIMEOUT_SECONDS = 10.0
_FIREFOX_READINESS_POLL_SECONDS = 0.1


class WindowsWebUIError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


_mutation_registry_lock = threading.Lock()
_mutation_locks: dict[str, threading.RLock] = {}
_mutation_owned = threading.local()


@contextmanager
def windows_mutation_guard(*, timeout: float = 10.0):
    """Serialize participating writers; external same-user edits remain detectable drift."""
    if not 0 < timeout <= 60:
        raise ValueError("Windows mutation timeout must be in (0, 60]")
    path = str(PureWindowsPath(windows_owner_paths().state_dir) / "webui-mutation.lock")
    key = str(PureWindowsPath(os.path.realpath(path))).casefold()
    with _mutation_registry_lock:
        lock = _mutation_locks.setdefault(key, threading.RLock())
    deadline = time.monotonic() + timeout
    if not lock.acquire(timeout=timeout):
        raise WindowsWebUIError("WINDOWS_MUTATION_BUSY", "another FactLane mutation is in progress")
    owned = getattr(_mutation_owned, "keys", None)
    if owned is None:
        owned = _mutation_owned.keys = set()
    try:
        if key in owned:
            yield
        else:
            with _native_windows_mutation_lock(path, deadline=deadline):
                owned.add(key)
                try:
                    yield
                finally:
                    owned.remove(key)
    finally:
        lock.release()


@contextmanager
def _native_windows_mutation_lock(path: str, *, deadline: float):
    sid = current_user_sid()
    target = Path(path)
    validate_windows_path_no_reparse(target)
    root = target.parent.parent
    if root.exists():
        validate_owner_private_path(root, owner_sid=sid, directory=True)
    else:
        ensure_owner_private_directory(root, owner_sid=sid)
    if target.parent.exists():
        validate_owner_private_path(target.parent, owner_sid=sid, directory=True)
    else:
        ensure_owner_private_directory(target.parent, owner_sid=sid)
    validate_owner_private_path(target.parent, owner_sid=sid, directory=True)
    _api, con, files = _require_win32file()
    import pywintypes

    parent_guard = _open_windows_parent_guard(target.parent, owner_sid=sid)
    try:
        handle = files.CreateFile(
            path, int(con.GENERIC_READ) | int(con.GENERIC_WRITE),
            int(con.FILE_SHARE_READ) | int(con.FILE_SHARE_WRITE),
            owner_only_security_attributes(sid), int(con.OPEN_ALWAYS),
            int(con.FILE_ATTRIBUTE_NORMAL) | 0x00200000, None,
        )
    except BaseException:
        parent_guard.close()
        raise
    acquired = False
    overlapped = pywintypes.OVERLAPPED()
    try:
        validate_owner_only_kernel_object(handle, sid)
        identity = _windows_handle_identity(handle, expected_path=path, directory=False)
        validate_owner_private_path(path, owner_sid=sid, directory=False)
        while True:
            try:
                files.LockFileEx(handle, 0x00000002 | 0x00000001, 0, 1, overlapped)
                acquired = True
                break
            except pywintypes.error as exc:
                if int(exc.winerror) != _ERROR_LOCK_VIOLATION:
                    raise
                if time.monotonic() >= deadline:
                    raise WindowsWebUIError("WINDOWS_MUTATION_BUSY", "another FactLane mutation is in progress") from exc
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        validate_windows_path_no_reparse(path)
        validate_owner_only_kernel_object(handle, sid)
        if not _same_windows_file(identity, _windows_handle_identity(handle, expected_path=path, directory=False)):
            raise WindowsWebUIError("WINDOWS_MUTATION_UNSAFE", "mutation lock identity changed")
        yield
    finally:
        try:
            if acquired:
                files.UnlockFileEx(handle, 0, 1, overlapped)
        finally:
            handle.Close()
            parent_guard.close()


@dataclass(frozen=True)
class WindowsOwnerPaths:
    root_dir: str
    owner_config_home: str
    service_config_path: str
    runtime_dir: str
    state_dir: str
    browser_profile_dir: str
    native_control_socket: str
    backend_endpoint: str
    task_receipt_path: str


@dataclass(frozen=True)
class WindowsBrowserInstall:
    family: str
    executable: str


@dataclass(frozen=True)
class WindowsTaskPlan:
    task_name: str
    executable: str
    arguments: str
    user_id: str
    logon_type: int = _TASK_LOGON_INTERACTIVE_TOKEN
    run_level: int = _TASK_RUNLEVEL_LUA
    multiple_instances: int = _TASK_INSTANCES_IGNORE_NEW
    execution_time_limit: str = "PT0S"


@dataclass(frozen=True)
class WindowsTaskObservation:
    active: bool
    enabled: bool
    active_state: str
    enabled_state: str
    engine_pid: int | None
    process_identity: WindowsProcessIdentity | None


@dataclass(frozen=True)
class WindowsManagedConfig:
    installation_id: str
    origin: str
    native_control_pipe: str
    runtime_dir: str
    state_dir: str
    browser_profile_dir: str
    db: str | None = None
    profile: str | None = None
    host_id: str | None = None
    version: int = WINDOWS_MANAGED_CONFIG_VERSION

    def __post_init__(self) -> None:
        if self.version != WINDOWS_MANAGED_CONFIG_VERSION:
            raise WindowsWebUIError("WINDOWS_CONFIG_INVALID", "Windows managed WebUI config version is unsupported")
        if not isinstance(self.installation_id, str) or not self.installation_id:
            raise WindowsWebUIError("WINDOWS_CONFIG_INVALID", "installation identity is required")
        _validate_windows_product_path(self.runtime_dir, "runtime_dir")
        _validate_windows_product_path(self.state_dir, "state_dir")
        _validate_windows_product_path(self.browser_profile_dir, "browser_profile_dir")
        if not self.native_control_pipe.startswith("\\\\.\\pipe\\factlane-owner-"):
            raise WindowsWebUIError("WINDOWS_CONFIG_INVALID", "native-control endpoint must be a FactLane local Named Pipe")
        _validate_origin(self.origin)
        configured = (self.db, self.profile, self.host_id)
        if sum(value is not None for value in configured) not in {0, 3}:
            raise WindowsWebUIError("WINDOWS_CONFIG_INVALID", "db, profile, and host_id must be configured together")
        if self.db is not None:
            _validate_windows_product_path(self.db, "db")


def _require_windows() -> tuple[Any, Any, Any, Any]:
    if os.name != "nt":
        raise WindowsWebUIError("WINDOWS_PLATFORM_REQUIRED", "native Windows WebUI support requires Windows")
    try:
        import pywintypes
        import win32api
        import win32con
        import win32security
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise WindowsWebUIError("WINDOWS_PLATFORM_UNAVAILABLE", "pywin32 Windows platform primitives are unavailable") from exc
    return pywintypes, win32api, win32con, win32security


def _validate_windows_product_path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or any(ch in value for ch in ("\n", "\r")):
        raise WindowsWebUIError("WINDOWS_PATH_INVALID", f"{label} is invalid")
    path = PureWindowsPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise WindowsWebUIError("WINDOWS_PATH_INVALID", f"{label} must be a canonical absolute Windows path")
    folded = value.replace("/", "\\").casefold()
    if folded.startswith("\\\\wsl") or folded.startswith("\\\\wsl.localhost"):
        raise WindowsWebUIError("WINDOWS_PATH_INVALID", f"{label} must not use WSL paths")
    if folded.startswith("\\mnt\\") or folded.startswith("/mnt/") or folded.startswith("/home/"):
        raise WindowsWebUIError("WINDOWS_PATH_INVALID", f"{label} must not use Linux paths")
    return str(path)


def windows_managed_python_executables(
    *,
    environment_executable: str | None = None,
    base_executable: str | None = None,
) -> tuple[str, str]:
    """Return the exact base runtime and FactLane environment executables.

    CPython's Windows venv ``python.exe`` is a redirector that spawns the base
    interpreter.  The managed Task Scheduler process cannot use that redirector
    because its EnginePID must be the process that owns FactLane's Named Pipe.
    We therefore run the base interpreter directly and recreate normal venv
    site initialization in-process before importing FactLane.
    """

    _require_windows()
    environment = _validate_windows_product_path(
        sys.executable if environment_executable is None else environment_executable,
        "FactLane environment Python executable",
    )
    base_candidate = getattr(sys, "_base_executable", None) if base_executable is None else base_executable
    if not isinstance(base_candidate, str) or not base_candidate:
        base_candidate = environment
    base = _validate_windows_product_path(base_candidate, "FactLane base Python executable")
    if not Path(environment).is_file() or not Path(base).is_file():
        raise WindowsWebUIError(
            "WINDOWS_FACTLANE_EXECUTABLE_UNAVAILABLE",
            "FactLane Python runtime executables are unavailable",
        )
    if environment.casefold() != base.casefold():
        environment_path = Path(environment)
        environment_root = environment_path.parent.parent
        if (
            environment_path.parent.name.casefold() != "scripts"
            or not (environment_root / "pyvenv.cfg").is_file()
        ):
            raise WindowsWebUIError(
                "WINDOWS_FACTLANE_ENVIRONMENT_INVALID",
                "FactLane environment Python must belong to one native Windows virtual environment",
            )
    return base, environment


def windows_managed_python_arguments(environment_executable: str, action: str) -> str:
    return subprocess.list2cmdline(windows_managed_python_argv(environment_executable, action))


def windows_managed_python_argv(environment_executable: str, action: str) -> tuple[str, ...]:
    if action not in {"serve", "restart"}:
        raise ValueError("managed Windows Python action must be serve or restart")
    environment = _validate_windows_product_path(
        environment_executable,
        "FactLane environment Python executable",
    )
    return (
        "-E",
        "-P",
        "-S",
        "-c",
        _WINDOWS_MANAGED_PYTHON_BOOTSTRAP,
        environment,
        action,
        "webui",
    )


def canonical_windows_database_path(value: str) -> str:
    """Return one exact native local-drive database path or fail closed."""

    if not isinstance(value, str) or not value or len(value) > 4096:
        raise WindowsWebUIError(
            "WINDOWS_DATABASE_PATH_INVALID",
            "database path must be bounded native Windows text",
        )
    canonical = _validate_windows_product_path(value, "db")
    path = PureWindowsPath(canonical)
    reserved = {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
    reserved.update(f"com{index}" for index in range(1, 10))
    reserved.update(f"lpt{index}" for index in range(1, 10))
    unsafe_component = any(
        not component
        or component.endswith((".", " "))
        or any(char in component for char in '<>:"|?*')
        or component.split(".", 1)[0].casefold() in reserved
        for component in path.parts[1:]
    )
    if (
        canonical != value
        or re.fullmatch(r"[A-Za-z]:", path.drive) is None
        or path.root != "\\"
        or any(part == ".." for part in path.parts)
        or unsafe_component
    ):
        raise WindowsWebUIError(
            "WINDOWS_DATABASE_PATH_INVALID",
            "database path must be one canonical absolute native Windows local-drive path",
        )
    return canonical


def validate_windows_database_binding_path(value: str, *, owner_sid: str) -> str:
    """Prove database path custody before it can enter managed configuration."""

    _require_windows()
    canonical = canonical_windows_database_path(value)
    target = validate_windows_path_no_reparse(canonical)
    if target.exists() or target.is_symlink():
        validate_owner_private_path(target, owner_sid=owner_sid, directory=False)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(canonical + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                validate_windows_path_no_reparse(sidecar)
                validate_owner_private_path(sidecar, owner_sid=owner_sid, directory=False)
    else:
        parent = validate_windows_path_no_reparse(target.parent)
        validate_owner_private_path(parent, owner_sid=owner_sid, directory=True)
    return canonical


def _owner_pipe_suffix(owner_sid: str) -> str:
    return hashlib.sha256(owner_sid.encode("ascii", errors="strict")).hexdigest()[:16]


def windows_owner_paths(
    *,
    environ: Mapping[str, str] | None = None,
    owner_sid: str | None = None,
) -> WindowsOwnerPaths:
    _require_windows()
    environment = os.environ if environ is None else environ
    local_appdata = environment.get("LOCALAPPDATA")
    if not isinstance(local_appdata, str) or not local_appdata:
        raise WindowsWebUIError("LOCALAPPDATA_UNAVAILABLE", "LOCALAPPDATA is required for native Windows WebUI state")
    base = PureWindowsPath(_validate_windows_product_path(local_appdata, "LOCALAPPDATA")) / "FactLane"
    config = base / "config"
    runtime = base / "runtime"
    state = base / "state"
    browser = base / "browser"
    sid = current_user_sid() if owner_sid is None else owner_sid
    suffix = _owner_pipe_suffix(sid)
    return WindowsOwnerPaths(
        root_dir=str(base),
        owner_config_home=str(config),
        service_config_path=str(config / "webui.json"),
        runtime_dir=str(runtime),
        state_dir=str(state),
        browser_profile_dir=str(browser),
        native_control_socket=rf"\\.\pipe\factlane-owner-{suffix}",
        backend_endpoint=rf"\\.\pipe\factlane-owner-http-{suffix}",
        task_receipt_path=str(state / "task-scheduler.json"),
    )


def ensure_windows_owner_paths(paths: WindowsOwnerPaths, *, owner_sid: str | None = None) -> None:
    sid = current_user_sid() if owner_sid is None else owner_sid
    for raw in (
        paths.root_dir,
        paths.owner_config_home,
        paths.runtime_dir,
        paths.state_dir,
    ):
        _validate_windows_product_path(raw, "FactLane owner path")
        ensure_owner_private_directory(raw, owner_sid=sid)


def windows_current_user_id() -> str:
    _pywintypes, win32api, win32con, _win32security = _require_windows()
    try:
        return str(win32api.GetUserNameEx(win32con.NameSamCompatible))
    except Exception:
        try:
            return str(win32api.GetUserName())
        except Exception as exc:
            raise WindowsWebUIError("WINDOWS_OWNER_ACCOUNT_UNAVAILABLE", "current Windows account could not be resolved") from exc


def windows_boot_id(*, wmi_factory: Callable[[], Any] | None = None) -> str:
    _require_windows()
    com_uninitialize: Callable[[], Any] | None = None
    if wmi_factory is None:
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise WindowsWebUIError("WINDOWS_BOOT_ID_UNAVAILABLE", "Windows COM support is unavailable") from exc

        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        except pythoncom.com_error as exc:
            hresult = getattr(exc, "hresult", exc.args[0] if exc.args else None)
            rpc_e_changed_mode = getattr(pythoncom, "RPC_E_CHANGED_MODE", -2147417850)
            if hresult != rpc_e_changed_mode:
                raise WindowsWebUIError(
                    "WINDOWS_BOOT_ID_UNAVAILABLE",
                    "Windows COM apartment could not be initialized",
                ) from exc
            # The caller already owns a COM apartment with a different model.
            # WMI works in that apartment; do not balance initialization we did
            # not successfully acquire.
        except Exception as exc:
            raise WindowsWebUIError("WINDOWS_BOOT_ID_UNAVAILABLE", "Windows COM apartment could not be initialized") from exc
        else:
            com_uninitialize = pythoncom.CoUninitialize

        def wmi_factory() -> Any:
            return win32com.client.GetObject(r"winmgmts:\\.\root\cimv2")

    def read_values() -> list[str]:
        # Keep COM proxy objects inside this frame so they are released before
        # the caller balances CoInitialize with CoUninitialize.
        service = wmi_factory()
        records = service.ExecQuery("SELECT LastBootUpTime FROM Win32_OperatingSystem")
        return [str(record.LastBootUpTime) for record in records]

    try:
        values = read_values()
    except Exception as exc:
        raise WindowsWebUIError("WINDOWS_BOOT_ID_UNAVAILABLE", "Windows boot identity could not be read") from exc
    finally:
        if com_uninitialize is not None:
            com_uninitialize()
    if len(values) != 1 or not values[0].strip():
        raise WindowsWebUIError("WINDOWS_BOOT_ID_UNAVAILABLE", "Windows boot identity is ambiguous")
    return f"windows:{values[0].strip()}"


def discover_windows_browser(
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> WindowsBrowserInstall | None:
    _require_windows()
    environment = os.environ if environ is None else environ
    candidates: list[tuple[str, str]] = []
    discovered = which("firefox.exe")
    if discovered:
        candidates.append((discovered, "firefox"))
    program_files = [environment.get("PROGRAMFILES"), environment.get("PROGRAMFILES(X86)"), environment.get("LOCALAPPDATA")]
    for root in program_files:
        if not isinstance(root, str) or not root:
            continue
        base = PureWindowsPath(root)
        candidates.extend(
            [
                (str(base / "Mozilla Firefox" / "firefox.exe"), "firefox"),
            ]
        )
    seen: set[str] = set()
    for raw, family in candidates:
        try:
            canonical = _validate_windows_product_path(raw, "browser executable")
        except WindowsWebUIError:
            continue
        folded = canonical.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        if Path(canonical).is_file():
            return WindowsBrowserInstall(family=family, executable=canonical)
    return None


class WindowsBrowserLauncher:
    def __init__(
        self,
        browser: WindowsBrowserInstall,
        profile_dir: str,
        *,
        owner_sid: str | None = None,
        process_factory: Callable[..., Any] = subprocess.Popen,
        profile_active_probe: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        readiness_timeout: float = _FIREFOX_READINESS_TIMEOUT_SECONDS,
        readiness_poll_interval: float = _FIREFOX_READINESS_POLL_SECONDS,
    ) -> None:
        _require_windows()
        if browser.family != "firefox":
            raise WindowsWebUIError(
                "WINDOWS_BROWSER_UNSUPPORTED",
                "managed Windows WebUI requires Firefox",
            )
        self.browser = browser
        self.profile_dir = _validate_windows_product_path(profile_dir, "browser profile")
        self.owner_sid = current_user_sid() if owner_sid is None else owner_sid
        self._process_factory = process_factory
        self._profile_active_probe = profile_active_probe or self._profile_is_active
        self._clock = clock
        self._sleep = sleep
        if readiness_timeout <= 0 or readiness_poll_interval <= 0:
            raise ValueError("Firefox readiness bounds must be positive")
        self._readiness_timeout = float(readiness_timeout)
        self._readiness_poll_interval = float(readiness_poll_interval)

    def _profile_is_active(self) -> bool:
        profile = Path(self.profile_dir)
        lock_path = profile / "parent.lock"
        try:
            validate_windows_path_no_reparse(profile)
            validate_owner_private_path(profile, owner_sid=self.owner_sid, directory=True)
            validate_windows_path_no_reparse(lock_path)
        except WindowsSecurityError as exc:
            raise WindowsWebUIError(
                "WINDOWS_BROWSER_PROFILE_STATE_UNAVAILABLE",
                "managed Firefox profile state could not be verified safely",
            ) from exc
        pywintypes, _win32api, _win32con, _win32security = _require_windows()
        _file_api, _file_con, win32file = _require_win32file()
        try:
            handle = win32file.CreateFile(
                str(lock_path),
                0x80000000,
                0,
                None,
                3,
                0x00000080 | 0x00200000,
                None,
            )
        except pywintypes.error as exc:
            code = int(getattr(exc, "winerror", 0) or 0)
            if code in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
                return False
            if code in {_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION}:
                return True
            raise WindowsWebUIError(
                "WINDOWS_BROWSER_PROFILE_STATE_UNAVAILABLE",
                "managed Firefox profile lock state is ambiguous",
            ) from exc
        except Exception as exc:
            raise WindowsWebUIError(
                "WINDOWS_BROWSER_PROFILE_STATE_UNAVAILABLE",
                "managed Firefox profile lock state could not be inspected",
            ) from exc
        try:
            return False
        finally:
            handle.Close()

    def _argv(self, target: str) -> tuple[str, ...]:
        return (
            self.browser.executable,
            "-no-remote",
            "-profile",
            self.profile_dir,
            "-new-window",
            target,
        )

    def _spawn(self, target: str) -> Any | None:
        try:
            return self._process_factory(
                self._argv(target),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except Exception:
            return None

    def _warm_process_failed(self, process: Any | None) -> bool:
        if process is None:
            return False
        try:
            return_code = process.poll()
        except Exception:
            return True
        return return_code not in {None, 0}

    def _wait_for_profile_active(self, warm_process: Any, deadline: float) -> bool:
        while True:
            if self._warm_process_failed(warm_process):
                return False
            try:
                if self._profile_active_probe():
                    return True
            except Exception:
                return False
            now = self._clock()
            if now >= deadline:
                return False
            self._sleep(min(self._readiness_poll_interval, deadline - now))

    def _benign_remoting_ready(self, warm_process: Any | None, deadline: float) -> bool:
        if self._warm_process_failed(warm_process):
            return False
        probe = self._spawn("about:blank")
        if probe is None:
            return False
        remaining = deadline - self._clock()
        if remaining <= 0:
            return False
        try:
            return_code = probe.wait(timeout=remaining)
        except Exception:
            return False
        if return_code != 0 or self._warm_process_failed(warm_process):
            return False
        try:
            active = bool(self._profile_active_probe())
        except Exception:
            return False
        if not active or self._clock() >= deadline or self._warm_process_failed(warm_process):
            return False
        return True

    def __call__(self, url: str) -> bool:
        _validate_connect_url(url)
        ensure_owner_private_directory(self.profile_dir, owner_sid=self.owner_sid)
        try:
            active = bool(self._profile_active_probe())
        except Exception:
            return False
        deadline = self._clock() + self._readiness_timeout
        warm_process: Any | None = None
        if not active:
            warm_process = self._spawn("about:blank")
            if warm_process is None or not self._wait_for_profile_active(warm_process, deadline):
                return False
        if not self._benign_remoting_ready(warm_process, deadline):
            return False
        if self._clock() >= deadline:
            return False
        return self._spawn(url) is not None


def windows_native_confirm(message: str) -> bool:
    _pywintypes, win32api, win32con, _win32security = _require_windows()
    if not isinstance(message, str) or not message or len(message) > 4096:
        raise ValueError("confirmation message must be bounded text")
    flags = win32con.MB_YESNO | win32con.MB_ICONQUESTION | win32con.MB_DEFBUTTON2 | win32con.MB_TASKMODAL
    try:
        result = win32api.MessageBox(0, message, "FactLane Owner WebUI setup", flags)
    except Exception:
        return False
    return result == win32con.IDYES


class WindowsSetupConsentProvider:
    def __init__(self, *, confirmer: Callable[[str], bool] = windows_native_confirm) -> None:
        self._confirmer = confirmer

    def acquire(self) -> bool:
        try:
            base, environment = windows_managed_python_executables()
            arguments = windows_managed_python_arguments(environment, "serve")
        except WindowsWebUIError:
            return False
        review = (
            "FactLane will create owner-private files under %LOCALAPPDATA%\\FactLane and "
            "register or update one current-user Task Scheduler entry named 'FactLane Owner WebUI'. "
            f"The task runs only as your interactive account with least privilege using exact Python runtime {base!r} "
            f"and arguments {arguments!r}. No system service, hosts-file change, system trust-store change, or WSL path is used."
        )
        try:
            return bool(self._confirmer(review))
        except Exception:
            return False


class WindowsTaskSchedulerSupervisor:
    def __init__(
        self,
        *,
        task_name: str = WINDOWS_TASK_NAME,
        expected_executable: str,
        environment_executable: str,
        owner_sid: str | None = None,
        user_id: str | None = None,
        service_factory: Callable[[], Any] | None = None,
    ) -> None:
        _require_windows()
        if _SAFE_TASK_NAME.fullmatch(task_name) is None:
            raise ValueError("task_name is invalid")
        self.task_name = task_name
        self.owner_sid = current_user_sid() if owner_sid is None else owner_sid
        self.user_id = windows_current_user_id() if user_id is None else user_id
        if not expected_executable:
            raise WindowsWebUIError(
                "WINDOWS_FACTLANE_EXECUTABLE_UNAVAILABLE",
                "exact FactLane executable is required for scheduled-task validation",
            )
        self.expected_executable = _validate_windows_product_path(expected_executable, "FactLane executable")
        self.environment_executable = _validate_windows_product_path(
            environment_executable,
            "FactLane environment Python executable",
        )
        if not Path(self.expected_executable).is_file() or not Path(self.environment_executable).is_file():
            raise WindowsWebUIError(
                "WINDOWS_FACTLANE_EXECUTABLE_UNAVAILABLE",
                "FactLane managed Python executables are unavailable",
            )
        self._service_factory = service_factory or _default_task_service

    def plan(self, executable: str) -> WindowsTaskPlan:
        return WindowsTaskPlan(
            task_name=self.task_name,
            executable=_validate_windows_product_path(executable, "FactLane executable"),
            arguments=windows_managed_python_arguments(self.environment_executable, "serve"),
            user_id=self.user_id,
        )

    def _task_definition(self, service: Any, plan: WindowsTaskPlan):
        definition = service.NewTask(0)
        definition.RegistrationInfo.Description = "FactLane Owner WebUI current-user managed runtime"
        principal = definition.Principal
        principal.UserId = plan.user_id
        principal.LogonType = plan.logon_type
        principal.RunLevel = plan.run_level
        trigger = definition.Triggers.Create(_TASK_TRIGGER_LOGON)
        trigger.Id = "FactLaneOwnerLogon"
        trigger.UserId = plan.user_id
        trigger.Enabled = True
        action = definition.Actions.Create(_TASK_ACTION_EXEC)
        action.Path = plan.executable
        action.Arguments = plan.arguments
        settings = definition.Settings
        settings.Enabled = True
        settings.AllowDemandStart = True
        settings.StartWhenAvailable = True
        settings.DisallowStartIfOnBatteries = False
        settings.StopIfGoingOnBatteries = False
        settings.RunOnlyIfIdle = False
        settings.Hidden = False
        settings.MultipleInstances = plan.multiple_instances
        settings.ExecutionTimeLimit = plan.execution_time_limit
        return definition

    def prepare_task(self, executable: str, *, transaction_id: str) -> dict[str, Any]:
        plan = self.plan(executable)
        def prepare(service):
            before = self._snapshot_task_with_service(service)
            if before["exists"]:
                task = service.GetFolder("\\").GetTask(self.task_name)
                self._validate_registered_task(task, plan, allow_disabled=True)
                if int(task.State) in {_TASK_STATE_RUNNING, _TASK_STATE_QUEUED}:
                    raise WindowsWebUIError("WINDOWS_SETUP_BUSY", "managed task must be quiescent before setup")
            definition = self._task_definition(service, plan)
            definition.RegistrationInfo.Source = "FactLane setup " + str(uuid.UUID(transaction_id))
            return {"exists": True, "xml": str(definition.XmlText),
                    "sddl": f"O:{self.owner_sid}G:{self.owner_sid}D:P(A;;GA;;;{self.owner_sid})(A;;GA;;;SY)",
                    "enabled": True}
        return self._run_with_service(prepare)

    def publish_task(self, intended: dict[str, Any], *, expected: dict[str, Any]) -> dict[str, Any]:
        def publish(service):
            if self._snapshot_task_with_service(service) != expected:
                raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "scheduled task changed before registration")
            root = service.GetFolder("\\")
            task = root.RegisterTask(self.task_name, intended["xml"], _TASK_CREATE_OR_UPDATE | 0x10,
                                     self.user_id, None, _TASK_LOGON_INTERACTIVE_TOKEN, intended["sddl"])
            self._validate_registered_task(task, self.plan(self.expected_executable))
            self._validate_task_security(task)
            post = self._snapshot_task_with_service(service)
            if post != intended:
                raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "scheduled task postimage differs from prepared registration")
            return post
        return self._run_with_service(publish)

    def register_or_update(self, executable: str) -> WindowsTaskPlan:
        with windows_mutation_guard():
            before = self.snapshot_task()
            intended = self.prepare_task(executable, transaction_id=str(uuid.uuid4()))
            self.publish_task(intended, expected=before)
        self.expected_executable = executable
        return self.plan(executable)

    def _snapshot_task_with_service(self, service: Any) -> dict[str, Any]:
        root = service.GetFolder("\\")
        try:
            task = root.GetTask(self.task_name)
        except Exception as exc:
            code = getattr(exc, "hresult", None)
            if code is None and exc.args:
                code = exc.args[0]
            if isinstance(code, int) and (code & 0xffffffff) == 0x80020009:
                info = getattr(exc, "excepinfo", None)
                if isinstance(info, tuple) and len(info) == 6:
                    code = info[5]
            if isinstance(code, int) and (code & 0xffffffff) == 0x80070002:
                return {"exists": False}
            raise WindowsWebUIError("WINDOWS_TASK_SNAPSHOT_UNAVAILABLE", "scheduled task absence could not be verified") from exc
        self._validate_task_security(task)
        xml = str(task.Xml)
        sddl = str(task.GetSecurityDescriptor(1 | 2 | 4))
        if not xml or len(xml.encode("utf-8")) > 262144 or not sddl or len(sddl) > 32768:
            raise WindowsWebUIError("WINDOWS_TASK_SNAPSHOT_UNAVAILABLE", "scheduled task snapshot is unbounded or incomplete")
        return {"exists": True, "xml": xml, "sddl": sddl, "enabled": bool(task.Enabled)}

    def verify_quiescent(self) -> None:
        def verify(service):
            snapshot = self._snapshot_task_with_service(service)
            if snapshot["exists"]:
                task = service.GetFolder("\\").GetTask(self.task_name)
                if int(task.State) not in {_TASK_STATE_DISABLED, _TASK_STATE_READY}:
                    raise WindowsWebUIError("WINDOWS_SETUP_BUSY", "managed task must be quiescent before recovery")
        self._run_with_service(verify)

    def snapshot_task(self) -> dict[str, Any]:
        """Return bounded plain data; never return a COM proxy outside its apartment."""
        # Pre-consent setup custody must be observational only.  The mutation
        # runner acquires windows_mutation_guard(), which may create FactLane's
        # owner-private state/lock substrate on a first install.  A task
        # snapshot itself needs no mutation serialization, so keep this path
        # strictly read-only and let later setup mutation revalidate the exact
        # snapshot under the mutation guard after Owner consent.
        return self._run_with_service_unlocked(self._snapshot_task_with_service)

    def restore_task(self, original: dict[str, Any], *, expected: dict[str, Any]) -> None:
        """Conditionally restore exact registration state without activating the task."""
        def restore(service: Any) -> None:
            if self._snapshot_task_with_service(service) != expected:
                raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "scheduled task changed outside setup")
            root = service.GetFolder("\\")
            if not original["exists"]:
                if expected["exists"]:
                    root.DeleteTask(self.task_name, 0)
            else:
                task = root.RegisterTask(
                    self.task_name, original["xml"], _TASK_CREATE_OR_UPDATE | 0x10,
                    self.user_id, None, _TASK_LOGON_INTERACTIVE_TOKEN, original["sddl"],
                )
                task.SetSecurityDescriptor(original["sddl"], 0x10)
                task.Enabled = original["enabled"]
            if self._snapshot_task_with_service(service) != original:
                raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "scheduled task exact restoration could not be verified")
        self._run_with_service(restore)

    def observe(self) -> WindowsTaskObservation:
        def observe(service: Any) -> WindowsTaskObservation:
            root = service.GetFolder("\\")
            try:
                task = root.GetTask(self.task_name)
            except Exception as exc:
                raise WindowsWebUIError(
                    "WINDOWS_TASK_UNAVAILABLE",
                    "FactLane current-user scheduled task is not registered",
                ) from exc
            self._validate_registered_task(task, self.plan(self.expected_executable))
            self._validate_task_security(task)
            state = int(task.State)
            state_name = {
                _TASK_STATE_DISABLED: "disabled",
                _TASK_STATE_QUEUED: "queued",
                _TASK_STATE_READY: "ready",
                _TASK_STATE_RUNNING: "running",
            }.get(state, "unknown")
            if state_name == "unknown":
                raise WindowsWebUIError(
                    "WINDOWS_TASK_STATUS_UNAVAILABLE",
                    "scheduled task state is unknown",
                )
            active = state == _TASK_STATE_RUNNING
            engine_pid: int | None = None
            identity: WindowsProcessIdentity | None = None
            if active:
                instances = list(task.GetInstances(0))
                pids = {
                    int(instance.EnginePID)
                    for instance in instances
                    if int(instance.EnginePID) > 0
                }
                if len(pids) != 1:
                    raise WindowsWebUIError(
                        "WINDOWS_TASK_PROCESS_UNAVAILABLE",
                        "running scheduled task process identity is ambiguous",
                    )
                engine_pid = pids.pop()
                try:
                    identity = process_identity(engine_pid)
                except WindowsSecurityError as exc:
                    raise WindowsWebUIError(
                        "WINDOWS_TASK_PROCESS_UNAVAILABLE",
                        exc.safe_message,
                    ) from exc
                if identity.sid.casefold() != self.owner_sid.casefold():
                    raise WindowsWebUIError(
                        "WINDOWS_TASK_PROCESS_DENIED",
                        "scheduled task process belongs to another Windows user",
                    )
            enabled = bool(task.Enabled)
            return WindowsTaskObservation(
                active=active,
                enabled=enabled,
                active_state=state_name,
                enabled_state="enabled" if enabled else "disabled",
                engine_pid=engine_pid,
                process_identity=identity,
            )

        return self._run_with_service(observe)

    def start(self) -> None:
        def start(service: Any) -> None:
            root = service.GetFolder("\\")
            try:
                task = root.GetTask(self.task_name)
                self._validate_registered_task(task, self.plan(self.expected_executable))
                self._validate_task_security(task)
                task.Run("")
            except WindowsWebUIError:
                raise
            except Exception as exc:
                raise WindowsWebUIError(
                    "WINDOWS_TASK_START_FAILED",
                    "FactLane current-user scheduled task failed to start",
                ) from exc

        self._run_with_service(start)

    def delete(self) -> None:
        def delete(service: Any) -> None:
            root = service.GetFolder("\\")
            try:
                root.DeleteTask(self.task_name, 0)
            except Exception:
                return

        self._run_with_service(delete)

    def _run_with_service(self, operation: Callable[[Any], Any]) -> Any:
        with windows_mutation_guard():
            return self._run_with_service_unlocked(operation)

    def _run_with_service_unlocked(self, operation: Callable[[Any], Any]) -> Any:
        com_uninitialize: Callable[[], Any] | None = None
        try:
            import pythoncom
        except ImportError as exc:  # pragma: no cover - native-Windows packaging guard
            raise WindowsWebUIError(
                "WINDOWS_TASK_SCHEDULER_UNAVAILABLE",
                "Task Scheduler 2.0 COM service is unavailable",
            ) from exc
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        except pythoncom.com_error as exc:
            hresult = getattr(exc, "hresult", exc.args[0] if exc.args else None)
            rpc_e_changed_mode = getattr(pythoncom, "RPC_E_CHANGED_MODE", -2147417850)
            if hresult != rpc_e_changed_mode:
                raise WindowsWebUIError(
                    "WINDOWS_TASK_SCHEDULER_UNAVAILABLE",
                    "Task Scheduler 2.0 COM service is unavailable",
                ) from exc
            # The caller already owns a COM apartment with a different model.
            # Task Scheduler is usable there; only uninitialize apartments that
            # this call successfully initialized.
        except Exception as exc:
            raise WindowsWebUIError(
                "WINDOWS_TASK_SCHEDULER_UNAVAILABLE",
                "Task Scheduler 2.0 COM service is unavailable",
            ) from exc
        else:
            com_uninitialize = pythoncom.CoUninitialize

        service: Any | None = None
        result: Any = None
        error_data: tuple[str, str] | None = None
        try:
            try:
                service = self._service_factory()
                service.Connect()
            except Exception:
                error_data = (
                    "WINDOWS_TASK_SCHEDULER_UNAVAILABLE",
                    "Task Scheduler 2.0 COM service is unavailable",
                )
            if error_data is None:
                try:
                    # The callback returns only plain Python values. Convert
                    # every operation failure to plain error data inside this
                    # frame so no exception traceback can retain callback-local
                    # COM proxies across an owned CoUninitialize.
                    result = operation(service)
                except WindowsWebUIError as exc:
                    error_data = (exc.code, exc.safe_message)
                except Exception:
                    error_data = (
                        "WINDOWS_TASK_SCHEDULER_UNAVAILABLE",
                        "Task Scheduler operation failed safely",
                    )
        finally:
            service = None
            if com_uninitialize is not None:
                com_uninitialize()
        if error_data is not None:
            raise WindowsWebUIError(*error_data)
        return result

    def _validate_registered_task(self, task: Any, plan: WindowsTaskPlan, *, allow_disabled: bool = False) -> None:
        try:
            definition = task.Definition
            principal = definition.Principal
            if int(principal.LogonType) != plan.logon_type or int(principal.RunLevel) != plan.run_level:
                raise WindowsWebUIError("WINDOWS_TASK_DEFINITION_UNSAFE", "scheduled task principal is not interactive least-privilege")
            _pywintypes, _win32api, _win32con, win32security = _require_windows()
            principal_sid = win32security.LookupAccountName(None, str(principal.UserId))[0]
            principal_sid_text = str(win32security.ConvertSidToStringSid(principal_sid))
            if principal_sid_text.casefold() != self.owner_sid.casefold():
                raise WindowsWebUIError("WINDOWS_TASK_DEFINITION_UNSAFE", "scheduled task principal is not the current Owner")
            if int(definition.Actions.Count) != 1:
                raise WindowsWebUIError("WINDOWS_TASK_DEFINITION_UNSAFE", "scheduled task must contain one exact action")
            action = definition.Actions.Item(1)
            if int(action.Type) != _TASK_ACTION_EXEC:
                raise WindowsWebUIError("WINDOWS_TASK_DEFINITION_UNSAFE", "scheduled task action type is not executable")
            if str(action.Path).casefold() != plan.executable.casefold() or str(action.Arguments) != plan.arguments:
                raise WindowsWebUIError("WINDOWS_TASK_DEFINITION_UNSAFE", "scheduled task executable or arguments changed")
            triggers = definition.Triggers
            logon_users = [
                str(triggers.Item(index).UserId)
                for index in range(1, int(triggers.Count) + 1)
                if int(triggers.Item(index).Type) == _TASK_TRIGGER_LOGON and bool(triggers.Item(index).Enabled)
            ]
            if len(logon_users) != 1 or logon_users[0].casefold() != plan.user_id.casefold():
                raise WindowsWebUIError("WINDOWS_TASK_DEFINITION_UNSAFE", "scheduled task logon trigger is not exact current-user scope")
            settings = definition.Settings
            if (
                (not allow_disabled and not bool(settings.Enabled))
                or not bool(settings.AllowDemandStart)
                or int(settings.MultipleInstances) != plan.multiple_instances
                or str(settings.ExecutionTimeLimit).upper() != plan.execution_time_limit
            ):
                raise WindowsWebUIError("WINDOWS_TASK_DEFINITION_UNSAFE", "scheduled task settings differ from the fixed lifecycle policy")
        except WindowsWebUIError:
            raise
        except Exception as exc:
            raise WindowsWebUIError("WINDOWS_TASK_DEFINITION_UNSAFE", "scheduled task definition could not be verified") from exc

    def _validate_task_security(self, task: Any) -> None:
        _pywintypes, _win32api, _win32con, win32security = _require_windows()
        try:
            sddl = str(task.GetSecurityDescriptor(_OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION))
            descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                sddl,
                win32security.SDDL_REVISION_1,
            )
            owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
            dacl = descriptor.GetSecurityDescriptorDacl()
        except Exception as exc:
            raise WindowsWebUIError("WINDOWS_TASK_SECURITY_UNAVAILABLE", "scheduled task security descriptor could not be verified") from exc
        if owner.casefold() != self.owner_sid.casefold() or dacl is None:
            raise WindowsWebUIError("WINDOWS_TASK_SECURITY_UNSAFE", "scheduled task is not owned by the current Windows Owner")
        allowed = {
            self.owner_sid.casefold(),
            "s-1-5-18",  # LocalSystem
        }
        for index in range(dacl.GetAceCount()):
            header, _mask, ace_sid = dacl.GetAce(index)
            if int(header[0]) != win32security.ACCESS_ALLOWED_ACE_TYPE:
                continue
            principal = str(win32security.ConvertSidToStringSid(ace_sid)).casefold()
            if principal not in allowed:
                raise WindowsWebUIError("WINDOWS_TASK_SECURITY_UNSAFE", "scheduled task grants control to an unexpected principal")


@dataclass(frozen=True)
class WindowsManagedRestartPlan:
    executable: str
    environment_executable: str
    creationflags: int
    process_factory: Callable[..., Any] = subprocess.Popen

    def handoff(self) -> None:
        try:
            process = self.process_factory(
                (self.executable, *windows_managed_python_argv(self.environment_executable, "restart")),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                creationflags=self.creationflags,
            )
        except Exception as exc:
            raise WindowsWebUIError(
                "WINDOWS_RESTART_HANDOFF_FAILED",
                "managed Windows restart helper could not be started",
            ) from exc
        pid = getattr(process, "pid", None)
        try:
            exited = process.poll()
        except Exception as exc:
            raise WindowsWebUIError(
                "WINDOWS_RESTART_HANDOFF_FAILED",
                "managed Windows restart helper identity could not be verified",
            ) from exc
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or exited is not None:
            raise WindowsWebUIError(
                "WINDOWS_RESTART_HANDOFF_FAILED",
                "managed Windows restart helper did not remain active for handoff",
            )


def prepare_windows_managed_restart(
    *,
    executable: str | None = None,
    process_factory: Callable[..., Any] = subprocess.Popen,
    supervisor_factory: Callable[..., WindowsTaskSchedulerSupervisor] = WindowsTaskSchedulerSupervisor,
) -> WindowsManagedRestartPlan:
    """Prove the live managed task before preparing an isolated module restart handoff."""

    _require_windows()
    base, environment = windows_managed_python_executables(
        environment_executable=executable,
    )
    supervisor = supervisor_factory(
        expected_executable=base,
        environment_executable=environment,
    )
    observation = supervisor.observe()
    if not observation.active or observation.engine_pid != os.getpid():
        raise WindowsWebUIError(
            "WINDOWS_RESTART_UNAVAILABLE",
            "managed restart requires the verified current FactLane scheduled-task process",
        )
    creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
        getattr(subprocess, "DETACHED_PROCESS", 0)
    )
    if creationflags == 0:
        raise WindowsWebUIError(
            "WINDOWS_RESTART_UNAVAILABLE",
            "detached managed restart support is unavailable",
        )
    return WindowsManagedRestartPlan(
        executable=base,
        environment_executable=environment,
        creationflags=creationflags,
        process_factory=process_factory,
    )


def _default_task_service() -> Any:
    try:
        import win32com.client
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise WindowsWebUIError("WINDOWS_TASK_SCHEDULER_UNAVAILABLE", "Task Scheduler COM support is unavailable") from exc
    return win32com.client.Dispatch("Schedule.Service")


@dataclass(frozen=True)
class _WindowsFileIdentity:
    volume_serial_number: int
    file_index: int
    final_path: str


@dataclass
class _WindowsParentGuard:
    path: str
    handle: Any
    identity: _WindowsFileIdentity
    private_parent: bool = True

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            handle.Close()


@dataclass
class _WindowsTempFile:
    path: Path
    handle: Any
    identity: _WindowsFileIdentity
    published: bool = False

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            handle.Close()


class _FILE_RENAME_INFORMATION_HEADER(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
    ]


class _IO_STATUS_BLOCK_STATUS(ctypes.Union):
    _fields_ = [
        ("Status", wintypes.LONG),
        ("Pointer", wintypes.LPVOID),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _anonymous_ = ("result",)
    _fields_ = [
        ("result", _IO_STATUS_BLOCK_STATUS),
        ("Information", ctypes.c_size_t),
    ]


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOL)]


def _require_win32file() -> tuple[Any, Any, Any]:
    if os.name != "nt":
        raise WindowsWebUIError("WINDOWS_PLATFORM_REQUIRED", "native Windows WebUI support requires Windows")
    try:
        import win32api
        import win32con
        import win32file
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise WindowsWebUIError("WINDOWS_PLATFORM_UNAVAILABLE", "pywin32 file primitives are unavailable") from exc
    return win32api, win32con, win32file


def _win32_handle_value(handle: Any) -> wintypes.HANDLE:
    try:
        value = int(handle)
    except Exception as exc:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows handle identity is unavailable",
        ) from exc
    if value <= 0:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows handle identity is unavailable",
        )
    return wintypes.HANDLE(value)


def _strip_extended_windows_prefix(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _windows_path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(_strip_extended_windows_prefix(path)))


def _windows_handle_identity(handle: Any, *, expected_path: str, directory: bool) -> _WindowsFileIdentity:
    _win32api, _win32con, win32file = _require_win32file()
    try:
        info = win32file.GetFileInformationByHandle(handle)
        attributes = int(info[0])
        volume_serial = int(info[4])
        file_index = (int(info[8]) << 32) | int(info[9])
        final_path = _strip_extended_windows_prefix(str(win32file.GetFinalPathNameByHandle(handle, 0)))
    except Exception as exc:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows file identity could not be verified",
        ) from exc
    is_directory = bool(attributes & 0x10)
    if bool(attributes & 0x400) or is_directory != directory:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows path resolved to an unsafe file type",
        )
    if _windows_path_key(final_path) != _windows_path_key(expected_path):
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows path identity changed",
        )
    return _WindowsFileIdentity(volume_serial, file_index, final_path)


def _same_windows_file(left: _WindowsFileIdentity, right: _WindowsFileIdentity) -> bool:
    return (
        left.volume_serial_number == right.volume_serial_number
        and left.file_index == right.file_index
        and _windows_path_key(left.final_path) == _windows_path_key(right.final_path)
    )


def _same_windows_object(left: _WindowsFileIdentity, right: _WindowsFileIdentity) -> bool:
    return (
        left.volume_serial_number == right.volume_serial_number
        and left.file_index == right.file_index
    )


def _open_windows_parent_guard(parent: Path, *, owner_sid: str, private_parent: bool = True) -> _WindowsParentGuard:
    _win32api, win32con, win32file = _require_win32file()
    parent_text = str(parent)
    try:
        validate_windows_path_no_reparse(parent)
        if private_parent:
            validate_owner_private_path(parent, owner_sid=owner_sid, directory=True)
        handle = win32file.CreateFile(
            parent_text,
            0,
            int(win32con.FILE_SHARE_READ) | int(win32con.FILE_SHARE_WRITE),
            None,
            int(win32con.OPEN_EXISTING),
            0x00200000 | 0x02000000,
            None,
        )
    except (OSError, WindowsSecurityError, WindowsWebUIError) as exc:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows parent directory could not be guarded safely",
        ) from exc
    except Exception as exc:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows parent directory could not be guarded safely",
        ) from exc
    try:
        identity = _windows_handle_identity(handle, expected_path=parent_text, directory=True)
        return _WindowsParentGuard(parent_text, handle, identity, private_parent)
    except BaseException:
        handle.Close()
        raise


def _revalidate_windows_parent_guard(guard: _WindowsParentGuard, *, owner_sid: str) -> None:
    if guard.handle is None:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows parent guard is closed",
        )
    try:
        validate_windows_path_no_reparse(guard.path)
        if guard.private_parent:
            validate_owner_private_path(guard.path, owner_sid=owner_sid, directory=True)
        retained = _windows_handle_identity(guard.handle, expected_path=guard.path, directory=True)
    except (OSError, WindowsSecurityError, WindowsWebUIError) as exc:
        if isinstance(exc, WindowsWebUIError):
            raise
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows parent directory changed during publication",
        ) from exc
    if not _same_windows_file(retained, guard.identity):
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows parent directory changed during publication",
        )


def _create_owner_private_temp(
    guard: _WindowsParentGuard,
    target: Path,
    content: str | bytes,
    *,
    owner_sid: str,
) -> _WindowsTempFile:
    _win32api, win32con, win32file = _require_win32file()
    _revalidate_windows_parent_guard(guard, owner_sid=owner_sid)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    handle = None
    try:
        validate_windows_path_no_reparse(temporary)
        handle = win32file.CreateFile(
            str(temporary),
            int(win32con.GENERIC_READ) | int(win32con.GENERIC_WRITE) | 0x00010000 | 0x00020000 | 0x00040000 | 0x00080000,
            0,
            owner_only_security_attributes(owner_sid),
            int(win32con.CREATE_NEW),
            int(win32con.FILE_ATTRIBUTE_NORMAL) | 0x00200000,
            None,
        )
        validate_owner_only_kernel_object(handle, owner_sid)
        identity = _windows_handle_identity(handle, expected_path=str(temporary), directory=False)
        encoded = content.encode("utf-8") if isinstance(content, str) else content
        error_code, written = win32file.WriteFile(handle, encoded)
        if int(error_code) != 0 or int(written) != len(encoded):
            raise WindowsWebUIError(
                "WINDOWS_OWNER_PRIVATE_WRITE_FAILED",
                "owner-private Windows file could not be written completely",
            )
        win32file.FlushFileBuffers(handle)
        validate_owner_only_kernel_object(handle, owner_sid)
        _revalidate_windows_parent_guard(guard, owner_sid=owner_sid)
        retained = _windows_handle_identity(handle, expected_path=str(temporary), directory=False)
        if not _same_windows_file(retained, identity):
            raise WindowsWebUIError(
                "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
                "owner-private Windows temporary file identity changed after write",
            )
        result = _WindowsTempFile(temporary, handle, identity)
        handle = None
        return result
    except WindowsWebUIError:
        raise
    except WindowsSecurityError as exc:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows temporary path became unsafe",
        ) from exc
    except (OSError, ValueError) as exc:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_FAILED",
            "owner-private Windows temporary file could not be created safely",
        ) from exc
    except Exception as exc:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_FAILED",
            "owner-private Windows temporary file could not be created safely",
        ) from exc
    finally:
        if handle is not None:
            handle.Close()


def _rename_windows_handle_replace(source_handle: Any, parent_handle: Any, target_leaf: str) -> None:
    if not target_leaf or "\\" in target_leaf or "/" in target_leaf:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows destination leaf is invalid",
        )
    encoded = target_leaf.encode("utf-16-le")
    filename_offset = _FILE_RENAME_INFORMATION_HEADER.FileNameLength.offset + ctypes.sizeof(wintypes.DWORD)
    buffer = ctypes.create_string_buffer(filename_offset + len(encoded) + ctypes.sizeof(wintypes.WCHAR))
    header = _FILE_RENAME_INFORMATION_HEADER.from_buffer(buffer)
    header.ReplaceIfExists = 1
    header.RootDirectory = _win32_handle_value(parent_handle)
    header.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + filename_offset, encoded, len(encoded))

    io_status = _IO_STATUS_BLOCK()
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    set_info = ntdll.NtSetInformationFile
    set_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IO_STATUS_BLOCK),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    set_info.restype = wintypes.LONG
    status = int(set_info(
        _win32_handle_value(source_handle),
        ctypes.byref(io_status),
        ctypes.byref(buffer),
        filename_offset + len(encoded),
        10,
    ))
    if status >= 0:
        return
    rtl_status_to_dos = ntdll.RtlNtStatusToDosError
    rtl_status_to_dos.argtypes = [wintypes.LONG]
    rtl_status_to_dos.restype = wintypes.ULONG
    error = int(rtl_status_to_dos(status))
    if error in {32, 33}:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows source file changed during publication",
        )
    raise WindowsWebUIError(
        "WINDOWS_OWNER_PRIVATE_WRITE_FAILED",
        "owner-private Windows file could not be published by retained handle",
    )


def _publish_owner_private_temp(
    guard: _WindowsParentGuard,
    temporary: _WindowsTempFile,
    target: Path,
    *,
    owner_sid: str,
    owner_private: bool = True,
) -> None:
    _require_win32file()
    _revalidate_windows_parent_guard(guard, owner_sid=owner_sid)
    if temporary.handle is None:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows temporary file handle is closed",
        )
    try:
        validate_owner_only_kernel_object(temporary.handle, owner_sid)
        source = _windows_handle_identity(
            temporary.handle,
            expected_path=str(temporary.path),
            directory=False,
        )
        if not _same_windows_file(source, temporary.identity):
            raise WindowsWebUIError(
                "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
                "owner-private Windows temporary file identity changed before publication",
            )
        if target.exists() or target.is_symlink():
            validate_windows_path_no_reparse(target)
            if owner_private:
                validate_owner_private_path(target, owner_sid=owner_sid, directory=False)
        _revalidate_windows_parent_guard(guard, owner_sid=owner_sid)
        _rename_windows_handle_replace(temporary.handle, guard.handle, target.name)
        published = _windows_handle_identity(
            temporary.handle,
            expected_path=str(target),
            directory=False,
        )
        validate_owner_only_kernel_object(temporary.handle, owner_sid)
        if not _same_windows_object(published, temporary.identity):
            raise WindowsWebUIError(
                "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
                "published owner-private Windows file identity changed",
            )
        _revalidate_windows_parent_guard(guard, owner_sid=owner_sid)
        if owner_private:
            validate_owner_private_path(target, owner_sid=owner_sid, directory=False)
        temporary.published = True
    except WindowsWebUIError:
        raise
    except WindowsSecurityError as exc:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows publication path became unsafe",
        ) from exc
    except OSError as exc:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_FAILED",
            "owner-private Windows file could not be published safely",
        ) from exc
    except Exception as exc:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_FAILED",
            "owner-private Windows file could not be published safely",
        ) from exc
def _cleanup_owner_private_temp(
    guard: _WindowsParentGuard,
    temporary: _WindowsTempFile | None,
    *,
    owner_sid: str,
) -> None:
    if temporary is None or temporary.published:
        return
    try:
        _revalidate_windows_parent_guard(guard, owner_sid=owner_sid)
        validate_owner_only_kernel_object(temporary.handle, owner_sid)
        source = _windows_handle_identity(
            temporary.handle,
            expected_path=str(temporary.path),
            directory=False,
        )
        if not _same_windows_file(source, temporary.identity):
            return
        disposition = _FILE_DISPOSITION_INFO(DeleteFile=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        set_info = kernel32.SetFileInformationByHandle
        set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        set_info.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        set_info(
            _win32_handle_value(temporary.handle),
            4,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        )
    except Exception:
        return


WINDOWS_SETUP_FILE_LIMIT = 262144


def windows_file_custody_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # Content equality alone never proves transaction custody.
    return left == right


def _snapshot_windows_file_handle(
    handle: Any,
    path: str,
    *,
    owner_sid: str,
    limit: int = WINDOWS_SETUP_FILE_LIMIT,
    owner_private: bool = True,
) -> dict[str, Any]:
    _py, _api, _con, security = _require_windows()
    _api, _con, files = _require_win32file()
    if owner_private:
        validate_owner_only_kernel_object(handle, owner_sid)
    else:
        validate_owner_controlled_kernel_object(handle, owner_sid)
    identity = _windows_handle_identity(handle, expected_path=path, directory=False)
    size = int(files.GetFileSize(handle))
    if size > limit:
        raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "setup file exceeds custody bound")
    files.SetFilePointer(handle, 0, 0)
    data = b"" if size == 0 else bytes(files.ReadFile(handle, size)[1])
    if len(data) != size:
        raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "setup file read was incomplete")
    descriptor = security.GetSecurityInfo(handle, security.SE_FILE_OBJECT, 1 | 2 | 4)
    sddl = security.ConvertSecurityDescriptorToStringSecurityDescriptor(descriptor, 1, 1 | 2 | 4)
    return {"exists": True, "identity": [identity.volume_serial_number, identity.file_index],
            "content": base64.b64encode(data).decode("ascii"), "sddl": str(sddl)}


def snapshot_windows_setup_file(
    path: str,
    *,
    owner_sid: str,
    limit: int = WINDOWS_SETUP_FILE_LIMIT,
    owner_private: bool = True,
) -> dict[str, Any]:
    validate_windows_path_no_reparse(path)
    _api, con, files = _require_win32file()
    import pywintypes
    try:
        handle = files.CreateFile(path, int(con.GENERIC_READ) | 0x00020000,
                                  int(con.FILE_SHARE_READ) | int(con.FILE_SHARE_WRITE), None,
                                  int(con.OPEN_EXISTING), 0x00200000, None)
    except pywintypes.error as exc:
        if int(exc.winerror) in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
            return {"exists": False}
        raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "setup file custody unavailable") from exc
    try:
        return _snapshot_windows_file_handle(
            handle,
            path,
            owner_sid=owner_sid,
            limit=limit,
            owner_private=owner_private,
        )
    finally:
        handle.Close()


def stage_windows_setup_file(path: str, content: bytes, *, owner_sid: str,
                             private_parent: bool, sddl: str | None = None,
                             owner_private: bool = True):
    """Create a unique secured postimage and retain its handle until publication."""
    guard = _open_windows_parent_guard(Path(path).parent, owner_sid=owner_sid, private_parent=private_parent)
    temporary = None
    try:
        temporary = _create_owner_private_temp(guard, Path(path), content, owner_sid=owner_sid)
        if sddl is not None:
            _py, _api, _con, security = _require_windows()
            descriptor = security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1)
            control = descriptor.GetSecurityDescriptorControl()[0]
            protection = 0x80000000 if control & 0x1000 else 0x20000000
            security.SetSecurityInfo(temporary.handle, security.SE_FILE_OBJECT, 1 | 2 | 4 | protection,
                                     descriptor.GetSecurityDescriptorOwner(), descriptor.GetSecurityDescriptorGroup(),
                                     descriptor.GetSecurityDescriptorDacl(), None)
        post = _snapshot_windows_file_handle(
            temporary.handle,
            str(temporary.path),
            owner_sid=owner_sid,
            owner_private=owner_private,
        )
        return guard, temporary, post
    except BaseException:
        _cleanup_owner_private_temp(guard, temporary, owner_sid=owner_sid)
        if temporary is not None:
            temporary.close()
        guard.close()
        raise


def publish_windows_setup_file(
    path: str,
    staged,
    *,
    expected: dict[str, Any],
    owner_sid: str,
    owner_private: bool = True,
) -> None:
    guard, temporary, post = staged
    if not windows_file_custody_matches(
        snapshot_windows_setup_file(path, owner_sid=owner_sid, owner_private=owner_private),
        expected,
    ):
        raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "setup file changed before publication")
    _publish_owner_private_temp(
        guard,
        temporary,
        Path(path),
        owner_sid=owner_sid,
        owner_private=owner_private,
    )
    if not windows_file_custody_matches(
        _snapshot_windows_file_handle(
            temporary.handle,
            path,
            owner_sid=owner_sid,
            owner_private=owner_private,
        ),
        post,
    ):
        raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "setup publication postimage changed")


def close_windows_setup_stage(staged, *, owner_sid: str) -> None:
    guard, temporary, _post = staged
    try:
        _cleanup_owner_private_temp(guard, temporary, owner_sid=owner_sid)
    finally:
        temporary.close()
        guard.close()


def delete_windows_setup_file(
    path: str,
    *,
    expected: dict[str, Any],
    owner_sid: str,
    owner_private: bool = True,
) -> None:
    _api, con, files = _require_win32file()
    validate_windows_path_no_reparse(path)
    handle = files.CreateFile(path, int(con.GENERIC_READ) | 0x00020000 | 0x00010000,
                              int(con.FILE_SHARE_READ), None, int(con.OPEN_EXISTING), 0x00200000, None)
    try:
        if _snapshot_windows_file_handle(
            handle,
            path,
            owner_sid=owner_sid,
            owner_private=owner_private,
        ) != expected:
            raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "setup file changed before deletion")
        disposition = _FILE_DISPOSITION_INFO(DeleteFile=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        operation = kernel32.SetFileInformationByHandle
        operation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        operation.restype = wintypes.BOOL
        if not operation(_win32_handle_value(handle), 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        handle.Close()


def write_owner_private_text_atomic(path: str, content: str, *, owner_sid: str | None = None,
                                  expected_state=..., snapshot_limit: int = WINDOWS_SETUP_FILE_LIMIT) -> None:
    _require_windows()
    target_text = _validate_windows_product_path(path, "owner-private file")
    target = Path(target_text)
    sid = current_user_sid() if owner_sid is None else owner_sid
    temporary: _WindowsTempFile | None = None
    guard: _WindowsParentGuard | None = None
    try:
        validate_windows_path_no_reparse(target)
        ensure_owner_private_directory(target.parent, owner_sid=sid)
        validate_windows_path_no_reparse(target)
        guard = _open_windows_parent_guard(target.parent, owner_sid=sid)
        if target.exists() or target.is_symlink():
            validate_owner_private_path(target, owner_sid=sid, directory=False)
        temporary = _create_owner_private_temp(guard, target, content, owner_sid=sid)
        if expected_state is not ... and snapshot_windows_setup_file(str(target), owner_sid=sid, limit=snapshot_limit) != expected_state:
            raise WindowsWebUIError("WINDOWS_SETUP_RECOVERY_REQUIRED", "owner-private file changed before publication")
        _publish_owner_private_temp(guard, temporary, target, owner_sid=sid)
    except WindowsWebUIError:
        raise
    except (OSError, WindowsSecurityError) as exc:
        raise WindowsWebUIError(
            "WINDOWS_OWNER_PRIVATE_WRITE_UNSAFE",
            "owner-private Windows file could not be written safely",
        ) from exc
    finally:
        if guard is not None:
            _cleanup_owner_private_temp(guard, temporary, owner_sid=sid)
            guard.close()
        if temporary is not None:
            temporary.close()


def load_windows_managed_config(path: str, *, owner_sid: str | None = None) -> WindowsManagedConfig | None:
    _require_windows()
    target_text = _validate_windows_product_path(path, "managed config")
    target = Path(target_text)
    if not target.exists():
        return None
    sid = current_user_sid() if owner_sid is None else owner_sid
    try:
        validate_owner_private_path(target, owner_sid=sid, directory=False)
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, WindowsSecurityError) as exc:
        raise WindowsWebUIError("WINDOWS_CONFIG_INVALID", "Windows managed WebUI config could not be read safely") from exc
    if not isinstance(raw, dict):
        raise WindowsWebUIError("WINDOWS_CONFIG_INVALID", "Windows managed WebUI config must be one JSON object")
    allowed = set(WindowsManagedConfig.__dataclass_fields__)
    if set(raw) != allowed:
        raise WindowsWebUIError("WINDOWS_CONFIG_INVALID", "Windows managed WebUI config has unexpected fields")
    try:
        return WindowsManagedConfig(**raw)
    except (TypeError, ValueError, WindowsWebUIError) as exc:
        raise WindowsWebUIError("WINDOWS_CONFIG_INVALID", "Windows managed WebUI config content is invalid") from exc


def render_windows_managed_config(config: WindowsManagedConfig) -> str:
    return json.dumps(config.__dict__, sort_keys=True, separators=(",", ":")) + "\n"


def _validate_origin(origin: str) -> None:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError as exc:
        raise WindowsWebUIError("WINDOWS_CONFIG_INVALID", "managed endpoint is invalid") from exc
    local_http = (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None
        and 8765 <= port <= 8774
        and origin == f"http://127.0.0.1:{port}"
    )
    legacy_https = (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.startswith("factlane-")
        and parsed.hostname.endswith(".test")
        and port is not None
        and 8765 <= port <= 8774
        and origin == f"https://{parsed.hostname}:{port}"
    )
    if (
        parsed.hostname is None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or not (local_http or legacy_https)
    ):
        raise WindowsWebUIError(
            "WINDOWS_CONFIG_INVALID",
            "managed endpoint must be canonical FactLane loopback HTTP or legacy HTTPS",
        )


def _validate_connect_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    except (ValueError, TypeError) as exc:
        raise WindowsWebUIError("WINDOWS_BROWSER_URL_INVALID", "browser URL is invalid") from exc
    _validate_origin(origin)
    if parsed.query or parsed.username is not None or parsed.password is not None:
        raise WindowsWebUIError("WINDOWS_BROWSER_URL_INVALID", "browser URL contains forbidden authority or query material")
    if parsed.path:
        raise WindowsWebUIError("WINDOWS_BROWSER_URL_INVALID", "browser URL must not contain a path")
    if parsed.fragment and not parsed.fragment.startswith("connect="):
        raise WindowsWebUIError("WINDOWS_BROWSER_URL_INVALID", "browser URL fragment is unsupported")
