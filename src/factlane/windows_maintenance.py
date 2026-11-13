from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable

from .windows_security import (
    WindowsSecurityError,
    current_user_sid,
    ensure_owner_private_directory,
    protect_owner_private_path,
    validate_owner_only_kernel_object,
    validate_owner_private_path,
)


_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OVERLAPPED = 0x40000000
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_ID_INFO_CLASS = 18
_FILE_DISPOSITION_INFO_CLASS = 4
_FILE_RENAME_INFO_EX_CLASS = 22
_FILE_RENAME_FLAG_REPLACE_IF_EXISTS = 0x00000001
_FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x00000002
_OBJ_CASE_INSENSITIVE = 0x00000040
_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_OPEN_IF = 3
_FILE_NON_DIRECTORY_FILE = 0x00000040
_SDDL_REVISION_1 = 1
_FSCTL_REQUEST_OPLOCK = 0x00090240
_REQUEST_OPLOCK_CURRENT_VERSION = 1
_REQUEST_OPLOCK_INPUT_FLAG_REQUEST = 0x00000001
_REQUEST_OPLOCK_INPUT_FLAG_ACK = 0x00000002
_REQUEST_OPLOCK_OUTPUT_FLAG_ACK_REQUIRED = 0x00000001
_REQUEST_OPLOCK_OUTPUT_FLAG_MODES_PROVIDED = 0x00000002
_OPLOCK_LEVEL_CACHE_READ = 0x00000001
_OPLOCK_LEVEL_CACHE_HANDLE = 0x00000002
_OPLOCK_LEVEL_CACHE_WRITE = 0x00000004
_OPLOCK_LEVEL_BATCH_EQUIVALENT = _OPLOCK_LEVEL_CACHE_READ | _OPLOCK_LEVEL_CACHE_HANDLE | _OPLOCK_LEVEL_CACHE_WRITE
_OPLOCK_LEVEL_EXPECTED_RENAME_BREAK = _OPLOCK_LEVEL_CACHE_READ | _OPLOCK_LEVEL_CACHE_WRITE
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33
_ERROR_NOT_SUPPORTED = 50
_ERROR_INVALID_PARAMETER = 87
_ERROR_CALL_NOT_IMPLEMENTED = 120
_ERROR_IO_PENDING = 997
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_INFINITE = 0xFFFFFFFF
_SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE = 1006
_SQLITE_OK = 0

_RETAINED_EXCLUSION_HANDLES: list[Any] = []
_RETAINED_EXCLUSION_LOCK = threading.Lock()
_MIGRATION_RAW_COPY_FAULT_HOOK: Any = None
_MIGRATION_RECOVERY_FAULT_HOOK: Any = None


def _migration_raw_copy_checkpoint(bytes_written: int, total_size: int) -> None:
    hook = _MIGRATION_RAW_COPY_FAULT_HOOK
    if callable(hook):
        hook(bytes_written, total_size)


def _migration_recovery_checkpoint(stage: str) -> None:
    hook = _MIGRATION_RECOVERY_FAULT_HOOK
    if callable(hook):
        hook(stage)


def _set_sqlite_no_checkpoint_on_close(connection: sqlite3.Connection, *, expected_path: str) -> None:
    setconfig = getattr(connection, "setconfig", None)
    config_id = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", _SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE)
    if callable(setconfig):
        try:
            setconfig(config_id, True)
        except Exception as exc:
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_GUARD_UNAVAILABLE",
                "SQLite no-checkpoint-on-close guard could not be enabled",
            ) from exc
        return

    if (
        sys.implementation.name != "cpython"
        or ctypes.sizeof(ctypes.c_void_p) != 8
        or ctypes.sizeof(ctypes.c_ssize_t) != 8
    ):
        raise WindowsMaintenanceError(
            "MIGRATION_RECOVERY_GUARD_UNAVAILABLE",
            "SQLite no-checkpoint-on-close guard is unavailable on this Python runtime",
        )
    sqlite_dll = Path(sys.base_prefix) / "DLLs" / "sqlite3.dll"
    if not sqlite_dll.is_file():
        raise WindowsMaintenanceError(
            "MIGRATION_RECOVERY_GUARD_UNAVAILABLE",
            "SQLite runtime DLL is unavailable for recovery close protection",
        )
    try:
        dll = ctypes.WinDLL(str(sqlite_dll))
        db_offset = ctypes.sizeof(ctypes.c_ssize_t) + ctypes.sizeof(ctypes.c_void_p)
        db_pointer = ctypes.c_void_p.from_address(id(connection) + db_offset).value
        if not db_pointer:
            raise ValueError("missing sqlite3 connection pointer")

        db_filename = dll.sqlite3_db_filename
        db_filename.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        db_filename.restype = ctypes.c_char_p
        raw_filename = db_filename(ctypes.c_void_p(db_pointer), b"main")
        if raw_filename is None:
            raise ValueError("missing sqlite3 main database filename")
        actual_path = raw_filename.decode("utf-8")
        if _path_key(actual_path) != _path_key(expected_path):
            raise ValueError("sqlite3 connection pointer does not match guarded database")

        db_config = dll.sqlite3_db_config
        db_config.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        db_config.restype = ctypes.c_int
        previous = ctypes.c_int()
        result = int(
            db_config(
                ctypes.c_void_p(db_pointer),
                int(config_id),
                1,
                ctypes.byref(previous),
            )
        )
        if result != _SQLITE_OK:
            raise ValueError(f"sqlite3_db_config failed with code {result}")
    except WindowsMaintenanceError:
        raise
    except Exception as exc:
        raise WindowsMaintenanceError(
            "MIGRATION_RECOVERY_GUARD_UNAVAILABLE",
            "SQLite recovery close protection could not be established safely",
        ) from exc


class WindowsMaintenanceError(RuntimeError):
    """Fail-closed native-Windows maintenance primitive failure."""

    def __init__(self, code: str, safe_message: str, *, durability: WindowsPromotionDurability | None = None) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.durability = durability


@dataclass(frozen=True, slots=True)
class WindowsFileIdentity:
    volume_serial_number: int
    file_id: bytes
    final_path: str

    @property
    def file_id_hex(self) -> str:
        return self.file_id.hex()


@dataclass(frozen=True, slots=True)
class WindowsPromotionDurability:
    pre_rename_file_flushed: bool
    post_rename_file_flushed: bool
    directory_durability_proven: bool
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class WindowsPromotionResult:
    old_target_identity: WindowsFileIdentity
    new_target_identity: WindowsFileIdentity
    durability: WindowsPromotionDurability


@dataclass(frozen=True, slots=True)
class _WindowsApi:
    pywintypes: Any
    win32con: Any
    win32file: Any
    kernel32: Any


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("ReparseTag", wintypes.DWORD),
    ]


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", _FILE_ID_128),
    ]


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOL)]


class _FILE_RENAME_INFO_EX_HEADER(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID),
        ("SecurityQualityOfService", wintypes.LPVOID),
    ]


class _IO_STATUS_BLOCK_STATUS(ctypes.Union):
    _fields_ = [
        ("Status", wintypes.LONG),
        ("Pointer", wintypes.LPVOID),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [
        ("u", _IO_STATUS_BLOCK_STATUS),
        ("Information", ctypes.c_size_t),
    ]


class _OVERLAPPED_OFFSET(ctypes.Structure):
    _fields_ = [
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
    ]


class _OVERLAPPED_UNION(ctypes.Union):
    _fields_ = [
        ("offset", _OVERLAPPED_OFFSET),
        ("Pointer", wintypes.LPVOID),
    ]


class _OVERLAPPED(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("u", _OVERLAPPED_UNION),
        ("hEvent", wintypes.HANDLE),
    ]


class _REQUEST_OPLOCK_INPUT_BUFFER(ctypes.Structure):
    _fields_ = [
        ("StructureVersion", wintypes.WORD),
        ("StructureLength", wintypes.WORD),
        ("RequestedOplockLevel", wintypes.DWORD),
        ("Flags", wintypes.DWORD),
    ]


class _REQUEST_OPLOCK_OUTPUT_BUFFER(ctypes.Structure):
    _fields_ = [
        ("StructureVersion", wintypes.WORD),
        ("StructureLength", wintypes.WORD),
        ("OriginalOplockLevel", wintypes.DWORD),
        ("NewOplockLevel", wintypes.DWORD),
        ("Flags", wintypes.DWORD),
        ("AccessMode", wintypes.DWORD),
        ("ShareMode", wintypes.WORD),
    ]


@dataclass(frozen=True, slots=True)
class _OplockBreak:
    original_level: int
    new_level: int
    flags: int
    access_mode: int
    share_mode: int

    @property
    def acknowledgment_required(self) -> bool:
        return bool(self.flags & _REQUEST_OPLOCK_OUTPUT_FLAG_ACK_REQUIRED)

    @property
    def modes_provided(self) -> bool:
        return bool(self.flags & _REQUEST_OPLOCK_OUTPUT_FLAG_MODES_PROVIDED)


class _NativeHandle:
    def __init__(self, value: int) -> None:
        self._value = value

    def __int__(self) -> int:
        return self._value

    def Close(self) -> None:
        if self._value == 0:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        value, self._value = self._value, 0
        close(wintypes.HANDLE(value))


class _WindowsBatchOplock:
    """Exclusive RWH oplock, the Windows 7 equivalent of a legacy batch oplock."""

    def __init__(
        self,
        file_handle: Any,
        event_handle: wintypes.HANDLE,
        overlapped: _OVERLAPPED,
        output: _REQUEST_OPLOCK_OUTPUT_BUFFER,
    ) -> None:
        self._file_handle = file_handle
        self._event_handle = event_handle
        self._overlapped = overlapped
        self._output = output
        self._break: _OplockBreak | None = None
        self._closed = False

    @classmethod
    def acquire(cls, file_handle: Any) -> _WindowsBatchOplock:
        _require_windows()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_event = kernel32.CreateEventW
        create_event.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        create_event.restype = wintypes.HANDLE
        event_handle = create_event(None, True, False, None)
        if not event_handle:
            raise WindowsMaintenanceError(
                "MAINTENANCE_HOLD_OPLOCK_UNAVAILABLE",
                "exclusive database oplock event could not be created",
            )

        request = _REQUEST_OPLOCK_INPUT_BUFFER(
            StructureVersion=_REQUEST_OPLOCK_CURRENT_VERSION,
            StructureLength=ctypes.sizeof(_REQUEST_OPLOCK_INPUT_BUFFER),
            RequestedOplockLevel=_OPLOCK_LEVEL_BATCH_EQUIVALENT,
            Flags=_REQUEST_OPLOCK_INPUT_FLAG_REQUEST,
        )
        output = _REQUEST_OPLOCK_OUTPUT_BUFFER()
        overlapped = _OVERLAPPED()
        overlapped.hEvent = event_handle
        bytes_returned = wintypes.DWORD()
        device_io = kernel32.DeviceIoControl
        device_io.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_OVERLAPPED),
        ]
        device_io.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        granted = bool(
            device_io(
                _handle_value(file_handle),
                _FSCTL_REQUEST_OPLOCK,
                ctypes.byref(request),
                ctypes.sizeof(request),
                ctypes.byref(output),
                ctypes.sizeof(output),
                ctypes.byref(bytes_returned),
                ctypes.byref(overlapped),
            )
        )
        error = ctypes.get_last_error()
        if granted or error != _ERROR_IO_PENDING:
            close = kernel32.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            close(event_handle)
            raise WindowsMaintenanceError(
                "MAINTENANCE_HOLD_CONTENTION",
                "database could not obtain exclusive batch-oplock custody",
            )
        return cls(file_handle, event_handle, overlapped, output)

    def is_broken(self) -> bool:
        self._require_open()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wait = kernel32.WaitForSingleObject
        wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait.restype = wintypes.DWORD
        result = int(wait(self._event_handle, 0))
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        raise WindowsMaintenanceError(
            "MAINTENANCE_HOLD_OPLOCK_UNAVAILABLE",
            "database oplock state could not be observed",
        )

    def require_unbroken(self) -> None:
        if self.is_broken():
            raise WindowsMaintenanceError(
                "MAINTENANCE_HOLD_CONTENTION",
                "database batch oplock was broken by another operation",
            )

    def wait_for_break(self, timeout_ms: int) -> bool:
        self._require_open()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wait = kernel32.WaitForSingleObject
        wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait.restype = wintypes.DWORD
        result = int(wait(self._event_handle, timeout_ms))
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        raise WindowsMaintenanceError(
            "MAINTENANCE_HOLD_OPLOCK_UNAVAILABLE",
            "database oplock break could not be observed",
        )

    def consume_break(self) -> _OplockBreak:
        self._require_open()
        if self._break is not None:
            return self._break
        if not self.is_broken():
            raise WindowsMaintenanceError(
                "MAINTENANCE_HOLD_OPLOCK_UNAVAILABLE",
                "database oplock break was not available for inspection",
            )
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_result = kernel32.GetOverlappedResult
        get_result.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_OVERLAPPED),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        ]
        get_result.restype = wintypes.BOOL
        transferred = wintypes.DWORD()
        if not get_result(
            _handle_value(self._file_handle),
            ctypes.byref(self._overlapped),
            ctypes.byref(transferred),
            False,
        ):
            raise WindowsMaintenanceError(
                "MAINTENANCE_HOLD_OPLOCK_UNAVAILABLE",
                "database oplock break details could not be read",
            )
        self._break = _OplockBreak(
            original_level=int(self._output.OriginalOplockLevel),
            new_level=int(self._output.NewOplockLevel),
            flags=int(self._output.Flags),
            access_mode=int(self._output.AccessMode),
            share_mode=int(self._output.ShareMode),
        )
        return self._break

    def acknowledge_to_none(self) -> None:
        break_info = self.consume_break()
        if not break_info.acknowledgment_required:
            raise WindowsMaintenanceError(
                "MAINTENANCE_HOLD_OPLOCK_UNSAFE",
                "database oplock break did not preserve acknowledgment custody",
            )
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        request = _REQUEST_OPLOCK_INPUT_BUFFER(
            StructureVersion=_REQUEST_OPLOCK_CURRENT_VERSION,
            StructureLength=ctypes.sizeof(_REQUEST_OPLOCK_INPUT_BUFFER),
            RequestedOplockLevel=0,
            Flags=_REQUEST_OPLOCK_INPUT_FLAG_ACK,
        )
        output = _REQUEST_OPLOCK_OUTPUT_BUFFER()
        bytes_returned = wintypes.DWORD()
        ack_event = kernel32.CreateEventW(None, True, False, None)
        if not ack_event:
            raise WindowsMaintenanceError(
                "MAINTENANCE_HOLD_OPLOCK_UNAVAILABLE",
                "database oplock acknowledgment event could not be created",
            )
        ack_overlapped = _OVERLAPPED()
        ack_overlapped.hEvent = ack_event
        device_io = kernel32.DeviceIoControl
        device_io.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_OVERLAPPED),
        ]
        device_io.restype = wintypes.BOOL
        try:
            ctypes.set_last_error(0)
            completed = bool(
                device_io(
                    _handle_value(self._file_handle),
                    _FSCTL_REQUEST_OPLOCK,
                    ctypes.byref(request),
                    ctypes.sizeof(request),
                    ctypes.byref(output),
                    ctypes.sizeof(output),
                    ctypes.byref(bytes_returned),
                    ctypes.byref(ack_overlapped),
                )
            )
            if completed:
                return
            error = ctypes.get_last_error()
            if error != _ERROR_IO_PENDING:
                raise WindowsMaintenanceError(
                    "MAINTENANCE_HOLD_OPLOCK_UNAVAILABLE",
                    "database oplock break could not be acknowledged",
                )
            wait = kernel32.WaitForSingleObject
            wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            wait.restype = wintypes.DWORD
            if int(wait(ack_event, 5000)) != _WAIT_OBJECT_0:
                raise WindowsMaintenanceError(
                    "MAINTENANCE_HOLD_OPLOCK_UNAVAILABLE",
                    "database oplock acknowledgment did not complete",
                )
            get_result = kernel32.GetOverlappedResult
            get_result.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_OVERLAPPED),
                ctypes.POINTER(wintypes.DWORD),
                wintypes.BOOL,
            ]
            get_result.restype = wintypes.BOOL
            if not get_result(
                _handle_value(self._file_handle),
                ctypes.byref(ack_overlapped),
                ctypes.byref(bytes_returned),
                False,
            ):
                raise WindowsMaintenanceError(
                    "MAINTENANCE_HOLD_OPLOCK_UNAVAILABLE",
                    "database oplock acknowledgment result could not be proven",
                )
        finally:
            close = kernel32.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            close(ack_event)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        event_handle, self._event_handle = self._event_handle, wintypes.HANDLE()
        if event_handle:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            close = kernel32.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            close(event_handle)

    def _require_open(self) -> None:
        if self._closed:
            raise WindowsMaintenanceError(
                "MAINTENANCE_HOLD_OPLOCK_UNAVAILABLE",
                "database oplock state is already closed",
            )


def _require_windows() -> _WindowsApi:
    if os.name != "nt":
        raise WindowsMaintenanceError(
            "WINDOWS_MAINTENANCE_UNAVAILABLE",
            "native Windows maintenance primitives are unavailable on this platform",
        )
    try:
        import pywintypes
        import win32con
        import win32file
    except ImportError as exc:  # pragma: no cover - native packaging guard
        raise WindowsMaintenanceError(
            "WINDOWS_MAINTENANCE_UNAVAILABLE",
            "pywin32 maintenance primitives are unavailable",
        ) from exc
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        for name in (
            "GetFileAttributesW",
            "GetFileInformationByHandleEx",
            "GetFinalPathNameByHandleW",
            "SetFileInformationByHandle",
        ):
            if not hasattr(kernel32, name):
                raise AttributeError(name)
    except Exception as exc:  # pragma: no cover - native API guard
        raise WindowsMaintenanceError(
            "WINDOWS_MAINTENANCE_UNAVAILABLE",
            "required Win32 maintenance APIs are unavailable",
        ) from exc
    return _WindowsApi(pywintypes, win32con, win32file, kernel32)


def _handle_value(handle: Any) -> wintypes.HANDLE:
    try:
        value = int(handle)
    except Exception as exc:
        raise WindowsMaintenanceError("WINDOWS_HANDLE_INVALID", "Windows file handle is invalid") from exc
    if value in {0, -1}:
        raise WindowsMaintenanceError("WINDOWS_HANDLE_INVALID", "Windows file handle is invalid")
    return wintypes.HANDLE(value)


def _winerror(exc: BaseException) -> int | None:
    value = getattr(exc, "winerror", None)
    if isinstance(value, int):
        return value
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return int(args[0])
    return None


def _close_handle(handle: Any | None) -> None:
    if handle is None:
        return
    close = getattr(handle, "Close", None)
    if callable(close):
        close()
        return
    close = getattr(handle, "close", None)
    if callable(close):
        close()


def _canonical_native_path(value: str | os.PathLike[str], *, label: str) -> str:
    _require_windows()
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise WindowsMaintenanceError("WINDOWS_PATH_INVALID", f"{label} path is invalid") from exc
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise WindowsMaintenanceError("WINDOWS_PATH_INVALID", f"{label} path is invalid")
    path = PureWindowsPath(raw)
    drive = path.drive
    if (
        not path.is_absolute()
        or len(drive) != 2
        or drive[1] != ":"
        or not drive[0].isalpha()
        or raw.startswith(("\\\\", "\\?\\", "\\.\\"))
        or ".." in path.parts
    ):
        raise WindowsMaintenanceError("WINDOWS_PATH_INVALID", f"{label} must be one absolute local drive path")
    for part in path.parts[1:]:
        if ":" in part or part.endswith((" ", ".")):
            raise WindowsMaintenanceError("WINDOWS_PATH_INVALID", f"{label} path contains an unsafe component")
    full = os.path.abspath(os.path.normpath(raw))
    normalized = PureWindowsPath(full)
    if not normalized.is_absolute() or len(normalized.drive) != 2:
        raise WindowsMaintenanceError("WINDOWS_PATH_INVALID", f"{label} could not be canonicalized")
    return str(normalized)


def _path_key(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def _strip_extended_prefix(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _get_file_attributes(api: _WindowsApi, path: str) -> int | None:
    fn = api.kernel32.GetFileAttributesW
    fn.argtypes = [wintypes.LPCWSTR]
    fn.restype = wintypes.DWORD
    ctypes.set_last_error(0)
    attributes = int(fn(path))
    if attributes != _INVALID_FILE_ATTRIBUTES:
        return attributes
    error = ctypes.get_last_error()
    if error in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
        return None
    raise WindowsMaintenanceError("WINDOWS_PATH_UNAVAILABLE", "Windows path attributes could not be verified")


def _reject_reparse_components(path: str, *, label: str, include_leaf: bool = True) -> None:
    api = _require_windows()
    parts = PureWindowsPath(path).parts
    current = parts[0]
    limit = len(parts) if include_leaf else max(1, len(parts) - 1)
    for part in parts[1:limit]:
        current = str(PureWindowsPath(current) / part)
        attributes = _get_file_attributes(api, current)
        if attributes is None:
            continue
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise WindowsMaintenanceError(
                "WINDOWS_REPARSE_DENIED",
                f"{label} path contains a reparse point",
            )


def _handle_identity(
    handle: Any,
    *,
    expected_path: str | None = None,
    expect_directory: bool = False,
) -> WindowsFileIdentity:
    api = _require_windows()
    raw_handle = _handle_value(handle)

    tag = _FILE_ATTRIBUTE_TAG_INFO()
    get_info = api.kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    if not get_info(raw_handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(tag), ctypes.sizeof(tag)):
        raise WindowsMaintenanceError("WINDOWS_IDENTITY_UNAVAILABLE", "Windows file attributes could not be read from the retained handle")
    if int(tag.FileAttributes) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise WindowsMaintenanceError("WINDOWS_REPARSE_DENIED", "Windows file handle refers to a reparse point")
    is_directory = bool(int(tag.FileAttributes) & _FILE_ATTRIBUTE_DIRECTORY)
    if expect_directory and not is_directory:
        raise WindowsMaintenanceError("WINDOWS_PATH_INVALID", "Windows parent handle does not refer to a directory")
    if not expect_directory and is_directory:
        raise WindowsMaintenanceError("WINDOWS_PATH_INVALID", "Windows maintenance file path refers to a directory")

    file_id = _FILE_ID_INFO()
    ctypes.set_last_error(0)
    if not get_info(raw_handle, _FILE_ID_INFO_CLASS, ctypes.byref(file_id), ctypes.sizeof(file_id)):
        raise WindowsMaintenanceError("WINDOWS_IDENTITY_UNAVAILABLE", "Windows file identity could not be read from the retained handle")

    get_final = api.kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final.restype = wintypes.DWORD
    ctypes.set_last_error(0)
    required = int(get_final(raw_handle, None, 0, 0))
    if required <= 0 or required > 32768:
        raise WindowsMaintenanceError("WINDOWS_IDENTITY_UNAVAILABLE", "Windows final file path could not be determined")
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = int(get_final(raw_handle, buffer, len(buffer), 0))
    if written <= 0 or written >= len(buffer):
        raise WindowsMaintenanceError("WINDOWS_IDENTITY_UNAVAILABLE", "Windows final file path could not be read")
    final_path = _strip_extended_prefix(buffer.value)
    if expected_path is not None and _path_key(final_path) != _path_key(expected_path):
        raise WindowsMaintenanceError("WINDOWS_PATH_IDENTITY_MISMATCH", "Windows path resolved to a different retained file")
    return WindowsFileIdentity(
        volume_serial_number=int(file_id.VolumeSerialNumber),
        file_id=bytes(file_id.FileId.Identifier),
        final_path=final_path,
    )


def _open_file(
    path: str,
    *,
    desired_access: int,
    share_mode: int,
    creation_disposition: int,
    security_attributes: Any = None,
    contention_code: str,
    contention_message: str,
    flags_and_attributes: int = _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
) -> Any:
    api = _require_windows()
    try:
        return api.win32file.CreateFile(
            path,
            desired_access,
            share_mode,
            security_attributes,
            creation_disposition,
            flags_and_attributes,
            None,
        )
    except Exception as exc:
        if _winerror(exc) in {_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION}:
            raise WindowsMaintenanceError(contention_code, contention_message) from exc
        raise WindowsMaintenanceError("WINDOWS_FILE_OPEN_FAILED", "Windows maintenance file handle could not be opened safely") from exc


def _open_relative_open_always(
    parent_handle: Any,
    leaf_path: str,
    *,
    desired_access: int,
    share_mode: int,
    owner_sid: str | None,
    contention_code: str,
    contention_message: str,
    nt_create_disposition: int = _FILE_OPEN_IF,
) -> Any:
    _require_windows()
    leaf_name = PureWindowsPath(leaf_path).name
    if not leaf_name or "\\" in leaf_name or "/" in leaf_name:
        raise WindowsMaintenanceError("WINDOWS_PATH_INVALID", "Windows maintenance leaf path is invalid")

    name_buffer = ctypes.create_unicode_buffer(leaf_name)
    name = _UNICODE_STRING(
        Length=len(leaf_name.encode("utf-16-le")),
        MaximumLength=ctypes.sizeof(name_buffer),
        Buffer=ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    descriptor = wintypes.LPVOID()
    if owner_sid is not None:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
        convert.restype = wintypes.BOOL
        if not convert(
            f"O:{owner_sid}D:P(A;;GA;;;{owner_sid})",
            _SDDL_REVISION_1,
            ctypes.byref(descriptor),
            None,
        ):
            raise WindowsMaintenanceError(
                "WINDOWS_MAINTENANCE_UNAVAILABLE",
                "owner-only Windows barrier security descriptor is unavailable",
            )

    attributes = _OBJECT_ATTRIBUTES(
        Length=ctypes.sizeof(_OBJECT_ATTRIBUTES),
        RootDirectory=_handle_value(parent_handle),
        ObjectName=ctypes.pointer(name),
        Attributes=_OBJ_CASE_INSENSITIVE,
        SecurityDescriptor=descriptor,
        SecurityQualityOfService=None,
    )
    io_status = _IO_STATUS_BLOCK()
    raw_handle = wintypes.HANDLE()
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    nt_create = ntdll.NtCreateFile
    nt_create.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_OBJECT_ATTRIBUTES),
        ctypes.POINTER(_IO_STATUS_BLOCK),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    ]
    nt_create.restype = wintypes.LONG
    try:
        status = int(
            nt_create(
                ctypes.byref(raw_handle),
                desired_access,
                ctypes.byref(attributes),
                ctypes.byref(io_status),
                None,
                _FILE_ATTRIBUTE_NORMAL,
                share_mode,
                nt_create_disposition,
                _FILE_NON_DIRECTORY_FILE | _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
                0,
            )
        )
    finally:
        if descriptor:
            local_free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
            local_free.argtypes = [wintypes.HLOCAL]
            local_free.restype = wintypes.HLOCAL
            local_free(descriptor)

    if status < 0:
        rtl_status_to_dos = ntdll.RtlNtStatusToDosError
        rtl_status_to_dos.argtypes = [wintypes.LONG]
        rtl_status_to_dos.restype = wintypes.ULONG
        error = int(rtl_status_to_dos(status))
        if error in {_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION}:
            raise WindowsMaintenanceError(contention_code, contention_message)
        raise WindowsMaintenanceError("WINDOWS_FILE_OPEN_FAILED", "Windows maintenance file handle could not be opened safely")
    value = int(raw_handle.value or 0)
    if value == 0:
        raise WindowsMaintenanceError("WINDOWS_HANDLE_INVALID", "Windows file handle is invalid")
    return _NativeHandle(value)


def _open_parent_guard(
    leaf_path: str,
    *,
    label: str,
    contention_code: str,
    contention_message: str,
) -> tuple[str, Any, WindowsFileIdentity]:
    parent = str(PureWindowsPath(leaf_path).parent)
    _reject_reparse_components(parent, label=f"{label} parent")
    handle = _open_file(
        parent,
        desired_access=0,
        share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
        creation_disposition=_OPEN_EXISTING,
        contention_code=contention_code,
        contention_message=contention_message,
        flags_and_attributes=_FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
    )
    try:
        identity = _handle_identity(handle, expected_path=parent, expect_directory=True)
        return parent, handle, identity
    except BaseException:
        _close_handle(handle)
        raise


def _require_parent_guard(
    parent: str,
    handle: Any,
    expected: WindowsFileIdentity,
    *,
    code: str,
    message: str,
) -> None:
    try:
        current = _handle_identity(handle, expect_directory=True)
    except WindowsMaintenanceError as exc:
        raise WindowsMaintenanceError(code, message) from exc
    if not _same_file(current, expected) or _path_key(current.final_path) != _path_key(parent):
        raise WindowsMaintenanceError(code, message)


def _probe_identity(path: str) -> WindowsFileIdentity:
    handle = _open_file(
        path,
        desired_access=0,
        share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        creation_disposition=_OPEN_EXISTING,
        contention_code="WINDOWS_IDENTITY_UNAVAILABLE",
        contention_message="Windows path identity could not be re-opened for verification",
    )
    try:
        return _handle_identity(handle, expected_path=path)
    finally:
        _close_handle(handle)


def _same_file(left: WindowsFileIdentity, right: WindowsFileIdentity) -> bool:
    return (
        left.volume_serial_number == right.volume_serial_number
        and left.file_id == right.file_id
        and _path_key(left.final_path) == _path_key(right.final_path)
    )


def _current_owner_sid(requested: str | None) -> str:
    try:
        actual = current_user_sid()
    except WindowsSecurityError as exc:
        raise WindowsMaintenanceError(
            "WINDOWS_MAINTENANCE_UNAVAILABLE",
            "current-user Windows SID could not be verified for maintenance custody",
        ) from exc
    if requested is not None and (
        not isinstance(requested, str)
        or not requested.startswith("S-")
        or requested.casefold() != actual.casefold()
    ):
        raise WindowsMaintenanceError(
            "BARRIER_OWNER_MISMATCH",
            "maintenance barrier owner must be the current Windows user",
        )
    return actual


def validate_windows_runtime_database_family(
    db_path: str | os.PathLike[str],
    *,
    owner_sid: str,
) -> str:
    """Validate strict Owner custody for a Windows runtime DB family without mutation."""

    canonical = _canonical_native_path(db_path, label="database")
    _reject_reparse_components(canonical, label="database")
    sid = _current_owner_sid(owner_sid)
    parent = str(PureWindowsPath(canonical).parent)
    try:
        validate_owner_private_path(parent, owner_sid=sid, directory=True)
    except WindowsSecurityError as exc:
        raise WindowsMaintenanceError(
            "RUNTIME_DATABASE_PATH_UNSAFE",
            "database parent is not protected owner-private state",
        ) from exc

    api = _require_windows()
    main_attributes = _get_file_attributes(api, canonical)
    sidecars = tuple(canonical + suffix for suffix in ("-wal", "-shm"))
    if main_attributes is None:
        if any(_get_file_attributes(api, path) is not None for path in sidecars):
            raise WindowsMaintenanceError(
                "RUNTIME_DATABASE_PATH_UNSAFE",
                "database sidecar exists without the main database",
            )
        return canonical
    if main_attributes & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
        raise WindowsMaintenanceError(
            "RUNTIME_DATABASE_PATH_UNSAFE",
            "database file path is unsafe",
        )
    try:
        validate_owner_private_path(canonical, owner_sid=sid, directory=False)
    except WindowsSecurityError as exc:
        raise WindowsMaintenanceError(
            "RUNTIME_DATABASE_PATH_UNSAFE",
            "database file is not protected owner-private state",
        ) from exc

    for sidecar_path in sidecars:
        attributes = _get_file_attributes(api, sidecar_path)
        if attributes is None:
            continue
        if attributes & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
            raise WindowsMaintenanceError(
                "RUNTIME_DATABASE_PATH_UNSAFE",
                "database sidecar path is unsafe",
            )
        try:
            validate_owner_private_path(sidecar_path, owner_sid=sid, directory=False)
        except WindowsSecurityError as exc:
            raise WindowsMaintenanceError(
                "RUNTIME_DATABASE_PATH_UNSAFE",
                "database sidecar is not protected owner-private state",
            ) from exc
    return canonical


def _require_identity(path: str, expected: WindowsFileIdentity, *, code: str, message: str) -> WindowsFileIdentity:
    current = _probe_identity(path)
    if not _same_file(current, expected):
        raise WindowsMaintenanceError(code, message)
    return current


def _scan_sidecars(db_path: str) -> tuple[str, ...]:
    api = _require_windows()
    result: list[str] = []
    for suffix in ("-wal", "-shm"):
        path = db_path + suffix
        attributes = _get_file_attributes(api, path)
        if attributes is None:
            continue
        if attributes & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
            raise WindowsMaintenanceError(
                "MAINTENANCE_HOLD_SIDECAR_UNCERTAIN",
                "database sidecar identity is unsafe for exclusive maintenance",
            )
        _reject_reparse_components(path, label="database sidecar")
        result.append(path)
    return tuple(result)


def _acquire_sidecar_custody(
    db_path: str,
    *,
    expected_identities: dict[str, WindowsFileIdentity] | None = None,
    writable: bool = False,
) -> dict[str, tuple[Any, WindowsFileIdentity]]:
    paths = _scan_sidecars(db_path)
    if expected_identities is not None and tuple(expected_identities) != paths:
        raise WindowsMaintenanceError(
            "MIGRATION_HOLD_SIDECAR_UNCERTAIN",
            "database WAL/SHM sidecar set changed while staged migration custody was held",
        )
    retained: dict[str, tuple[Any, WindowsFileIdentity]] = {}
    try:
        for sidecar_path in paths:
            handle = _open_file(
                sidecar_path,
                desired_access=_GENERIC_READ | (_GENERIC_WRITE | _DELETE if writable else 0),
                share_mode=0,
                creation_disposition=_OPEN_EXISTING,
                contention_code="MIGRATION_HOLD_CONTENTION",
                contention_message="database WAL/SHM sidecar is still held by another process",
            )
            try:
                identity = _handle_identity(handle, expected_path=sidecar_path)
                _require_identity(
                    sidecar_path,
                    identity,
                    code="MIGRATION_HOLD_IDENTITY_DRIFT",
                    message="database sidecar path changed during staged migration custody",
                )
                if expected_identities is not None and not _same_file(identity, expected_identities[sidecar_path]):
                    raise WindowsMaintenanceError(
                        "MIGRATION_HOLD_IDENTITY_DRIFT",
                        "database sidecar identity changed while staged migration custody was held",
                    )
            except BaseException:
                _close_handle(handle)
                raise
            retained[sidecar_path] = (handle, identity)

        if _scan_sidecars(db_path) != paths:
            raise WindowsMaintenanceError(
                "MIGRATION_HOLD_SIDECAR_UNCERTAIN",
                "database WAL/SHM sidecar set was unstable during staged migration custody",
            )
        return retained
    except BaseException:
        for handle, _identity in retained.values():
            _close_handle(handle)
        raise


def _coordination_state_directory() -> tuple[str, str]:
    _require_windows()
    raw = os.environ.get("LOCALAPPDATA")
    if not raw:
        raise WindowsMaintenanceError(
            "WINDOWS_COORDINATION_UNAVAILABLE",
            "LOCALAPPDATA is required for native Windows database coordination",
        )
    base = _canonical_native_path(raw, label="LOCALAPPDATA")
    _reject_reparse_components(base, label="LOCALAPPDATA")
    api = _require_windows()
    attributes = _get_file_attributes(api, base)
    if attributes is None or not (attributes & _FILE_ATTRIBUTE_DIRECTORY):
        raise WindowsMaintenanceError(
            "WINDOWS_COORDINATION_UNAVAILABLE",
            "LOCALAPPDATA is unavailable for native Windows database coordination",
        )
    sid = _current_owner_sid(None)
    current = PureWindowsPath(base)
    for component in ("FactLane", "runtime", "database-coordination"):
        current = current / component
        try:
            ensure_owner_private_directory(str(current), owner_sid=sid)
            validate_owner_private_path(str(current), owner_sid=sid, directory=True)
        except WindowsSecurityError as exc:
            raise WindowsMaintenanceError(
                "WINDOWS_COORDINATION_PATH_UNSAFE",
                "database coordination state is not protected owner-private Windows state",
            ) from exc
    return str(current), sid


def _coordination_sentinel_path(db_path: str) -> tuple[str, str]:
    state_dir, sid = _coordination_state_directory()
    digest = hashlib.sha256(_path_key(db_path).encode("utf-8")).hexdigest()
    return str(PureWindowsPath(state_dir) / f"db-{digest}.lease"), sid


def windows_migration_rollback_intent_path(db_path: str | os.PathLike[str]) -> str:
    canonical = _canonical_native_path(db_path, label="database")
    state_dir, _sid = _coordination_state_directory()
    digest = hashlib.sha256(_path_key(canonical).encode("utf-8")).hexdigest()
    return str(PureWindowsPath(state_dir) / f"db-{digest}.rollback-intent.json")


class _WindowsDBCoordinationSentinel:
    def __init__(self, path: str, handle: Any, identity: WindowsFileIdentity, owner_sid: str, *, exclusive: bool) -> None:
        self.path = path
        self.identity = identity
        self.owner_sid = owner_sid
        self.exclusive = exclusive
        self._handle = handle
        self._closed = False

    @classmethod
    def acquire(cls, db_path: str, *, exclusive: bool) -> _WindowsDBCoordinationSentinel:
        sentinel_path, sid = _coordination_sentinel_path(db_path)
        parent, parent_handle, parent_identity = _open_parent_guard(
            sentinel_path,
            label="database coordination sentinel",
            contention_code="WINDOWS_COORDINATION_PATH_UNSAFE",
            contention_message="database coordination parent cannot be guarded safely",
        )
        try:
            try:
                validate_owner_private_path(parent, owner_sid=sid, directory=True)
            except WindowsSecurityError as exc:
                raise WindowsMaintenanceError(
                    "WINDOWS_COORDINATION_PATH_UNSAFE",
                    "database coordination parent is not protected owner-private state",
                ) from exc

            api = _require_windows()
            existing = _get_file_attributes(api, sentinel_path)
            if existing is not None:
                if existing & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
                    raise WindowsMaintenanceError(
                        "WINDOWS_COORDINATION_PATH_UNSAFE",
                        "database coordination sentinel path is unsafe",
                    )
                try:
                    validate_owner_private_path(sentinel_path, owner_sid=sid, directory=False)
                except WindowsSecurityError as exc:
                    raise WindowsMaintenanceError(
                        "WINDOWS_COORDINATION_PATH_UNSAFE",
                        "database coordination sentinel is not protected owner-private state",
                    ) from exc
                create_owner_sid = None
            else:
                create_owner_sid = sid

            handle = _open_relative_open_always(
                parent_handle,
                sentinel_path,
                desired_access=_GENERIC_READ | (_GENERIC_WRITE if exclusive else 0),
                share_mode=0 if exclusive else _FILE_SHARE_READ,
                owner_sid=create_owner_sid,
                contention_code="MAINTENANCE_IN_PROGRESS",
                contention_message="database coordination sentinel is held by an incompatible runtime or maintenance process",
            )
            try:
                identity = _handle_identity(handle)
                _require_parent_guard(
                    parent,
                    parent_handle,
                    parent_identity,
                    code="WINDOWS_COORDINATION_IDENTITY_DRIFT",
                    message="database coordination parent changed during sentinel acquisition",
                )
                if _path_key(identity.final_path) != _path_key(sentinel_path):
                    raise WindowsMaintenanceError(
                        "WINDOWS_COORDINATION_IDENTITY_DRIFT",
                        "database coordination sentinel path changed during acquisition",
                    )
                try:
                    validate_owner_private_path(sentinel_path, owner_sid=sid, directory=False)
                except WindowsSecurityError as exc:
                    raise WindowsMaintenanceError(
                        "WINDOWS_COORDINATION_PATH_UNSAFE",
                        "database coordination sentinel is not protected owner-private state",
                    ) from exc
                _require_identity(
                    sentinel_path,
                    identity,
                    code="WINDOWS_COORDINATION_IDENTITY_DRIFT",
                    message="database coordination sentinel path changed during acquisition",
                )
                _require_parent_guard(
                    parent,
                    parent_handle,
                    parent_identity,
                    code="WINDOWS_COORDINATION_IDENTITY_DRIFT",
                    message="database coordination parent changed during sentinel acquisition",
                )
                return cls(sentinel_path, handle, identity, sid, exclusive=exclusive)
            except BaseException:
                _close_handle(handle)
                raise
        finally:
            _close_handle(parent_handle)

    def revalidate(self) -> WindowsFileIdentity:
        if self._closed:
            raise WindowsMaintenanceError(
                "WINDOWS_COORDINATION_CLOSED",
                "database coordination sentinel is already closed",
            )
        try:
            validate_owner_private_path(self.path, owner_sid=self.owner_sid, directory=False)
        except WindowsSecurityError as exc:
            raise WindowsMaintenanceError(
                "WINDOWS_COORDINATION_PATH_UNSAFE",
                "database coordination sentinel lost owner-private custody",
            ) from exc
        return _require_identity(
            self.path,
            self.identity,
            code="WINDOWS_COORDINATION_IDENTITY_DRIFT",
            message="database coordination sentinel path no longer names the retained file",
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle, self._handle = self._handle, None
        _close_handle(handle)


class WindowsBarrierGuard:
    """Owner-private, non-blocking process barrier backed by a share=0 file handle."""

    def __init__(self, path: str, handle: Any, identity: WindowsFileIdentity, owner_sid: str) -> None:
        self.path = path
        self.identity = identity
        self.owner_sid = owner_sid
        self._handle = handle
        self._closed = False

    @classmethod
    def acquire(cls, path: str | os.PathLike[str], *, owner_sid: str | None = None) -> WindowsBarrierGuard:
        canonical = _canonical_native_path(path, label="maintenance barrier")
        sid = _current_owner_sid(owner_sid)
        parent, parent_handle, parent_identity = _open_parent_guard(
            canonical,
            label="maintenance barrier",
            contention_code="BARRIER_PATH_UNSAFE",
            contention_message="maintenance barrier parent cannot be guarded safely",
        )
        try:
            try:
                validate_owner_private_path(parent, owner_sid=sid, directory=True)
            except WindowsSecurityError as exc:
                raise WindowsMaintenanceError("BARRIER_PATH_UNSAFE", "maintenance barrier parent is not protected owner-private state") from exc

            api = _require_windows()
            existing = _get_file_attributes(api, canonical)
            if existing is not None:
                if existing & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
                    raise WindowsMaintenanceError("BARRIER_PATH_UNSAFE", "maintenance barrier path is unsafe")
                try:
                    validate_owner_private_path(canonical, owner_sid=sid, directory=False)
                except WindowsSecurityError as exc:
                    raise WindowsMaintenanceError("BARRIER_PATH_UNSAFE", "maintenance barrier file is not protected owner-private state") from exc
                security_attributes = None
            else:
                security_attributes = sid

            handle = _open_relative_open_always(
                parent_handle,
                canonical,
                desired_access=_GENERIC_READ | _GENERIC_WRITE,
                share_mode=0,
                owner_sid=security_attributes,
                contention_code="BARRIER_BUSY",
                contention_message="maintenance barrier is already held",
            )
            try:
                identity = _handle_identity(handle)
                _require_parent_guard(
                    parent,
                    parent_handle,
                    parent_identity,
                    code="BARRIER_IDENTITY_DRIFT",
                    message="maintenance barrier parent changed during acquisition",
                )
                if _path_key(identity.final_path) != _path_key(canonical):
                    raise WindowsMaintenanceError("BARRIER_IDENTITY_DRIFT", "maintenance barrier path changed during acquisition")
                try:
                    validate_owner_private_path(canonical, owner_sid=sid, directory=False)
                except WindowsSecurityError as exc:
                    try:
                        _require_parent_guard(
                            parent,
                            parent_handle,
                            parent_identity,
                            code="BARRIER_IDENTITY_DRIFT",
                            message="maintenance barrier parent changed during acquisition",
                        )
                    except WindowsMaintenanceError as drift:
                        raise drift from exc
                    raise WindowsMaintenanceError("BARRIER_PATH_UNSAFE", "maintenance barrier file is not protected owner-private state") from exc
                try:
                    _require_identity(
                        canonical,
                        identity,
                        code="BARRIER_IDENTITY_DRIFT",
                        message="maintenance barrier path changed during acquisition",
                    )
                except WindowsMaintenanceError as exc:
                    if exc.code != "BARRIER_IDENTITY_DRIFT":
                        raise WindowsMaintenanceError("BARRIER_IDENTITY_DRIFT", "maintenance barrier path changed during acquisition") from exc
                    raise
                _require_parent_guard(
                    parent,
                    parent_handle,
                    parent_identity,
                    code="BARRIER_IDENTITY_DRIFT",
                    message="maintenance barrier parent changed during acquisition",
                )
                return cls(canonical, handle, identity, sid)
            except BaseException:
                _close_handle(handle)
                raise
        finally:
            _close_handle(parent_handle)

    def revalidate(self) -> WindowsFileIdentity:
        if self._closed:
            raise WindowsMaintenanceError("BARRIER_CLOSED", "maintenance barrier handle is already closed")
        return _require_identity(
            self.path,
            self.identity,
            code="BARRIER_IDENTITY_DRIFT",
            message="maintenance barrier path no longer names the retained file",
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle, self._handle = self._handle, None
        _close_handle(handle)

    def __enter__(self) -> WindowsBarrierGuard:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


class WindowsRuntimeDBLease:
    """Shared runtime handle compatible with SQLite and peer FactLane runtimes."""

    def __init__(
        self,
        path: str,
        handle: Any,
        identity: WindowsFileIdentity,
        sentinel: _WindowsDBCoordinationSentinel,
    ) -> None:
        self.path = path
        self.identity = identity
        self._handle = handle
        self._sentinel = sentinel
        self._closed = False

    @classmethod
    def open(
        cls,
        db_path: str | os.PathLike[str],
        *,
        owner_sid: str | None = None,
    ) -> WindowsRuntimeDBLease:
        canonical = _canonical_native_path(db_path, label="database")
        _reject_reparse_components(canonical, label="database")
        sid = _current_owner_sid(owner_sid) if owner_sid is not None else None
        sentinel = _WindowsDBCoordinationSentinel.acquire(canonical, exclusive=False)
        try:
            parent, parent_handle, parent_identity = _open_parent_guard(
                canonical,
                label="database",
                contention_code="WINDOWS_PATH_UNAVAILABLE",
                contention_message="database parent cannot be guarded safely",
            )
            try:
                if sid is not None:
                    try:
                        validate_owner_private_path(parent, owner_sid=sid, directory=True)
                    except WindowsSecurityError as exc:
                        raise WindowsMaintenanceError(
                            "RUNTIME_DATABASE_PATH_UNSAFE",
                            "database parent is not protected owner-private state",
                        ) from exc
                    if _get_file_attributes(_require_windows(), canonical) is not None:
                        try:
                            validate_owner_private_path(canonical, owner_sid=sid, directory=False)
                        except WindowsSecurityError as exc:
                            raise WindowsMaintenanceError(
                                "RUNTIME_DATABASE_PATH_UNSAFE",
                                "database file is not protected owner-private state",
                            ) from exc
                handle = _open_relative_open_always(
                    parent_handle,
                    canonical,
                    desired_access=_GENERIC_READ,
                    share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                    owner_sid=sid,
                    contention_code="MAINTENANCE_IN_PROGRESS",
                    contention_message="exclusive database maintenance is in progress",
                )
                try:
                    identity = _handle_identity(handle)
                    if sid is not None:
                        try:
                            validate_owner_only_kernel_object(int(handle), sid)
                            validate_owner_private_path(canonical, owner_sid=sid, directory=False)
                        except WindowsSecurityError as exc:
                            raise WindowsMaintenanceError(
                                "RUNTIME_DATABASE_PATH_UNSAFE",
                                "database file is not protected owner-private state",
                            ) from exc
                    _require_parent_guard(
                        parent,
                        parent_handle,
                        parent_identity,
                        code="RUNTIME_LEASE_IDENTITY_DRIFT",
                        message="database parent changed during runtime lease acquisition",
                    )
                    if _path_key(identity.final_path) != _path_key(canonical):
                        raise WindowsMaintenanceError("RUNTIME_LEASE_IDENTITY_DRIFT", "database path changed during runtime lease acquisition")
                    try:
                        _require_identity(
                            canonical,
                            identity,
                            code="RUNTIME_LEASE_IDENTITY_DRIFT",
                            message="database path changed during runtime lease acquisition",
                        )
                    except WindowsMaintenanceError as exc:
                        if exc.code != "RUNTIME_LEASE_IDENTITY_DRIFT":
                            raise WindowsMaintenanceError("RUNTIME_LEASE_IDENTITY_DRIFT", "database path changed during runtime lease acquisition") from exc
                        raise
                    _require_parent_guard(
                        parent,
                        parent_handle,
                        parent_identity,
                        code="RUNTIME_LEASE_IDENTITY_DRIFT",
                        message="database parent changed during runtime lease acquisition",
                    )
                    sentinel.revalidate()
                    return cls(canonical, handle, identity, sentinel)
                except BaseException:
                    _close_handle(handle)
                    raise
            finally:
                _close_handle(parent_handle)
        except BaseException:
            sentinel.close()
            raise

    def revalidate(self) -> WindowsFileIdentity:
        if self._closed:
            raise WindowsMaintenanceError("RUNTIME_LEASE_CLOSED", "runtime database lease is already closed")
        self._sentinel.revalidate()
        return _require_identity(
            self.path,
            self.identity,
            code="RUNTIME_LEASE_IDENTITY_DRIFT",
            message="database path no longer names the retained runtime file",
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle, self._handle = self._handle, None
        _close_handle(handle)
        sentinel, self._sentinel = self._sentinel, None
        if sentinel is not None:
            sentinel.close()

    def __enter__(self) -> WindowsRuntimeDBLease:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


class WindowsRuntimeDBFamilyCustody:
    """Retain strict main/WAL/SHM pathname custody across SQLite initialization."""

    def __init__(
        self,
        db_path: str,
        runtime_lease: WindowsRuntimeDBLease,
        owner_sid: str,
        parent_path: str,
        parent_handle: Any,
        parent_identity: WindowsFileIdentity,
        sidecars: dict[str, tuple[Any, WindowsFileIdentity]],
        missing: tuple[str, ...],
    ) -> None:
        self.db_path = db_path
        self.runtime_lease = runtime_lease
        self.owner_sid = owner_sid
        self.parent_path = parent_path
        self.parent_identity = parent_identity
        self._parent_handle = parent_handle
        self._sidecars = sidecars
        self._missing = missing
        self._closed = False

    @classmethod
    def acquire_existing(
        cls,
        db_path: str | os.PathLike[str],
        runtime_lease: WindowsRuntimeDBLease,
        *,
        owner_sid: str,
    ) -> WindowsRuntimeDBFamilyCustody:
        canonical = _canonical_native_path(db_path, label="database")
        sid = _current_owner_sid(owner_sid)
        if _path_key(runtime_lease.path) != _path_key(canonical):
            raise WindowsMaintenanceError(
                "RUNTIME_DATABASE_IDENTITY_DRIFT",
                "runtime database lease belongs to another path",
            )
        runtime_lease.revalidate()
        parent, parent_handle, parent_identity = _open_parent_guard(
            canonical,
            label="runtime database family",
            contention_code="RUNTIME_DATABASE_PATH_UNSAFE",
            contention_message="runtime database parent cannot be guarded safely",
        )
        retained: dict[str, tuple[Any, WindowsFileIdentity]] = {}
        try:
            try:
                validate_owner_private_path(parent, owner_sid=sid, directory=True)
                validate_owner_private_path(canonical, owner_sid=sid, directory=False)
            except WindowsSecurityError as exc:
                raise WindowsMaintenanceError(
                    "RUNTIME_DATABASE_PATH_UNSAFE",
                    "runtime database family is not protected owner-private state",
                ) from exc
            missing: list[str] = []
            api = _require_windows()
            for suffix in ("-wal", "-shm"):
                sidecar_path = canonical + suffix
                attributes = _get_file_attributes(api, sidecar_path)
                if attributes is None:
                    missing.append(sidecar_path)
                    continue
                if attributes & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
                    raise WindowsMaintenanceError(
                        "RUNTIME_DATABASE_PATH_UNSAFE",
                        "runtime database sidecar path is unsafe",
                    )
                try:
                    validate_owner_private_path(sidecar_path, owner_sid=sid, directory=False)
                except WindowsSecurityError as exc:
                    raise WindowsMaintenanceError(
                        "RUNTIME_DATABASE_PATH_UNSAFE",
                        "runtime database sidecar is not protected owner-private state",
                    ) from exc
                handle = _open_relative_open_always(
                    parent_handle,
                    sidecar_path,
                    desired_access=_GENERIC_READ,
                    share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                    owner_sid=None,
                    contention_code="RUNTIME_DATABASE_BUSY",
                    contention_message="runtime database sidecar is held by an incompatible process",
                    nt_create_disposition=_FILE_OPEN,
                )
                try:
                    identity = _handle_identity(handle, expected_path=sidecar_path)
                    validate_owner_only_kernel_object(int(handle), sid)
                    _require_identity(
                        sidecar_path,
                        identity,
                        code="RUNTIME_DATABASE_IDENTITY_DRIFT",
                        message="runtime database sidecar path changed during custody acquisition",
                    )
                    retained[sidecar_path] = (handle, identity)
                except BaseException:
                    _close_handle(handle)
                    raise
            _require_parent_guard(
                parent,
                parent_handle,
                parent_identity,
                code="RUNTIME_DATABASE_IDENTITY_DRIFT",
                message="runtime database parent changed during family custody acquisition",
            )
            runtime_lease.revalidate()
            return cls(
                canonical,
                runtime_lease,
                sid,
                parent,
                parent_handle,
                parent_identity,
                retained,
                tuple(missing),
            )
        except BaseException:
            for handle, _identity in retained.values():
                _close_handle(handle)
            _close_handle(parent_handle)
            raise

    def create_missing(self) -> None:
        if self._closed:
            raise WindowsMaintenanceError(
                "RUNTIME_DATABASE_CUSTODY_CLOSED",
                "runtime database family custody is already closed",
            )
        self.revalidate()
        api = _require_windows()
        for sidecar_path in self._missing:
            if _get_file_attributes(api, sidecar_path) is not None:
                raise WindowsMaintenanceError(
                    "RUNTIME_DATABASE_IDENTITY_DRIFT",
                    "runtime database sidecar appeared before secure creation",
                )
        for sidecar_path in self._missing:
            handle = _open_relative_open_always(
                self._parent_handle,
                sidecar_path,
                desired_access=_GENERIC_READ | _GENERIC_WRITE,
                share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                owner_sid=self.owner_sid,
                contention_code="RUNTIME_DATABASE_BUSY",
                contention_message="runtime database sidecar could not be created safely",
                nt_create_disposition=_FILE_CREATE,
            )
            try:
                identity = _handle_identity(handle, expected_path=sidecar_path)
                validate_owner_only_kernel_object(int(handle), self.owner_sid)
                validate_owner_private_path(sidecar_path, owner_sid=self.owner_sid, directory=False)
                _require_identity(
                    sidecar_path,
                    identity,
                    code="RUNTIME_DATABASE_IDENTITY_DRIFT",
                    message="runtime database sidecar path changed during secure creation",
                )
                self._sidecars[sidecar_path] = (handle, identity)
            except BaseException:
                _close_handle(handle)
                raise
        self._missing = ()
        self.revalidate()

    def revalidate(self) -> None:
        if self._closed:
            raise WindowsMaintenanceError(
                "RUNTIME_DATABASE_CUSTODY_CLOSED",
                "runtime database family custody is already closed",
            )
        self.runtime_lease.revalidate()
        _require_parent_guard(
            self.parent_path,
            self._parent_handle,
            self.parent_identity,
            code="RUNTIME_DATABASE_IDENTITY_DRIFT",
            message="runtime database parent changed while family custody was held",
        )
        try:
            validate_owner_private_path(self.parent_path, owner_sid=self.owner_sid, directory=True)
            validate_owner_private_path(self.db_path, owner_sid=self.owner_sid, directory=False)
        except WindowsSecurityError as exc:
            raise WindowsMaintenanceError(
                "RUNTIME_DATABASE_PATH_UNSAFE",
                "runtime database family lost protected owner-private state",
            ) from exc
        for sidecar_path, (handle, identity) in self._sidecars.items():
            try:
                validate_owner_only_kernel_object(int(handle), self.owner_sid)
                validate_owner_private_path(sidecar_path, owner_sid=self.owner_sid, directory=False)
            except WindowsSecurityError as exc:
                raise WindowsMaintenanceError(
                    "RUNTIME_DATABASE_PATH_UNSAFE",
                    "runtime database sidecar lost protected owner-private state",
                ) from exc
            retained = _handle_identity(handle, expected_path=sidecar_path)
            if not _same_file(retained, identity):
                raise WindowsMaintenanceError(
                    "RUNTIME_DATABASE_IDENTITY_DRIFT",
                    "runtime database sidecar identity changed while custody was held",
                )
            _require_identity(
                sidecar_path,
                identity,
                code="RUNTIME_DATABASE_IDENTITY_DRIFT",
                message="runtime database sidecar path changed while custody was held",
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for handle, _identity in self._sidecars.values():
            _close_handle(handle)
        self._sidecars.clear()
        _close_handle(self._parent_handle)
        self._parent_handle = None

    def __enter__(self) -> WindowsRuntimeDBFamilyCustody:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


class WindowsInPlaceMaintenanceLease:
    """DB-scoped Windows A3 lease that keeps SQLite mutation strictly in place."""

    def __init__(
        self,
        path: str,
        handle: Any,
        identity: WindowsFileIdentity,
        parent_path: str,
        parent_handle: Any,
        parent_identity: WindowsFileIdentity,
        sentinel: _WindowsDBCoordinationSentinel,
        connection: sqlite3.Connection,
    ) -> None:
        self.path = path
        self.identity = identity
        self.parent_path = parent_path
        self.parent_identity = parent_identity
        self.connection = connection
        self._handle = handle
        self._parent_handle = parent_handle
        self._sentinel = sentinel
        self._closed = False
        self._retained = False
        self._runtime_activation_prepared = False

    @staticmethod
    def _restore_runtime_regime_under_exclusion(connection: sqlite3.Connection) -> None:
        locking = connection.execute("PRAGMA locking_mode").fetchone()
        if locking is None or str(locking[0]).casefold() != "exclusive":
            raise WindowsMaintenanceError(
                "MAINTENANCE_ACTIVATION_UNPROVEN",
                "continuous SQLite exclusion was lost before runtime regime restoration",
            )
        journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal is None or str(journal[0]).casefold() != "wal":
            raise WindowsMaintenanceError(
                "MAINTENANCE_ACTIVATION_UNPROVEN",
                "database could not restore WAL mode under continuous SQLite exclusion",
            )
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        # DELETE-mode commits are already in the main database. Switching that
        # same excluded connection back to WAL creates no pre-existing WAL
        # frames that require a kernel checkpoint here. A checkpoint can also
        # self-conflict after sqlite3_backup() has targeted this connection.
        # Configure NORMAL for the eventual runtime handoff while the physical
        # exclusive lock remains held by this connection until it is closed.
        connection.execute("PRAGMA locking_mode=NORMAL")
        if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold() != "wal":
            raise WindowsMaintenanceError(
                "MAINTENANCE_ACTIVATION_UNPROVEN",
                "database WAL mode could not be verified under continuous SQLite exclusion",
            )
        if int(connection.execute("PRAGMA synchronous").fetchone()[0]) != 1:
            raise WindowsMaintenanceError(
                "MAINTENANCE_ACTIVATION_UNPROVEN",
                "database synchronous mode could not be verified under continuous SQLite exclusion",
            )
        if int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) < 5000:
            raise WindowsMaintenanceError(
                "MAINTENANCE_ACTIVATION_UNPROVEN",
                "database busy timeout could not be verified under continuous SQLite exclusion",
            )
        locking = connection.execute("PRAGMA locking_mode").fetchone()
        if locking is None or str(locking[0]).casefold() != "normal":
            raise WindowsMaintenanceError(
                "MAINTENANCE_ACTIVATION_UNPROVEN",
                "database locking mode could not be configured for runtime handoff",
            )

    @classmethod
    def acquire(cls, db_path: str | os.PathLike[str]) -> WindowsInPlaceMaintenanceLease:
        canonical = _canonical_native_path(db_path, label="database")
        _reject_reparse_components(canonical, label="database")
        sentinel = _WindowsDBCoordinationSentinel.acquire(canonical, exclusive=True)
        parent_handle = None
        db_handle = None
        connection: sqlite3.Connection | None = None
        try:
            api = _require_windows()
            attributes = _get_file_attributes(api, canonical)
            if attributes is None or attributes & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
                raise WindowsMaintenanceError(
                    "MAINTENANCE_HOLD_DATABASE_UNAVAILABLE",
                    "database file is unavailable for in-place maintenance",
                )

            parent, parent_handle, parent_identity = _open_parent_guard(
                canonical,
                label="database",
                contention_code="MAINTENANCE_HOLD_CONTENTION",
                contention_message="database parent cannot be guarded for in-place maintenance",
            )
            db_handle = _open_file(
                canonical,
                desired_access=_GENERIC_READ | _GENERIC_WRITE,
                share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                creation_disposition=_OPEN_EXISTING,
                contention_code="MAINTENANCE_HOLD_CONTENTION",
                contention_message="database is held by an incompatible external handle",
            )
            identity = _handle_identity(db_handle, expected_path=canonical)
            _require_parent_guard(
                parent,
                parent_handle,
                parent_identity,
                code="MAINTENANCE_HOLD_IDENTITY_DRIFT",
                message="database parent changed during in-place maintenance acquisition",
            )
            _require_identity(
                canonical,
                identity,
                code="MAINTENANCE_HOLD_IDENTITY_DRIFT",
                message="database path changed during in-place maintenance acquisition",
            )
            sentinel.revalidate()

            connection = sqlite3.connect(canonical, timeout=0.0, isolation_level=None)
            sqlite_exclusion_proven = False
            try:
                connection.execute("PRAGMA busy_timeout=0")
                initial_journal = connection.execute("PRAGMA journal_mode").fetchone()
                if initial_journal is None or str(initial_journal[0]).casefold() != "wal":
                    raise WindowsMaintenanceError(
                        "MAINTENANCE_HOLD_RUNTIME_REGIME",
                        "database is not in the required WAL runtime regime before in-place maintenance",
                    )
                locking = connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()
                if locking is None or str(locking[0]).casefold() != "exclusive":
                    raise WindowsMaintenanceError(
                        "MAINTENANCE_HOLD_SQLITE_BUSY",
                        "database could not request exclusive SQLite locking mode",
                    )
                connection.execute("BEGIN EXCLUSIVE")
                connection.execute("COMMIT")
                sqlite_exclusion_proven = True
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is not None and int(checkpoint[0]) != 0:
                    raise WindowsMaintenanceError(
                        "MAINTENANCE_HOLD_SQLITE_BUSY",
                        "database WAL could not be checkpointed because another SQLite user is active",
                    )

                journal = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
                if journal is None or str(journal[0]).casefold() != "delete":
                    raise WindowsMaintenanceError(
                        "MAINTENANCE_HOLD_SQLITE_BUSY",
                        "database could not transition to rollback-journal mode for in-place maintenance",
                    )
                connection.execute("PRAGMA synchronous=FULL")
                locking = connection.execute("PRAGMA locking_mode").fetchone()
                if locking is None or str(locking[0]).casefold() != "exclusive":
                    raise WindowsMaintenanceError(
                        "MAINTENANCE_HOLD_SQLITE_BUSY",
                        "database lost exclusive SQLite locking mode during in-place acquisition",
                    )
            except BaseException as exc:
                failure: BaseException
                if isinstance(exc, WindowsMaintenanceError):
                    failure = exc
                elif isinstance(exc, sqlite3.OperationalError):
                    failure = WindowsMaintenanceError(
                        "MAINTENANCE_HOLD_SQLITE_BUSY",
                        "database is actively held by another SQLite connection",
                    )
                    failure.__cause__ = exc
                elif isinstance(exc, sqlite3.DatabaseError):
                    failure = WindowsMaintenanceError(
                        "MAINTENANCE_HOLD_DATABASE_UNAVAILABLE",
                        "database could not enter safe in-place maintenance mode",
                    )
                    failure.__cause__ = exc
                else:
                    raise
                if sqlite_exclusion_proven:
                    try:
                        cls._restore_runtime_regime_under_exclusion(connection)
                    except BaseException as restore_exc:
                        with _RETAINED_EXCLUSION_LOCK:
                            for retained in (connection, db_handle, parent_handle, sentinel):
                                if retained is not None:
                                    _RETAINED_EXCLUSION_HANDLES.append(retained)
                        connection = None
                        db_handle = None
                        parent_handle = None
                        sentinel = None
                        raise WindowsMaintenanceError(
                            "MAINTENANCE_ACQUISITION_RECOVERY_UNPROVEN",
                            "failed in-place acquisition could not restore the runtime SQLite regime under exclusion",
                        ) from restore_exc
                raise failure

            _require_parent_guard(
                parent,
                parent_handle,
                parent_identity,
                code="MAINTENANCE_HOLD_IDENTITY_DRIFT",
                message="database parent changed during in-place maintenance acquisition",
            )
            current = _handle_identity(db_handle, expected_path=canonical)
            if not _same_file(current, identity):
                raise WindowsMaintenanceError(
                    "MAINTENANCE_HOLD_IDENTITY_DRIFT",
                    "database identity changed during in-place maintenance acquisition",
                )
            sentinel.revalidate()

            lease = cls(canonical, db_handle, identity, parent, parent_handle, parent_identity, sentinel, connection)
            db_handle = None
            parent_handle = None
            connection = None
            sentinel = None
            lease.flush_file_buffers()
            return lease
        finally:
            if parent_handle is not None:
                _close_handle(parent_handle)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            _close_handle(db_handle)
            if sentinel is not None:
                sentinel.close()

    def revalidate(self) -> WindowsFileIdentity:
        self._require_open()
        self._sentinel.revalidate()
        _require_parent_guard(
            self.parent_path,
            self._parent_handle,
            self.parent_identity,
            code="MAINTENANCE_HOLD_IDENTITY_DRIFT",
            message="database parent changed during in-place maintenance",
        )
        current = _handle_identity(self._handle, expected_path=self.path)
        if not _same_file(current, self.identity):
            raise WindowsMaintenanceError(
                "MAINTENANCE_HOLD_IDENTITY_DRIFT",
                "database retained identity changed during in-place maintenance",
            )
        _require_identity(
            self.path,
            self.identity,
            code="MAINTENANCE_HOLD_IDENTITY_DRIFT",
            message="database path no longer names the retained in-place maintenance file",
        )
        return current

    def flush_file_buffers(self) -> None:
        self._require_open()
        self.revalidate()
        api = _require_windows()
        try:
            api.win32file.FlushFileBuffers(self._handle)
        except Exception as exc:
            self.retain_fail_closed()
            raise WindowsMaintenanceError(
                "MAINTENANCE_DURABILITY_UNPROVEN",
                "database file buffers could not be flushed during in-place maintenance",
            ) from exc

    def prepare_runtime_activation(self) -> None:
        """Restore FactLane's normal SQLite regime before releasing the exclusive sentinel."""

        self._require_open()
        self.revalidate()
        try:
            self.flush_file_buffers()
            self._restore_runtime_regime_under_exclusion(self.connection)
            self.flush_file_buffers()
            self._runtime_activation_prepared = True
        except WindowsMaintenanceError:
            self.retain_fail_closed()
            raise
        except sqlite3.DatabaseError as exc:
            self.retain_fail_closed()
            raise WindowsMaintenanceError(
                "MAINTENANCE_ACTIVATION_UNPROVEN",
                "database runtime activation regime could not be restored safely",
            ) from exc

    def release_unmutated(self) -> None:
        """Explicitly release a lease after caller-owned preflight proves no A3 data mutation occurred."""

        self._require_open()
        self.prepare_runtime_activation()
        self.close()

    def retain_fail_closed(self) -> None:
        if self._retained:
            return
        self._retained = True
        self._closed = True
        connection, self.connection = self.connection, None
        handle, self._handle = self._handle, None
        parent_handle, self._parent_handle = self._parent_handle, None
        sentinel, self._sentinel = self._sentinel, None
        with _RETAINED_EXCLUSION_LOCK:
            for retained in (connection, handle, parent_handle, sentinel):
                if retained is not None:
                    _RETAINED_EXCLUSION_HANDLES.append(retained)

    def close(self) -> None:
        if self._retained or self._closed:
            return
        if not self._runtime_activation_prepared:
            self.retain_fail_closed()
            raise WindowsMaintenanceError(
                "MAINTENANCE_ACTIVATION_REQUIRED",
                "in-place maintenance cannot release exclusion before runtime activation is prepared",
            )
        self._closed = True
        connection, self.connection = self.connection, None
        handle, self._handle = self._handle, None
        parent_handle, self._parent_handle = self._parent_handle, None
        sentinel, self._sentinel = self._sentinel, None
        if connection is not None:
            connection.close()
        _close_handle(handle)
        _close_handle(parent_handle)
        if sentinel is not None:
            sentinel.close()

    def _require_open(self) -> None:
        if self._closed:
            raise WindowsMaintenanceError(
                "MAINTENANCE_LEASE_CLOSED",
                "in-place maintenance lease is already closed",
            )

    def __enter__(self) -> WindowsInPlaceMaintenanceLease:
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        if exc_type is not None:
            self.retain_fail_closed()
            return
        if not self._runtime_activation_prepared:
            self.prepare_runtime_activation()
        self.close()


class WindowsStagedMigrationLease:
    """Two-phase native Windows migration lease with mutation-free Phase 1 custody."""

    PHASE_CUSTODY = "CUSTODY"
    PHASE_BACKUP_GUARD = "BACKUP_GUARD"
    PHASE_RECOVERY_GUARD = "RECOVERY_GUARD"
    PHASE_MUTATION = "MUTATION"
    PHASE_EXACT_RESTORED = "EXACT_RESTORED"

    def __init__(
        self,
        db_path: str,
        db_handle: Any,
        db_identity: WindowsFileIdentity,
        parent_path: str,
        parent_handle: Any,
        parent_identity: WindowsFileIdentity,
        sentinel: _WindowsDBCoordinationSentinel,
        sidecars: dict[str, tuple[Any, WindowsFileIdentity]],
    ) -> None:
        self.db_path = db_path
        self.identity = db_identity
        self.parent_path = parent_path
        self.parent_identity = parent_identity
        self.sidecar_identities = {path: identity for path, (_handle, identity) in sidecars.items()}
        self.connection: sqlite3.Connection | None = None
        self.phase = self.PHASE_CUSTODY
        self._db_handle = db_handle
        self._parent_handle = parent_handle
        self._sentinel = sentinel
        self._sidecar_handles = {path: handle for path, (handle, _identity) in sidecars.items()}
        self._runtime_activation_prepared = False
        self._exact_restore_expected_sha256: str | None = None
        self._exact_restore_expected_size: int | None = None
        self._exact_release_prepared = False
        self._recovery_initial_journal: str | None = None
        self._recovery_byte_state: dict[str, tuple[WindowsFileIdentity, int, str]] | None = None
        self._recovery_scratch_root: str | None = None
        self._stage_db_path: str | None = None
        self._stage_guard: Any = None
        self._staged_image_sha256: str | None = None
        self._staged_image_size: int | None = None
        self._sidecars_delete_pending = False
        self._active_overwrite_started = False
        self._closed = False
        self._retained = False

    @classmethod
    def acquire(cls, db_path: str | os.PathLike[str]) -> WindowsStagedMigrationLease:
        canonical = _canonical_native_path(db_path, label="database")
        _reject_reparse_components(canonical, label="database")
        sentinel = _WindowsDBCoordinationSentinel.acquire(canonical, exclusive=True)
        parent_handle = None
        db_handle = None
        sidecars: dict[str, tuple[Any, WindowsFileIdentity]] = {}
        try:
            api = _require_windows()
            attributes = _get_file_attributes(api, canonical)
            if attributes is None or attributes & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY):
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_DATABASE_UNAVAILABLE",
                    "database file is unavailable for staged migration custody",
                )
            initial_sidecars = _scan_sidecars(canonical)
            parent, parent_handle, parent_identity = _open_parent_guard(
                canonical,
                label="database",
                contention_code="MIGRATION_HOLD_CONTENTION",
                contention_message="database parent cannot be guarded for staged migration",
            )
            db_handle = _open_file(
                canonical,
                desired_access=_GENERIC_READ | _GENERIC_WRITE,
                # The active database is never opened through SQLite while this
                # lease is held.  Denying WRITE/DELETE sharing here is the
                # native maintenance boundary: a pre-existing writer makes
                # acquisition fail, and no new same-user raw writer can enter.
                share_mode=_FILE_SHARE_READ,
                creation_disposition=_OPEN_EXISTING,
                contention_code="MIGRATION_HOLD_CONTENTION",
                contention_message="database is held by an incompatible external handle",
            )
            db_identity = _handle_identity(db_handle, expected_path=canonical)
            _require_parent_guard(
                parent,
                parent_handle,
                parent_identity,
                code="MIGRATION_HOLD_IDENTITY_DRIFT",
                message="database parent changed during staged migration acquisition",
            )
            _require_identity(
                canonical,
                db_identity,
                code="MIGRATION_HOLD_IDENTITY_DRIFT",
                message="database path changed during staged migration acquisition",
            )
            sentinel.revalidate()
            if _scan_sidecars(canonical) != initial_sidecars:
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SIDECAR_UNCERTAIN",
                    "database WAL/SHM sidecar set changed during staged migration acquisition",
                )
            sidecars = _acquire_sidecar_custody(canonical, writable=True)
            if tuple(sidecars) != initial_sidecars:
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SIDECAR_UNCERTAIN",
                    "database WAL/SHM sidecar set changed during staged migration acquisition",
                )
            _require_parent_guard(
                parent,
                parent_handle,
                parent_identity,
                code="MIGRATION_HOLD_IDENTITY_DRIFT",
                message="database parent changed during staged migration acquisition",
            )
            current = _handle_identity(db_handle, expected_path=canonical)
            if not _same_file(current, db_identity):
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_IDENTITY_DRIFT",
                    "database identity changed during staged migration acquisition",
                )
            sentinel.revalidate()
            lease = cls(canonical, db_handle, db_identity, parent, parent_handle, parent_identity, sentinel, sidecars)
            db_handle = None
            parent_handle = None
            sentinel = None
            sidecars = {}
            return lease
        finally:
            for handle, _identity in sidecars.values():
                _close_handle(handle)
            _close_handle(db_handle)
            _close_handle(parent_handle)
            if sentinel is not None:
                sentinel.close()

    def revalidate(self) -> WindowsFileIdentity:
        self._require_open()
        self._sentinel.revalidate()
        _require_parent_guard(
            self.parent_path,
            self._parent_handle,
            self.parent_identity,
            code="MIGRATION_HOLD_IDENTITY_DRIFT",
            message="database parent changed while staged migration custody was held",
        )
        current = _handle_identity(self._db_handle, expected_path=self.db_path)
        if not _same_file(current, self.identity):
            raise WindowsMaintenanceError(
                "MIGRATION_HOLD_IDENTITY_DRIFT",
                "database retained identity changed while staged migration custody was held",
            )
        _require_identity(
            self.db_path,
            self.identity,
            code="MIGRATION_HOLD_IDENTITY_DRIFT",
            message="database path no longer names the retained staged migration file",
        )
        if not self._sidecars_delete_pending:
            current_sidecars = _scan_sidecars(self.db_path)
            if tuple(self._sidecar_handles) != current_sidecars:
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SIDECAR_UNCERTAIN",
                    "database WAL/SHM sidecar set changed while staged migration custody was held",
                )
            for path, expected in self.sidecar_identities.items():
                current_sidecar = _handle_identity(self._sidecar_handles[path], expected_path=path)
                if not _same_file(current_sidecar, expected):
                    raise WindowsMaintenanceError(
                        "MIGRATION_HOLD_IDENTITY_DRIFT",
                        "database sidecar identity changed while staged migration custody was held",
                    )
        else:
            for path, expected in self.sidecar_identities.items():
                handle = self._sidecar_handles.get(path)
                if handle is None:
                    raise WindowsMaintenanceError(
                        "MIGRATION_HOLD_SIDECAR_UNCERTAIN",
                        "delete-pending database sidecar custody was lost",
                    )
                current_sidecar = _handle_identity(handle)
                if not _same_file(current_sidecar, expected):
                    raise WindowsMaintenanceError(
                        "MIGRATION_HOLD_IDENTITY_DRIFT",
                        "delete-pending database sidecar identity changed while staged migration custody was held",
                    )
        return current

    @staticmethod
    def _hash_file_bytes(path: str) -> tuple[int, str]:
        size = int(os.path.getsize(path))
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return size, digest.hexdigest()

    @staticmethod
    def _hash_retained_handle_bytes(handle: Any) -> tuple[int, str]:
        kernel32 = _require_windows().kernel32
        get_size = kernel32.GetFileSizeEx
        get_size.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
        get_size.restype = wintypes.BOOL
        set_pointer = kernel32.SetFilePointerEx
        set_pointer.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
        set_pointer.restype = wintypes.BOOL
        read_file = kernel32.ReadFile
        read_file.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        read_file.restype = wintypes.BOOL

        size_value = ctypes.c_longlong()
        ctypes.set_last_error(0)
        if not get_size(_handle_value(handle), ctypes.byref(size_value)) or size_value.value < 0:
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_BYTE_DRIFT",
                "migration recovery retained file size could not be read",
            )
        size = int(size_value.value)
        ctypes.set_last_error(0)
        if not set_pointer(_handle_value(handle), 0, None, 0):
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_BYTE_DRIFT",
                "migration recovery retained file position could not be reset",
            )
        remaining = size
        digest = hashlib.sha256()
        while remaining:
            chunk_size = min(1024 * 1024, remaining)
            buffer = ctypes.create_string_buffer(chunk_size)
            read = wintypes.DWORD()
            ctypes.set_last_error(0)
            if not read_file(
                _handle_value(handle),
                ctypes.byref(buffer),
                chunk_size,
                ctypes.byref(read),
                None,
            ):
                raise WindowsMaintenanceError(
                    "MIGRATION_RECOVERY_BYTE_DRIFT",
                    "migration recovery retained file bytes could not be read",
                )
            read_count = int(read.value)
            if read_count <= 0 or read_count > chunk_size:
                raise WindowsMaintenanceError(
                    "MIGRATION_RECOVERY_BYTE_DRIFT",
                    "migration recovery retained file read ended before expected size",
                )
            digest.update(buffer.raw[:read_count])
            remaining -= read_count
        ctypes.set_last_error(0)
        if not set_pointer(_handle_value(handle), 0, None, 0):
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_BYTE_DRIFT",
                "migration recovery retained file position could not be restored",
            )
        return size, digest.hexdigest()

    def _capture_recovery_byte_state(self) -> dict[str, tuple[WindowsFileIdentity, int, str]]:
        self._require_open()
        self.revalidate()
        if _scan_sidecars(self.db_path) != tuple(self.sidecar_identities):
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_BYTE_DRIFT",
                "migration recovery sidecar set changed before semantic guard",
            )
        state: dict[str, tuple[WindowsFileIdentity, int, str]] = {}
        retained: dict[str, tuple[WindowsFileIdentity, Any]] = {
            self.db_path: (self.identity, self._db_handle),
            **{
                path: (identity, self._sidecar_handles[path])
                for path, identity in self.sidecar_identities.items()
            },
        }
        for path, (identity, handle) in retained.items():
            before = _handle_identity(handle, expected_path=path)
            if not _same_file(before, identity):
                raise WindowsMaintenanceError(
                    "MIGRATION_RECOVERY_BYTE_DRIFT",
                    "migration recovery retained file identity changed before semantic guard",
                )
            size, digest = self._hash_retained_handle_bytes(handle)
            after = _handle_identity(handle, expected_path=path)
            if not _same_file(after, identity):
                raise WindowsMaintenanceError(
                    "MIGRATION_RECOVERY_BYTE_DRIFT",
                    "migration recovery retained file identity changed while byte state was captured",
                )
            state[path] = (identity, size, digest)
        return state

    @staticmethod
    def _copy_retained_handle_bytes(handle: Any, target: Path) -> None:
        kernel32 = _require_windows().kernel32
        get_size = kernel32.GetFileSizeEx
        get_size.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
        get_size.restype = wintypes.BOOL
        set_pointer = kernel32.SetFilePointerEx
        set_pointer.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
        set_pointer.restype = wintypes.BOOL
        read_file = kernel32.ReadFile
        read_file.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        read_file.restype = wintypes.BOOL

        size_value = ctypes.c_longlong()
        ctypes.set_last_error(0)
        if not get_size(_handle_value(handle), ctypes.byref(size_value)) or size_value.value < 0:
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_SNAPSHOT_FAILED",
                "migration recovery retained source size could not be read",
            )
        ctypes.set_last_error(0)
        if not set_pointer(_handle_value(handle), 0, None, 0):
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_SNAPSHOT_FAILED",
                "migration recovery retained source position could not be reset",
            )
        remaining = int(size_value.value)
        try:
            with target.open("xb", buffering=0) as destination:
                while remaining:
                    chunk_size = min(1024 * 1024, remaining)
                    buffer = ctypes.create_string_buffer(chunk_size)
                    read = wintypes.DWORD()
                    ctypes.set_last_error(0)
                    if not read_file(
                        _handle_value(handle),
                        ctypes.byref(buffer),
                        chunk_size,
                        ctypes.byref(read),
                        None,
                    ):
                        raise WindowsMaintenanceError(
                            "MIGRATION_RECOVERY_SNAPSHOT_FAILED",
                            "migration recovery retained source bytes could not be copied",
                        )
                    read_count = int(read.value)
                    if read_count <= 0 or read_count > chunk_size:
                        raise WindowsMaintenanceError(
                            "MIGRATION_RECOVERY_SNAPSHOT_FAILED",
                            "migration recovery retained source copy ended before the expected size",
                        )
                    if destination.write(buffer.raw[:read_count]) != read_count:
                        raise WindowsMaintenanceError(
                            "MIGRATION_RECOVERY_SNAPSHOT_FAILED",
                            "migration recovery semantic snapshot could not be written completely",
                        )
                    remaining -= read_count
                destination.flush()
                os.fsync(destination.fileno())
        finally:
            ctypes.set_last_error(0)
            if not set_pointer(_handle_value(handle), 0, None, 0):
                raise WindowsMaintenanceError(
                    "MIGRATION_RECOVERY_SNAPSHOT_FAILED",
                    "migration recovery retained source position could not be restored",
                )

    def _create_recovery_semantic_snapshot(self) -> str:
        self._require_open()
        if self._recovery_scratch_root is not None:
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_SNAPSHOT_FAILED",
                "migration recovery semantic snapshot already exists",
            )
        raw_root = Path(tempfile.mkdtemp(prefix="factlane-recovery-guard-"))
        root = raw_root
        owner_sid = current_user_sid()
        try:
            _reject_reparse_components(str(raw_root), label="migration recovery scratch")
            root = Path(
                _canonical_native_path(
                    os.path.realpath(raw_root, strict=True),
                    label="migration recovery scratch",
                )
            )
            _reject_reparse_components(str(root), label="migration recovery scratch")
            ensure_owner_private_directory(root, owner_sid=owner_sid)
            scratch_db = root / Path(self.db_path).name
            self._copy_retained_handle_bytes(self._db_handle, scratch_db)
            protect_owner_private_path(scratch_db, owner_sid=owner_sid, directory=False)
            for sidecar_path, handle in self._sidecar_handles.items():
                suffix = sidecar_path[len(self.db_path) :]
                if suffix not in {"-wal", "-shm"}:
                    raise WindowsMaintenanceError(
                        "MIGRATION_RECOVERY_SNAPSHOT_FAILED",
                        "migration recovery sidecar identity is outside the expected database family",
                    )
                scratch_sidecar = Path(str(scratch_db) + suffix)
                self._copy_retained_handle_bytes(handle, scratch_sidecar)
                protect_owner_private_path(scratch_sidecar, owner_sid=owner_sid, directory=False)
            self._recovery_scratch_root = str(root)
            self._stage_db_path = str(scratch_db)
            return str(scratch_db)
        except BaseException:
            shutil.rmtree(raw_root, ignore_errors=True)
            raise

    def _cleanup_recovery_semantic_snapshot(self) -> None:
        stage_guard, self._stage_guard = self._stage_guard, None
        if stage_guard is not None:
            stage_guard.close()
        root = self._recovery_scratch_root
        if root is None:
            self._stage_db_path = None
            self._staged_image_sha256 = None
            self._staged_image_size = None
            return
        try:
            shutil.rmtree(root)
        except OSError as exc:
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_SNAPSHOT_CLEANUP_FAILED",
                "migration recovery semantic snapshot could not be removed safely",
            ) from exc
        if os.path.exists(root):
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_SNAPSHOT_CLEANUP_FAILED",
                "migration recovery semantic snapshot still exists after cleanup",
            )
        self._recovery_scratch_root = None
        self._stage_db_path = None
        self._staged_image_sha256 = None
        self._staged_image_size = None

    def _require_recovery_byte_state(
        self,
        expected: dict[str, tuple[WindowsFileIdentity, int, str]],
        *,
        message: str,
    ) -> None:
        self._require_open()
        self.revalidate()
        expected_sidecars = tuple(path for path in expected if path != self.db_path)
        if _scan_sidecars(self.db_path) != expected_sidecars:
            raise WindowsMaintenanceError("MIGRATION_RECOVERY_BYTE_DRIFT", message)
        retained_handles: dict[str, Any] | None = None
        if not self._sidecars_delete_pending:
            retained_handles = {self.db_path: self._db_handle, **self._sidecar_handles}
        for path, (identity, expected_size, expected_sha256) in expected.items():
            if retained_handles is not None:
                handle = retained_handles.get(path)
                if handle is None:
                    raise WindowsMaintenanceError("MIGRATION_RECOVERY_BYTE_DRIFT", message)
                before = _handle_identity(handle, expected_path=path)
                if not _same_file(before, identity):
                    raise WindowsMaintenanceError("MIGRATION_RECOVERY_BYTE_DRIFT", message)
                size, digest = self._hash_retained_handle_bytes(handle)
                after = _handle_identity(handle, expected_path=path)
                if not _same_file(after, identity):
                    raise WindowsMaintenanceError("MIGRATION_RECOVERY_BYTE_DRIFT", message)
            else:
                _require_identity(
                    path,
                    identity,
                    code="MIGRATION_RECOVERY_BYTE_DRIFT",
                    message=message,
                )
                size, digest = self._hash_file_bytes(path)
                _require_identity(
                    path,
                    identity,
                    code="MIGRATION_RECOVERY_BYTE_DRIFT",
                    message=message,
                )
            if size != expected_size or digest.casefold() != expected_sha256.casefold():
                raise WindowsMaintenanceError("MIGRATION_RECOVERY_BYTE_DRIFT", message)

    def open_backup_source(self) -> sqlite3.Connection:
        """Open an owner-private exact family snapshot; never SQLite-open the active DB."""

        self._require_open()
        if self.phase != self.PHASE_CUSTODY:
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "staged migration backup source is available only from Phase 1 custody",
            )
        byte_state = self._capture_recovery_byte_state()
        connection: sqlite3.Connection | None = None
        try:
            stage_db = self._create_recovery_semantic_snapshot()
            connection = sqlite3.connect(stage_db, timeout=0.0, isolation_level=None)
            connection.execute("PRAGMA busy_timeout=0")
            initial_journal = connection.execute("PRAGMA journal_mode").fetchone()
            if initial_journal is None or str(initial_journal[0]).casefold() != "wal":
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_RUNTIME_REGIME",
                    "database is not in the required WAL runtime regime before migration backup",
                )
            locking = connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()
            if locking is None or str(locking[0]).casefold() != "exclusive":
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SQLITE_BUSY",
                    "database could not request exclusive SQLite locking mode for migration backup",
                )
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("COMMIT")
            self.connection = connection
            connection = None
            self._recovery_initial_journal = "wal"
            self._recovery_byte_state = byte_state
            self.phase = self.PHASE_BACKUP_GUARD
            self.revalidate()
            self._require_recovery_byte_state(
                byte_state,
                message="active database family changed while private backup source was established",
            )
            return self.connection
        except BaseException as exc:
            active_connection, self.connection = self.connection, None
            if active_connection is not None:
                try:
                    active_connection.close()
                except Exception:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            try:
                self._cleanup_recovery_semantic_snapshot()
                self._require_recovery_byte_state(
                    byte_state,
                    message="active database family changed while failed backup source was released",
                )
            except BaseException as custody_exc:
                self.retain_fail_closed()
                raise WindowsMaintenanceError(
                    "MIGRATION_BACKUP_GUARD_CUSTODY_UNPROVEN",
                    "migration backup guard could not preserve active database family custody",
                ) from custody_exc
            self._recovery_initial_journal = None
            self._recovery_byte_state = None
            self.phase = self.PHASE_CUSTODY
            if isinstance(exc, WindowsMaintenanceError):
                raise
            if isinstance(exc, sqlite3.DatabaseError):
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SQLITE_BUSY",
                    "database could not obtain exclusive SQLite custody for migration backup",
                ) from exc
            raise

    def transition_to_mutation(self, *, backup_manifest_verified: bool) -> sqlite3.Connection:
        """Prepare the private staged image only after backup+manifest are durable."""

        self._require_open()
        if self.phase != self.PHASE_BACKUP_GUARD or self.connection is None:
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "staged migration mutation requires the held SQLite backup-source guard",
            )
        if backup_manifest_verified is not True:
            raise WindowsMaintenanceError(
                "MIGRATION_BACKUP_PROOF_REQUIRED",
                "staged migration mutation requires a durably verified backup and manifest",
            )
        if self._recovery_byte_state is None:
            raise WindowsMaintenanceError(
                "MIGRATION_BACKUP_PROOF_REQUIRED",
                "staged migration mutation is missing the exact active-family byte proof",
            )
        self._require_recovery_byte_state(
            self._recovery_byte_state,
            message="active database family changed before staged mutation",
        )
        try:
            journal = self.connection.execute("PRAGMA journal_mode").fetchone()
            locking = self.connection.execute("PRAGMA locking_mode").fetchone()
            if (
                journal is None
                or str(journal[0]).casefold() != "wal"
                or locking is None
                or str(locking[0]).casefold() != "exclusive"
            ):
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SQLITE_BUSY",
                    "migration backup-source SQLite exclusion was lost before mutation",
                )
            checkpoint = self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SQLITE_BUSY",
                    "database WAL could not be checkpointed before migration mutation",
                )
            journal = self.connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            if journal is None or str(journal[0]).casefold() != "delete":
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SQLITE_BUSY",
                    "database could not transition to rollback-journal mode for migration mutation",
                )
            self.connection.execute("PRAGMA synchronous=FULL")
            locking = self.connection.execute("PRAGMA locking_mode").fetchone()
            if locking is None or str(locking[0]).casefold() != "exclusive":
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SQLITE_BUSY",
                    "database lost exclusive SQLite locking mode during migration mutation transition",
                )
            self.phase = self.PHASE_MUTATION
            self.revalidate()
            self._require_recovery_byte_state(
                self._recovery_byte_state,
                message="active database family changed while private mutation stage was prepared",
            )
            return self.connection
        except BaseException as exc:
            self.retain_fail_closed()
            if isinstance(exc, WindowsMaintenanceError):
                raise
            if isinstance(exc, sqlite3.DatabaseError):
                raise WindowsMaintenanceError(
                    "MIGRATION_MUTATION_STAGE_UNPROVEN",
                    "migration mutation-stage transition failed after backup verification",
                ) from exc
            raise

    def open_verified_recovery_source(self, *, recovery_proof_verified: bool) -> sqlite3.Connection:
        """Open a byte-stable recovery target under SQLite exclusion without source mutation."""

        self._require_open()
        if self.phase != self.PHASE_CUSTODY or self.connection is not None:
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "verified migration recovery source requires fresh Phase 1 custody",
            )
        if recovery_proof_verified is not True:
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_PROOF_REQUIRED",
                "migration recovery guard requires verified backup, manifest, and target identity proof",
            )
        byte_state = self._capture_recovery_byte_state()
        connection: sqlite3.Connection | None = None
        try:
            scratch_db = self._create_recovery_semantic_snapshot()
            connection = sqlite3.connect(scratch_db, timeout=0.0, isolation_level=None)
            connection.execute("PRAGMA busy_timeout=0")
            locking = connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()
            if locking is None or str(locking[0]).casefold() != "exclusive":
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SQLITE_BUSY",
                    "recovery semantic snapshot could not request exclusive SQLite locking mode",
                )
            journal = connection.execute("PRAGMA journal_mode").fetchone()
            if journal is None or str(journal[0]).casefold() not in {"wal", "delete"}:
                raise WindowsMaintenanceError(
                    "MIGRATION_RECOVERY_RUNTIME_REGIME",
                    "verified migration recovery requires WAL or DELETE journal mode",
                )
            journal_mode = str(journal[0]).casefold()
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("COMMIT")
            self.connection = connection
            connection = None
            self._recovery_initial_journal = journal_mode
            self._recovery_byte_state = byte_state
            self.phase = self.PHASE_RECOVERY_GUARD
            self.revalidate()
            self._require_recovery_byte_state(
                byte_state,
                message="migration recovery source bytes changed while semantic guard was established",
            )
            return self.connection
        except BaseException as exc:
            active_connection, self.connection = self.connection, None
            if active_connection is not None:
                try:
                    active_connection.close()
                except Exception:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self.phase = self.PHASE_CUSTODY
            self._recovery_initial_journal = None
            self._recovery_byte_state = None
            try:
                self._cleanup_recovery_semantic_snapshot()
                self._require_recovery_byte_state(
                    byte_state,
                    message="migration recovery source bytes changed while failed semantic guard was released",
                )
            except BaseException:
                self.retain_fail_closed()
                raise
            if isinstance(exc, WindowsMaintenanceError):
                raise
            if isinstance(exc, sqlite3.DatabaseError):
                raise WindowsMaintenanceError(
                    "MIGRATION_RECOVERY_STAGE_UNPROVEN",
                    "verified migration recovery could not establish semantic guard safely",
                ) from exc
            raise

    def transition_verified_recovery_guard_to_mutation(
        self,
        *,
        recovery_target_verified: bool,
    ) -> sqlite3.Connection:
        """Mutate only after the guarded target is separately proven recovery-eligible."""

        self._require_open()
        if (
            self.phase != self.PHASE_RECOVERY_GUARD
            or self.connection is None
            or self._recovery_initial_journal not in {"wal", "delete"}
            or self._recovery_byte_state is None
        ):
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "migration recovery mutation requires the held semantic recovery guard",
            )
        if recovery_target_verified is not True:
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_TARGET_PROOF_REQUIRED",
                "migration recovery mutation requires separate semantic target eligibility proof",
            )
        self._require_recovery_byte_state(
            self._recovery_byte_state,
            message="migration recovery source bytes changed before semantic eligibility proof completed",
        )
        try:
            initial_journal = self._recovery_initial_journal
            byte_state = self._recovery_byte_state
            scratch_connection = self.connection
            journal = scratch_connection.execute("PRAGMA journal_mode").fetchone()
            locking = scratch_connection.execute("PRAGMA locking_mode").fetchone()
            if (
                journal is None
                or str(journal[0]).casefold() != initial_journal
                or locking is None
                or str(locking[0]).casefold() != "exclusive"
            ):
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SQLITE_BUSY",
                    "migration recovery semantic snapshot changed before authorized mutation",
                )
            _migration_recovery_checkpoint("before_cleanup_mutation")
            if initial_journal == "wal":
                checkpoint = scratch_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is not None and int(checkpoint[0]) != 0:
                    raise WindowsMaintenanceError(
                        "MIGRATION_HOLD_SQLITE_BUSY",
                        "private recovery stage WAL could not be checkpointed before mutation",
                    )
                _migration_recovery_checkpoint("after_wal_checkpoint")
            journal = scratch_connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            if journal is None or str(journal[0]).casefold() != "delete":
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SQLITE_BUSY",
                    "private recovery stage could not establish DELETE journal mode",
                )
            scratch_connection.execute("PRAGMA synchronous=FULL")
            synchronous = scratch_connection.execute("PRAGMA synchronous").fetchone()
            locking = scratch_connection.execute("PRAGMA locking_mode").fetchone()
            if (
                synchronous is None
                or int(synchronous[0]) != 2
                or locking is None
                or str(locking[0]).casefold() != "exclusive"
            ):
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SQLITE_BUSY",
                    "private recovery stage could not establish DELETE/FULL/EXCLUSIVE custody",
                )
            self._require_recovery_byte_state(
                byte_state,
                message="active database family changed while private recovery stage entered mutation",
            )
            self.phase = self.PHASE_MUTATION
            self.revalidate()
            return scratch_connection
        except BaseException as exc:
            self.retain_fail_closed()
            if isinstance(exc, WindowsMaintenanceError):
                raise
            if isinstance(exc, sqlite3.DatabaseError):
                raise WindowsMaintenanceError(
                    "MIGRATION_RECOVERY_STAGE_UNPROVEN",
                    "verified migration recovery mutation could not be established safely",
                ) from exc
            raise

    def transition_verified_recovery_to_mutation(
        self,
        *,
        recovery_proof_verified: bool,
        recovery_target_verified: bool = False,
    ) -> sqlite3.Connection:
        """Compatibility entry point that cannot mutate without the second target proof."""

        self.open_verified_recovery_source(recovery_proof_verified=recovery_proof_verified)
        return self.transition_verified_recovery_guard_to_mutation(
            recovery_target_verified=recovery_target_verified,
        )

    @property
    def staged_database_path(self) -> str | None:
        """Owner-private SQLite stage path, never the active database path."""

        return self._stage_db_path

    def freeze_staged_image(self) -> dict[str, Any]:
        """Close and strictly custody the fully verified private mutation image."""

        self._require_open()
        if self.phase != self.PHASE_MUTATION or self.connection is None or self._stage_db_path is None:
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "staged image freeze requires the private mutation-stage SQLite connection",
            )
        if self.connection.in_transaction:
            raise WindowsMaintenanceError(
                "MIGRATION_STAGE_UNVERIFIED",
                "staged image cannot be frozen while a SQLite transaction is active",
            )
        journal = self.connection.execute("PRAGMA journal_mode").fetchone()
        synchronous = self.connection.execute("PRAGMA synchronous").fetchone()
        locking = self.connection.execute("PRAGMA locking_mode").fetchone()
        if (
            journal is None
            or str(journal[0]).casefold() != "delete"
            or synchronous is None
            or int(synchronous[0]) != 2
            or locking is None
            or str(locking[0]).casefold() != "exclusive"
        ):
            raise WindowsMaintenanceError(
                "MIGRATION_STAGE_UNVERIFIED",
                "staged image requires DELETE/FULL/EXCLUSIVE SQLite state before freeze",
            )
        expected_active = self._recovery_byte_state
        if expected_active is None:
            raise WindowsMaintenanceError(
                "MIGRATION_STAGE_UNVERIFIED",
                "staged image is missing the pre-mutation active-family proof",
            )
        self._require_recovery_byte_state(
            expected_active,
            message="active database family changed before staged image freeze",
        )
        # Restore the ordinary v3 runtime header/regime on the private stage,
        # never on the active path.  The subsequent strict stage guard and
        # read-only semantic recheck bind the exact bytes that will be promoted.
        WindowsInPlaceMaintenanceLease._restore_runtime_regime_under_exclusion(self.connection)
        self.connection.close()
        self.connection = None
        stage_path = self._stage_db_path
        if _scan_sidecars(stage_path):
            raise WindowsMaintenanceError(
                "MIGRATION_STAGE_UNVERIFIED",
                "staged image retained unexpected WAL/SHM state after DELETE transition",
            )
        guard: Any = None
        try:
            guard = WindowsMigrationArtifactCustody.open_existing(stage_path)
            receipt = guard.read_exact()
            self._stage_guard = guard
            guard = None
            self._staged_image_sha256 = str(receipt["sha256"])
            self._staged_image_size = int(receipt["size"])
            self._require_recovery_byte_state(
                expected_active,
                message="active database family changed while staged image was frozen",
            )
            return {
                "path": stage_path,
                "size": self._staged_image_size,
                "sha256": self._staged_image_sha256,
                "file_id": str(receipt["file_id"]),
                "volume_serial_number": int(receipt["volume_serial_number"]),
            }
        except BaseException:
            if guard is not None:
                guard.close()
            self.retain_fail_closed()
            raise

    def _mark_active_sidecars_delete_pending(self) -> None:
        """Reserve both sidecar names and hold them delete-pending until custody release."""

        self._require_open()
        if self._sidecars_delete_pending:
            return
        if _scan_sidecars(self.db_path) != tuple(self._sidecar_handles):
            raise WindowsMaintenanceError(
                "MIGRATION_HOLD_SIDECAR_UNCERTAIN",
                "database sidecar set changed before active-image mutation",
            )
        newly_created: list[str] = []
        try:
            for suffix in ("-wal", "-shm"):
                path = self.db_path + suffix
                if path not in self._sidecar_handles:
                    handle = _open_file(
                        path,
                        desired_access=_GENERIC_READ | _GENERIC_WRITE | _DELETE,
                        share_mode=0,
                        creation_disposition=_CREATE_NEW,
                        contention_code="MIGRATION_HOLD_SIDECAR_UNCERTAIN",
                        contention_message="database sidecar namespace could not be reserved for mutation",
                    )
                    try:
                        identity = _handle_identity(handle, expected_path=path)
                    except BaseException:
                        _close_handle(handle)
                        raise
                    self._sidecar_handles[path] = handle
                    self.sidecar_identities[path] = identity
                    newly_created.append(path)

            disposition = _FILE_DISPOSITION_INFO(wintypes.BOOL(1))
            set_info = _require_windows().kernel32.SetFileInformationByHandle
            set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
            set_info.restype = wintypes.BOOL
            for path, handle in self._sidecar_handles.items():
                ctypes.set_last_error(0)
                if not set_info(
                    _handle_value(handle),
                    _FILE_DISPOSITION_INFO_CLASS,
                    ctypes.byref(disposition),
                    ctypes.sizeof(disposition),
                ):
                    raise WindowsMaintenanceError(
                        "MIGRATION_HOLD_SIDECAR_UNCERTAIN",
                        "database sidecar could not be invalidated under retained custody",
                    )
                current = _handle_identity(handle)
                if not _same_file(current, self.sidecar_identities[path]):
                    raise WindowsMaintenanceError(
                        "MIGRATION_HOLD_IDENTITY_DRIFT",
                        "database sidecar identity changed while invalidation was established",
                    )
            self._sidecars_delete_pending = True
        except BaseException:
            # Any partial reservation is still held fail-closed; it must not be
            # released while the active main-file custody remains uncertain.
            self.retain_fail_closed()
            raise

    def promote_frozen_stage(
        self,
        *,
        rollback_intent_verified: bool,
        expected_sha256: str,
        expected_size: int,
    ) -> dict[str, Any]:
        """Raw-promote the frozen private v3 image through the retained active handle."""

        self._require_open()
        if self.phase != self.PHASE_MUTATION or self.connection is not None:
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "active promotion requires a frozen private mutation image",
            )
        if rollback_intent_verified is not True:
            raise WindowsMaintenanceError(
                "MIGRATION_ROLLBACK_INTENT_REQUIRED",
                "durable rollback intent is required before active database overwrite",
            )
        if (
            self._stage_guard is None
            or self._stage_db_path is None
            or self._staged_image_sha256 is None
            or self._staged_image_size is None
            or self._staged_image_sha256.casefold() != expected_sha256.casefold()
            or self._staged_image_size != expected_size
        ):
            raise WindowsMaintenanceError(
                "MIGRATION_STAGE_UNVERIFIED",
                "active promotion requires the exact frozen staged-image proof",
            )
        expected_active = self._recovery_byte_state
        if expected_active is None:
            raise WindowsMaintenanceError(
                "MIGRATION_STAGE_UNVERIFIED",
                "active promotion is missing the original database-family proof",
            )
        self._stage_guard.verify(expected_sha256=expected_sha256, expected_size=expected_size)
        self._require_recovery_byte_state(
            expected_active,
            message="active database family changed before raw staged promotion",
        )
        self._mark_active_sidecars_delete_pending()
        try:
            self._active_overwrite_started = True
            self._overwrite_active_from_path(self._stage_db_path)
            self.flush_file_buffers()
            self.revalidate()
            size, digest = self._hash_retained_handle_bytes(self._db_handle)
            if size != expected_size or digest.casefold() != expected_sha256.casefold():
                raise WindowsMaintenanceError(
                    "MIGRATION_PROMOTION_EXACTNESS_UNPROVEN",
                    "raw-promoted active database does not match the frozen staged image",
                )
            self._runtime_activation_prepared = True
            self._recovery_initial_journal = None
            self._recovery_byte_state = None
            return {"size": size, "sha256": digest}
        except BaseException as exc:
            self.retain_fail_closed()
            if isinstance(exc, WindowsMaintenanceError):
                raise
            raise WindowsMaintenanceError(
                "MIGRATION_PROMOTION_EXACTNESS_UNPROVEN",
                "raw staged promotion failed after active overwrite began",
            ) from exc

    def release_staged_mutation_unmutated(self) -> None:
        """Discard a private stage only when the active database family is still exact."""

        self._require_open()
        if self.phase != self.PHASE_MUTATION or self._active_overwrite_started:
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "unmutated staged release is unavailable after active overwrite begins",
            )
        expected = self._recovery_byte_state
        if expected is None:
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_RELEASE_UNPROVEN",
                "unmutated staged release is missing the active-family byte proof",
            )
        try:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            self._cleanup_recovery_semantic_snapshot()
            self.phase = self.PHASE_CUSTODY
            self._recovery_initial_journal = None
            self._recovery_byte_state = expected
            self._require_recovery_byte_state(
                expected,
                message="active database family changed while failed private mutation stage was released",
            )
            self._recovery_byte_state = None
            self._close_handles_and_sentinel()
        except BaseException:
            if not self._retained:
                self.retain_fail_closed()
            raise

    def release_verified_recovery_guard_unmutated(self) -> None:
        """Release semantic recovery guard only if SQLite close preserves exact source bytes."""

        self._require_open()
        if (
            self.phase != self.PHASE_RECOVERY_GUARD
            or self.connection is None
            or self._recovery_byte_state is None
        ):
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "unmutated recovery release requires the held semantic recovery guard",
            )
        byte_state = self._recovery_byte_state
        try:
            self._require_recovery_byte_state(
                byte_state,
                message="migration recovery source bytes changed while semantic guard was held",
            )
            self.connection.close()
            self.connection = None
            self._cleanup_recovery_semantic_snapshot()
            self.phase = self.PHASE_CUSTODY
            self._recovery_initial_journal = None
            self._recovery_byte_state = None
            self.revalidate()
            self._require_recovery_byte_state(
                byte_state,
                message="migration recovery source bytes changed while semantic guard was released",
            )
        except BaseException as exc:
            self.retain_fail_closed()
            if isinstance(exc, WindowsMaintenanceError):
                raise
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_RELEASE_UNPROVEN",
                "migration recovery semantic guard could not be released without source mutation",
            ) from exc
        self._close_handles_and_sentinel()

    def prepare_verified_exact_restore_release(
        self,
        *,
        recovery_proof_verified: bool,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        """Release an already exact-restored pre-v3 target without opening SQLite."""

        self._require_open()
        if self.phase != self.PHASE_CUSTODY or self.connection is not None:
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "verified exact-restore recovery requires fresh Phase 1 custody",
            )
        if recovery_proof_verified is not True:
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_PROOF_REQUIRED",
                "exact-restore recovery requires verified backup, manifest, and target identity proof",
            )
        self.revalidate()
        if _scan_sidecars(self.db_path):
            raise WindowsMaintenanceError(
                "MIGRATION_ROLLBACK_SIDECAR_UNCERTAIN",
                "verified exact-restore recovery requires WAL/SHM to be absent",
            )
        self._exact_restore_expected_sha256 = expected_sha256
        self._exact_restore_expected_size = expected_size
        self.phase = self.PHASE_EXACT_RESTORED
        self.prepare_exact_restore_release()

    def restore_exact_from_backup_from_custody(
        self,
        backup_path: str | os.PathLike[str],
        *,
        recovery_proof_verified: bool,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        """Retry an already-authorized exact rollback from fresh native custody without SQLite."""

        self._require_open()
        if self.phase != self.PHASE_CUSTODY or self.connection is not None:
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "fresh-custody exact rollback retry requires Phase 1 native custody",
            )
        if recovery_proof_verified is not True:
            raise WindowsMaintenanceError(
                "MIGRATION_RECOVERY_PROOF_REQUIRED",
                "fresh-custody exact rollback retry requires durable rollback and backup proof",
            )
        self.revalidate()

        artifact = WindowsMigrationArtifactCustody.open_existing(backup_path)
        raw_copy_started = False
        try:
            if _same_file(artifact.identity, self.identity):
                raise WindowsMaintenanceError(
                    "MIGRATION_ROLLBACK_BACKUP_INVALID",
                    "migration rollback backup resolves to the active database file",
                )
            artifact.verify(expected_sha256=expected_sha256, expected_size=expected_size)
            self.revalidate()
            self._mark_active_sidecars_delete_pending()
            raw_copy_started = True
            self._active_overwrite_started = True
            self._overwrite_active_from_path(artifact.path)
            self.flush_file_buffers()
            self.revalidate()
            restored_size, restored_sha256 = self._hash_retained_handle_bytes(self._db_handle)
            if restored_size != expected_size or restored_sha256.casefold() != expected_sha256.casefold():
                raise WindowsMaintenanceError(
                    "MIGRATION_ROLLBACK_EXACTNESS_UNPROVEN",
                    "fresh-custody rollback retry did not restore the exact verified backup bytes",
                )
            self._exact_restore_expected_sha256 = expected_sha256
            self._exact_restore_expected_size = expected_size
            self._exact_release_prepared = False
            self.phase = self.PHASE_EXACT_RESTORED
        except BaseException as exc:
            if raw_copy_started:
                self.retain_fail_closed()
                if isinstance(exc, WindowsMaintenanceError):
                    raise
                raise WindowsMaintenanceError(
                    "MIGRATION_ROLLBACK_EXACTNESS_UNPROVEN",
                    "fresh-custody exact rollback retry failed after raw overwrite began",
                ) from exc
            raise
        finally:
            artifact.close()

    def flush_file_buffers(self) -> None:
        self._require_open()
        self.revalidate()
        api = _require_windows()
        try:
            api.win32file.FlushFileBuffers(self._db_handle)
        except Exception as exc:
            if self.phase == self.PHASE_MUTATION:
                self.retain_fail_closed()
            raise WindowsMaintenanceError(
                "MIGRATION_DURABILITY_UNPROVEN",
                "database file buffers could not be flushed during staged migration",
            ) from exc

    def restore_exact_from_backup(
        self,
        backup_path: str | os.PathLike[str],
        *,
        rollback_intent_verified: bool,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        """Restore exact backup bytes in-place while active Win32 custody remains live."""

        self._require_open()
        if self.phase != self.PHASE_MUTATION or self.connection is None:
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "exact migration rollback is available only during the mutation phase",
            )
        if rollback_intent_verified is not True:
            raise WindowsMaintenanceError(
                "MIGRATION_ROLLBACK_INTENT_REQUIRED",
                "durable rollback intent is required before exact active-database overwrite",
            )
        if self.connection.in_transaction:
            raise WindowsMaintenanceError(
                "MIGRATION_ROLLBACK_UNSAFE",
                "exact migration rollback requires no active SQLite transaction",
            )
        journal = self.connection.execute("PRAGMA journal_mode").fetchone()
        synchronous = self.connection.execute("PRAGMA synchronous").fetchone()
        locking = self.connection.execute("PRAGMA locking_mode").fetchone()
        if (
            journal is None
            or str(journal[0]).casefold() != "delete"
            or synchronous is None
            or int(synchronous[0]) != 2
            or locking is None
            or str(locking[0]).casefold() != "exclusive"
        ):
            raise WindowsMaintenanceError(
                "MIGRATION_ROLLBACK_UNSAFE",
                "exact migration rollback requires DELETE/FULL/EXCLUSIVE private-stage custody",
            )
        expected_active = self._recovery_byte_state
        if expected_active is None:
            raise WindowsMaintenanceError(
                "MIGRATION_ROLLBACK_UNSAFE",
                "exact migration rollback is missing the active-family pre-mutation proof",
            )
        self._require_recovery_byte_state(
            expected_active,
            message="active database family changed before exact rollback overwrite",
        )

        backup = _canonical_native_path(backup_path, label="migration rollback backup")
        if _path_key(backup) == _path_key(self.db_path):
            raise WindowsMaintenanceError(
                "MIGRATION_ROLLBACK_BACKUP_INVALID",
                "migration rollback backup must be a distinct file",
            )
        _reject_reparse_components(backup, label="migration rollback backup")
        sid = _current_owner_sid(None)
        backup_parent, backup_parent_handle, backup_parent_identity = _open_parent_guard(
            backup,
            label="migration rollback backup",
            contention_code="MIGRATION_ROLLBACK_BACKUP_INVALID",
            contention_message="migration rollback backup parent cannot be guarded safely",
        )
        backup_handle = None
        raw_copy_started = False
        try:
            try:
                validate_owner_private_path(backup_parent, owner_sid=sid, directory=True)
                validate_owner_private_path(backup, owner_sid=sid, directory=False)
            except WindowsSecurityError as exc:
                raise WindowsMaintenanceError(
                    "MIGRATION_ROLLBACK_BACKUP_INVALID",
                    "migration rollback backup is not protected owner-private state",
                ) from exc
            backup_handle = _open_file(
                backup,
                desired_access=_GENERIC_READ,
                share_mode=_FILE_SHARE_READ,
                creation_disposition=_OPEN_EXISTING,
                contention_code="MIGRATION_ROLLBACK_BACKUP_INVALID",
                contention_message="migration rollback backup is busy",
            )
            backup_identity = _handle_identity(backup_handle, expected_path=backup)
            if _same_file(backup_identity, self.identity):
                raise WindowsMaintenanceError(
                    "MIGRATION_ROLLBACK_BACKUP_INVALID",
                    "migration rollback backup resolves to the active database file",
                )
            _require_parent_guard(
                backup_parent,
                backup_parent_handle,
                backup_parent_identity,
                code="MIGRATION_ROLLBACK_BACKUP_INVALID",
                message="migration rollback backup parent changed during verification",
            )
            backup_size = int(os.path.getsize(backup))
            digest = hashlib.sha256()
            with open(backup, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if backup_size != expected_size or digest.hexdigest().casefold() != expected_sha256.casefold():
                raise WindowsMaintenanceError(
                    "MIGRATION_ROLLBACK_BACKUP_INVALID",
                    "migration rollback backup size or SHA-256 verification failed",
                )
            _require_identity(
                backup,
                backup_identity,
                code="MIGRATION_ROLLBACK_BACKUP_INVALID",
                message="migration rollback backup identity changed during verification",
            )

            self.connection.close()
            self.connection = None
            self._cleanup_recovery_semantic_snapshot()
            self._require_recovery_byte_state(
                expected_active,
                message="active database family changed while rollback stage was released",
            )
            self._mark_active_sidecars_delete_pending()
            raw_copy_started = True
            self._active_overwrite_started = True
            self._overwrite_active_from_path(backup)
            self.flush_file_buffers()
            self.revalidate()
            restored_size, restored_sha256 = self._hash_retained_handle_bytes(self._db_handle)
            if restored_size != expected_size or restored_sha256.casefold() != expected_sha256.casefold():
                raise WindowsMaintenanceError(
                    "MIGRATION_ROLLBACK_EXACTNESS_UNPROVEN",
                    "active database bytes do not exactly match the verified rollback backup",
                )
            self._recovery_initial_journal = None
            self._recovery_byte_state = None
            self._exact_restore_expected_sha256 = expected_sha256
            self._exact_restore_expected_size = expected_size
            self._exact_release_prepared = False
            self.phase = self.PHASE_EXACT_RESTORED
        except BaseException as exc:
            if raw_copy_started:
                self.retain_fail_closed()
                if isinstance(exc, WindowsMaintenanceError):
                    raise
                raise WindowsMaintenanceError(
                    "MIGRATION_ROLLBACK_EXACTNESS_UNPROVEN",
                    "exact in-place migration rollback failed after raw overwrite began",
                ) from exc
            raise
        finally:
            _close_handle(backup_handle)
            _close_handle(backup_parent_handle)

    def _overwrite_active_from_path(self, source_path: str) -> None:
        kernel32 = _require_windows().kernel32
        source_size = int(os.path.getsize(source_path))
        set_pointer = kernel32.SetFilePointerEx
        set_pointer.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
        set_pointer.restype = wintypes.BOOL
        set_end = kernel32.SetEndOfFile
        set_end.argtypes = [wintypes.HANDLE]
        set_end.restype = wintypes.BOOL
        write_file = kernel32.WriteFile
        write_file.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        write_file.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        if not set_pointer(_handle_value(self._db_handle), 0, None, 0):
            raise WindowsMaintenanceError(
                "MIGRATION_ROLLBACK_EXACTNESS_UNPROVEN",
                "active database file position could not be reset for exact rollback",
            )
        bytes_written = 0
        with open(source_path, "rb") as source:
            for chunk in iter(lambda: source.read(64 * 1024), b""):
                buffer = ctypes.create_string_buffer(chunk)
                written = wintypes.DWORD()
                ctypes.set_last_error(0)
                if not write_file(
                    _handle_value(self._db_handle),
                    ctypes.byref(buffer),
                    len(chunk),
                    ctypes.byref(written),
                    None,
                ) or int(written.value) != len(chunk):
                    raise WindowsMaintenanceError(
                        "MIGRATION_ROLLBACK_EXACTNESS_UNPROVEN",
                        "active database raw rollback write did not complete",
                    )
                bytes_written += len(chunk)
                _migration_raw_copy_checkpoint(bytes_written, source_size)
        if not set_end(_handle_value(self._db_handle)):
            raise WindowsMaintenanceError(
                "MIGRATION_ROLLBACK_EXACTNESS_UNPROVEN",
                "active database could not be truncated to the exact rollback size",
            )

    def prepare_runtime_activation(self) -> None:
        self._require_open()
        if self.phase == self.PHASE_EXACT_RESTORED:
            self.prepare_exact_restore_release()
            return
        if (
            self.phase != self.PHASE_MUTATION
            or self.connection is not None
            or not self._runtime_activation_prepared
            or self._staged_image_sha256 is None
            or self._staged_image_size is None
        ):
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "runtime activation requires a fully promoted and verified staged image",
            )
        try:
            self.revalidate()
            self.flush_file_buffers()
            size, digest = self._hash_retained_handle_bytes(self._db_handle)
            if (
                size != self._staged_image_size
                or digest.casefold() != self._staged_image_sha256.casefold()
            ):
                raise WindowsMaintenanceError(
                    "MIGRATION_ACTIVATION_UNPROVEN",
                    "active database bytes changed after staged promotion verification",
                )
        except BaseException as exc:
            self.retain_fail_closed()
            if isinstance(exc, WindowsMaintenanceError):
                raise
            raise WindowsMaintenanceError(
                "MIGRATION_ACTIVATION_UNPROVEN",
                "migration active-image proof could not be completed safely",
            ) from exc

    def prepare_exact_restore_release(self) -> None:
        self._require_open()
        if self.phase != self.PHASE_EXACT_RESTORED:
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "exact-restore release preparation requires exact-restored migration state",
            )
        if self._exact_release_prepared:
            return
        expected_sha256 = self._exact_restore_expected_sha256
        expected_size = self._exact_restore_expected_size
        if expected_sha256 is None or expected_size is None:
            self.retain_fail_closed()
            raise WindowsMaintenanceError(
                "MIGRATION_ROLLBACK_EXACTNESS_UNPROVEN",
                "exact-restore release is missing the expected rollback byte proof",
            )
        try:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            self.revalidate()
            if not self._sidecars_delete_pending and _scan_sidecars(self.db_path):
                raise WindowsMaintenanceError(
                    "MIGRATION_ROLLBACK_SIDECAR_UNCERTAIN",
                    "exact-restore release requires WAL/SHM to remain absent",
                )
            restored_size, restored_sha256 = self._hash_retained_handle_bytes(self._db_handle)
            self.revalidate()
            if not self._sidecars_delete_pending and _scan_sidecars(self.db_path):
                raise WindowsMaintenanceError(
                    "MIGRATION_ROLLBACK_SIDECAR_UNCERTAIN",
                    "exact-restore release observed new WAL/SHM state",
                )
            if (
                restored_size != expected_size
                or restored_sha256.casefold() != expected_sha256.casefold()
            ):
                raise WindowsMaintenanceError(
                    "MIGRATION_ROLLBACK_EXACTNESS_UNPROVEN",
                    "exact-restored database bytes changed before release",
                )
            self._exact_release_prepared = True
        except BaseException as exc:
            self.retain_fail_closed()
            if isinstance(exc, WindowsMaintenanceError):
                raise
            raise WindowsMaintenanceError(
                "MIGRATION_ROLLBACK_EXACTNESS_UNPROVEN",
                "exact-restored database release proof could not be completed safely",
            ) from exc

    def release_unmutated(self) -> None:
        self._require_open()
        if self.phase == self.PHASE_RECOVERY_GUARD:
            self.release_verified_recovery_guard_unmutated()
            return
        if self.phase in {self.PHASE_CUSTODY, self.PHASE_BACKUP_GUARD}:
            self._close_handles_and_sentinel()
            return
        if self.phase == self.PHASE_EXACT_RESTORED:
            raise WindowsMaintenanceError(
                "MIGRATION_PHASE_INVALID",
                "exact rollback is a mutation and cannot use the unmutated release path",
            )
        self.prepare_runtime_activation()
        self.close()

    def retain_fail_closed(self) -> None:
        if self._retained:
            return
        self._retained = True
        self._closed = True
        connection, self.connection = self.connection, None
        db_handle, self._db_handle = self._db_handle, None
        parent_handle, self._parent_handle = self._parent_handle, None
        sentinel, self._sentinel = self._sentinel, None
        stage_guard, self._stage_guard = self._stage_guard, None
        sidecars = list(self._sidecar_handles.values())
        self._sidecar_handles = {}
        with _RETAINED_EXCLUSION_LOCK:
            for retained in (connection, stage_guard, db_handle, parent_handle, sentinel, *sidecars):
                if retained is not None:
                    _RETAINED_EXCLUSION_HANDLES.append(retained)

    def close(self) -> None:
        if self._retained or self._closed:
            return
        if self.phase == self.PHASE_RECOVERY_GUARD:
            self.release_verified_recovery_guard_unmutated()
            return
        if self.phase == self.PHASE_MUTATION and not self._runtime_activation_prepared:
            self.retain_fail_closed()
            raise WindowsMaintenanceError(
                "MIGRATION_ACTIVATION_REQUIRED",
                "staged migration cannot release mutation exclusion before runtime activation is prepared",
            )
        if self.phase == self.PHASE_EXACT_RESTORED and not self._exact_release_prepared:
            self.retain_fail_closed()
            raise WindowsMaintenanceError(
                "MIGRATION_EXACT_RELEASE_REQUIRED",
                "exact-restored migration cannot release custody before exact byte proof is prepared",
            )
        self._close_handles_and_sentinel()

    def _close_handles_and_sentinel(self) -> None:
        if self._closed:
            return
        try:
            connection, self.connection = self.connection, None
            if connection is not None:
                connection.close()
            self._cleanup_recovery_semantic_snapshot()

            sidecars = list(self._sidecar_handles.values())
            self._sidecar_handles = {}
            for handle in sidecars:
                _close_handle(handle)
            if self._sidecars_delete_pending and _scan_sidecars(self.db_path):
                raise WindowsMaintenanceError(
                    "MIGRATION_HOLD_SIDECAR_UNCERTAIN",
                    "delete-pending WAL/SHM sidecars remained after custody release",
                )

            # Main-file custody is the final data-bearing handle released.  All
            # terminal byte proofs above are therefore protected until here.
            db_handle, self._db_handle = self._db_handle, None
            _close_handle(db_handle)
            parent_handle, self._parent_handle = self._parent_handle, None
            _close_handle(parent_handle)
            sentinel, self._sentinel = self._sentinel, None
            if sentinel is not None:
                sentinel.close()
            self._closed = True
        except BaseException:
            if not self._retained:
                self.retain_fail_closed()
            raise

    def _require_open(self) -> None:
        if self._closed:
            raise WindowsMaintenanceError(
                "MIGRATION_LEASE_CLOSED",
                "staged migration lease is already closed",
            )

    def __enter__(self) -> WindowsStagedMigrationLease:
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        if exc_type is not None:
            if self.phase in {self.PHASE_MUTATION, self.PHASE_EXACT_RESTORED}:
                self.retain_fail_closed()
            elif self.phase == self.PHASE_RECOVERY_GUARD:
                self.release_verified_recovery_guard_unmutated()
            else:
                self._close_handles_and_sentinel()
            return
        if self.phase == self.PHASE_RECOVERY_GUARD:
            self.release_verified_recovery_guard_unmutated()
            return
        if self.phase == self.PHASE_MUTATION and not self._runtime_activation_prepared:
            self.prepare_runtime_activation()
        elif self.phase == self.PHASE_EXACT_RESTORED and not self._exact_release_prepared:
            self.prepare_exact_restore_release()
        self.close()


class WindowsMigrationArtifactCustody:
    """Owner-private retained-handle custody for migration backup/manifest artifacts."""

    def __init__(
        self,
        path: str,
        handle: Any,
        identity: WindowsFileIdentity,
        parent_path: str,
        parent_handle: Any,
        parent_identity: WindowsFileIdentity,
        owner_sid: str,
        writable: bool = True,
    ) -> None:
        self.path = path
        self.identity = identity
        self.parent_path = parent_path
        self.parent_identity = parent_identity
        self.owner_sid = owner_sid
        self._handle = handle
        self._parent_handle = parent_handle
        self._writable = writable
        self._closed = False
        self._write_started = False

    @classmethod
    def create(cls, path: str | os.PathLike[str]) -> WindowsMigrationArtifactCustody:
        canonical = _canonical_native_path(path, label="migration artifact")
        _reject_reparse_components(canonical, label="migration artifact", include_leaf=False)
        sid = _current_owner_sid(None)
        parent, parent_handle, parent_identity = _open_parent_guard(
            canonical,
            label="migration artifact",
            contention_code="MIGRATION_ARTIFACT_PATH_UNSAFE",
            contention_message="migration artifact parent cannot be guarded safely",
        )
        handle = None
        try:
            try:
                validate_owner_private_path(parent, owner_sid=sid, directory=True)
            except WindowsSecurityError as exc:
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_PATH_UNSAFE",
                    "migration artifact parent is not protected owner-private state",
                ) from exc
            if _get_file_attributes(_require_windows(), canonical) is not None:
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_EXISTS",
                    "migration artifact destination already exists",
                )
            try:
                handle = _open_relative_open_always(
                    parent_handle,
                    canonical,
                    desired_access=_GENERIC_READ | _GENERIC_WRITE,
                    share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                    owner_sid=sid,
                    contention_code="MIGRATION_ARTIFACT_BUSY",
                    contention_message="migration artifact destination is busy",
                    nt_create_disposition=_FILE_CREATE,
                )
            except WindowsMaintenanceError as exc:
                if _get_file_attributes(_require_windows(), canonical) is not None:
                    raise WindowsMaintenanceError(
                        "MIGRATION_ARTIFACT_EXISTS",
                        "migration artifact destination already exists",
                    ) from exc
                raise
            identity = _handle_identity(handle, expected_path=canonical)
            _require_parent_guard(
                parent,
                parent_handle,
                parent_identity,
                code="MIGRATION_ARTIFACT_IDENTITY_DRIFT",
                message="migration artifact parent changed during creation",
            )
            try:
                validate_owner_private_path(canonical, owner_sid=sid, directory=False)
            except WindowsSecurityError as exc:
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_PATH_UNSAFE",
                    "migration artifact is not protected owner-private state",
                ) from exc
            _require_identity(
                canonical,
                identity,
                code="MIGRATION_ARTIFACT_IDENTITY_DRIFT",
                message="migration artifact path changed during creation",
            )
            artifact = cls(canonical, handle, identity, parent, parent_handle, parent_identity, sid)
            handle = None
            parent_handle = None
            return artifact
        finally:
            _close_handle(handle)
            _close_handle(parent_handle)

    @classmethod
    def open_existing(cls, path: str | os.PathLike[str]) -> WindowsMigrationArtifactCustody:
        canonical = _canonical_native_path(path, label="migration artifact")
        _reject_reparse_components(canonical, label="migration artifact")
        sid = _current_owner_sid(None)
        parent, parent_handle, parent_identity = _open_parent_guard(
            canonical,
            label="migration artifact",
            contention_code="MIGRATION_ARTIFACT_PATH_UNSAFE",
            contention_message="migration artifact parent cannot be guarded safely",
        )
        handle = None
        try:
            try:
                validate_owner_private_path(parent, owner_sid=sid, directory=True)
            except WindowsSecurityError as exc:
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_PATH_UNSAFE",
                    "migration artifact parent is not protected owner-private state",
                ) from exc
            if _get_file_attributes(_require_windows(), canonical) is None:
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_MISSING",
                    "migration artifact does not exist",
                )
            handle = _open_relative_open_always(
                parent_handle,
                canonical,
                desired_access=_GENERIC_READ,
                share_mode=_FILE_SHARE_READ,
                owner_sid=None,
                contention_code="MIGRATION_ARTIFACT_BUSY",
                contention_message="migration artifact is busy",
                nt_create_disposition=_FILE_OPEN,
            )
            identity = _handle_identity(handle, expected_path=canonical)
            _require_parent_guard(
                parent,
                parent_handle,
                parent_identity,
                code="MIGRATION_ARTIFACT_IDENTITY_DRIFT",
                message="migration artifact parent changed during open",
            )
            try:
                validate_owner_private_path(canonical, owner_sid=sid, directory=False)
            except WindowsSecurityError as exc:
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_PATH_UNSAFE",
                    "migration artifact is not protected owner-private state",
                ) from exc
            _require_identity(
                canonical,
                identity,
                code="MIGRATION_ARTIFACT_IDENTITY_DRIFT",
                message="migration artifact path changed during open",
            )
            artifact = cls(
                canonical,
                handle,
                identity,
                parent,
                parent_handle,
                parent_identity,
                sid,
                writable=False,
            )
            handle = None
            parent_handle = None
            return artifact
        finally:
            _close_handle(handle)
            _close_handle(parent_handle)

    @classmethod
    def delete_existing_verified(
        cls,
        path: str | os.PathLike[str],
        *,
        expected_sha256: str,
        expected_size: int,
        expected_volume_serial_number: int,
        expected_file_id: str,
    ) -> None:
        """Delete the exact verified artifact by retained handle, never by replaceable pathname."""

        canonical = _canonical_native_path(path, label="migration artifact")
        _reject_reparse_components(canonical, label="migration artifact")
        sid = _current_owner_sid(None)
        parent, parent_handle, parent_identity = _open_parent_guard(
            canonical,
            label="migration artifact",
            contention_code="MIGRATION_ARTIFACT_PATH_UNSAFE",
            contention_message="migration artifact parent cannot be guarded safely",
        )
        handle = None
        read_handle = None
        try:
            try:
                validate_owner_private_path(parent, owner_sid=sid, directory=True)
                validate_owner_private_path(canonical, owner_sid=sid, directory=False)
            except WindowsSecurityError as exc:
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_PATH_UNSAFE",
                    "migration artifact is not protected owner-private state",
                ) from exc
            handle = _open_relative_open_always(
                parent_handle,
                canonical,
                desired_access=_GENERIC_READ | _DELETE,
                share_mode=_FILE_SHARE_READ,
                owner_sid=None,
                contention_code="MIGRATION_ARTIFACT_BUSY",
                contention_message="migration artifact cannot be consumed while another handle is active",
                nt_create_disposition=_FILE_OPEN,
            )
            identity = _handle_identity(handle, expected_path=canonical)
            if (
                int(identity.volume_serial_number) != int(expected_volume_serial_number)
                or str(identity.file_id_hex).casefold() != str(expected_file_id).casefold()
            ):
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_IDENTITY_DRIFT",
                    "migration artifact identity changed before consumption",
                )
            read_handle = _open_file(
                canonical,
                desired_access=_GENERIC_READ,
                share_mode=_FILE_SHARE_READ | _FILE_SHARE_DELETE,
                creation_disposition=_OPEN_EXISTING,
                contention_code="MIGRATION_ARTIFACT_BUSY",
                contention_message="migration artifact could not be re-read under delete custody",
            )
            read_identity = _handle_identity(read_handle, expected_path=canonical)
            if not _same_file(read_identity, identity):
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_IDENTITY_DRIFT",
                    "migration artifact read verification resolved to a different file",
                )
            size, digest = WindowsStagedMigrationLease._hash_retained_handle_bytes(read_handle)
            if size != expected_size or digest.casefold() != expected_sha256.casefold():
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_VERIFICATION_FAILED",
                    "migration artifact bytes changed before consumption",
                )
            _close_handle(read_handle)
            read_handle = None
            _require_parent_guard(
                parent,
                parent_handle,
                parent_identity,
                code="MIGRATION_ARTIFACT_IDENTITY_DRIFT",
                message="migration artifact parent changed before consumption",
            )
            disposition = _FILE_DISPOSITION_INFO(wintypes.BOOL(1))
            set_info = _require_windows().kernel32.SetFileInformationByHandle
            set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
            set_info.restype = wintypes.BOOL
            ctypes.set_last_error(0)
            if not set_info(
                _handle_value(handle),
                _FILE_DISPOSITION_INFO_CLASS,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_CONSUME_FAILED",
                    "migration artifact could not be marked consumed safely",
                )
        finally:
            _close_handle(read_handle)
            _close_handle(handle)
            _close_handle(parent_handle)
        if _get_file_attributes(_require_windows(), canonical) is not None:
            raise WindowsMaintenanceError(
                "MIGRATION_ARTIFACT_CONSUME_FAILED",
                "migration artifact remained present after verified consumption",
            )

    def write_bytes(self, data: bytes) -> None:
        self._require_open()
        if not self._writable:
            raise WindowsMaintenanceError(
                "MIGRATION_ARTIFACT_WRITE_INVALID",
                "existing migration artifact custody is read-only",
            )
        if self._write_started:
            raise WindowsMaintenanceError(
                "MIGRATION_ARTIFACT_WRITE_INVALID",
                "migration artifact retained-handle write is single-shot",
            )
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        self.revalidate()
        try:
            with open(self.path, "r+b", buffering=0) as stream:
                written = stream.write(data)
                if written != len(data):
                    raise WindowsMaintenanceError(
                        "MIGRATION_ARTIFACT_WRITE_FAILED",
                        "migration artifact could not be written completely",
                    )
                stream.truncate(len(data))
                stream.flush()
                os.fsync(stream.fileno())
        except WindowsMaintenanceError:
            raise
        except OSError as exc:
            raise WindowsMaintenanceError(
                "MIGRATION_ARTIFACT_WRITE_FAILED",
                "migration artifact could not be written completely",
            ) from exc
        self._write_started = True
        self.flush_file_buffers()
        self.revalidate()

    def read_exact(self) -> dict[str, Any]:
        self._require_open()
        self.revalidate()
        read_handle = _open_file(
            self.path,
            desired_access=_GENERIC_READ,
            share_mode=_FILE_SHARE_READ,
            creation_disposition=_OPEN_EXISTING,
            contention_code="MIGRATION_ARTIFACT_BUSY",
            contention_message="migration artifact could not be opened for exact read",
        )
        try:
            read_identity = _handle_identity(read_handle, expected_path=self.path)
            if not _same_file(read_identity, self.identity):
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_IDENTITY_DRIFT",
                    "migration artifact exact-read handle resolved to a different file",
                )
            kernel32 = _require_windows().kernel32
            get_size = kernel32.GetFileSizeEx
            get_size.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
            get_size.restype = wintypes.BOOL
            read_file = kernel32.ReadFile
            read_file.argtypes = [
                wintypes.HANDLE,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID,
            ]
            read_file.restype = wintypes.BOOL

            size_value = ctypes.c_longlong()
            ctypes.set_last_error(0)
            if not get_size(_handle_value(read_handle), ctypes.byref(size_value)) or size_value.value < 0:
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_READ_FAILED",
                    "migration artifact exact-read size could not be read",
                )
            size = int(size_value.value)

            remaining = size
            chunks: list[bytes] = []
            while remaining:
                chunk_size = min(1024 * 1024, remaining)
                buffer = ctypes.create_string_buffer(chunk_size)
                read = wintypes.DWORD()
                ctypes.set_last_error(0)
                if not read_file(
                    _handle_value(read_handle),
                    ctypes.byref(buffer),
                    chunk_size,
                    ctypes.byref(read),
                    None,
                ):
                    raise WindowsMaintenanceError(
                        "MIGRATION_ARTIFACT_READ_FAILED",
                        "migration artifact exact bytes could not be read",
                    )
                read_count = int(read.value)
                if read_count <= 0 or read_count > chunk_size:
                    raise WindowsMaintenanceError(
                        "MIGRATION_ARTIFACT_READ_FAILED",
                        "migration artifact exact read ended before the expected size",
                    )
                chunks.append(buffer.raw[:read_count])
                remaining -= read_count

            data = b"".join(chunks)
            size_after = ctypes.c_longlong()
            ctypes.set_last_error(0)
            if not get_size(_handle_value(read_handle), ctypes.byref(size_after)) or int(size_after.value) != size:
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_IDENTITY_DRIFT",
                    "migration artifact exact-read size changed while custody was held",
                )
            self.revalidate()
            digest = hashlib.sha256(data).hexdigest()
            return {
                "path": self.path,
                "data": data,
                "size": size,
                "sha256": digest,
                "file_id": self.identity.file_id_hex,
                "volume_serial_number": self.identity.volume_serial_number,
            }
        finally:
            _close_handle(read_handle)

    def flush_file_buffers(self) -> None:
        self._require_open()
        self.revalidate()
        if not self._writable:
            return
        try:
            _require_windows().win32file.FlushFileBuffers(self._handle)
        except Exception as exc:
            raise WindowsMaintenanceError(
                "MIGRATION_ARTIFACT_DURABILITY_UNPROVEN",
                "migration artifact file buffers could not be flushed",
            ) from exc

    def revalidate(self) -> WindowsFileIdentity:
        self._require_open()
        _require_parent_guard(
            self.parent_path,
            self._parent_handle,
            self.parent_identity,
            code="MIGRATION_ARTIFACT_IDENTITY_DRIFT",
            message="migration artifact parent changed while custody was held",
        )
        current = _handle_identity(self._handle, expected_path=self.path)
        if not _same_file(current, self.identity):
            raise WindowsMaintenanceError(
                "MIGRATION_ARTIFACT_IDENTITY_DRIFT",
                "migration artifact retained identity changed",
            )
        try:
            validate_owner_private_path(self.path, owner_sid=self.owner_sid, directory=False)
        except WindowsSecurityError as exc:
            raise WindowsMaintenanceError(
                "MIGRATION_ARTIFACT_PATH_UNSAFE",
                "migration artifact lost owner-private custody",
            ) from exc
        _require_identity(
            self.path,
            self.identity,
            code="MIGRATION_ARTIFACT_IDENTITY_DRIFT",
            message="migration artifact path no longer names the retained file",
        )
        return current

    def verify(self, *, expected_sha256: str, expected_size: int) -> dict[str, Any]:
        self.flush_file_buffers()
        self.revalidate()
        reopened = _open_file(
            self.path,
            desired_access=_GENERIC_READ,
            share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
            creation_disposition=_OPEN_EXISTING,
            contention_code="MIGRATION_ARTIFACT_BUSY",
            contention_message="migration artifact could not be reopened for verification",
        )
        try:
            reopened_identity = _handle_identity(reopened, expected_path=self.path)
            if not _same_file(reopened_identity, self.identity):
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_IDENTITY_DRIFT",
                    "migration artifact reopened as a different file",
                )
            try:
                validate_owner_private_path(self.path, owner_sid=self.owner_sid, directory=False)
            except WindowsSecurityError as exc:
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_PATH_UNSAFE",
                    "migration artifact is not owner-private during verification",
                ) from exc
            size = int(os.path.getsize(self.path))
            digest = hashlib.sha256()
            with open(self.path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual_sha256 = digest.hexdigest()
            if size != expected_size or actual_sha256.casefold() != expected_sha256.casefold():
                raise WindowsMaintenanceError(
                    "MIGRATION_ARTIFACT_VERIFICATION_FAILED",
                    "migration artifact size or SHA-256 verification failed",
                )
            self.revalidate()
            return {
                "path": self.path,
                "size": size,
                "sha256": actual_sha256,
                "file_id": self.identity.file_id_hex,
                "volume_serial_number": self.identity.volume_serial_number,
            }
        finally:
            _close_handle(reopened)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle, self._handle = self._handle, None
        parent_handle, self._parent_handle = self._parent_handle, None
        _close_handle(handle)
        _close_handle(parent_handle)

    def _require_open(self) -> None:
        if self._closed:
            raise WindowsMaintenanceError(
                "MIGRATION_ARTIFACT_CLOSED",
                "migration artifact custody is already closed",
            )

    def __enter__(self) -> WindowsMigrationArtifactCustody:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


class WindowsExclusiveMaintenanceLease:
    """Exclusive main/WAL/SHM exclusion lease with native handle-based promotion."""

    def __init__(
        self,
        db_path: str,
        main_handle: Any,
        main_identity: WindowsFileIdentity,
        main_oplock: _WindowsBatchOplock,
        sidecars: dict[str, tuple[Any, WindowsFileIdentity]],
    ) -> None:
        self.db_path = db_path
        self.main_identity = main_identity
        self.sidecar_identities = {path: identity for path, (_handle, identity) in sidecars.items()}
        self._main_handle = main_handle
        self._main_oplock: _WindowsBatchOplock | None = main_oplock
        self._sidecar_handles = {path: handle for path, (handle, _identity) in sidecars.items()}
        self._retired_handles: list[Any] = []
        self._closed = False
        self._retained = False
        self.last_promotion_result: WindowsPromotionResult | None = None

    @classmethod
    def acquire(cls, db_path: str | os.PathLike[str]) -> WindowsExclusiveMaintenanceLease:
        canonical = _canonical_native_path(db_path, label="database")
        _reject_reparse_components(canonical, label="database")
        api = _require_windows()
        attributes = _get_file_attributes(api, canonical)
        if attributes is None or attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise WindowsMaintenanceError("MAINTENANCE_HOLD_DATABASE_UNAVAILABLE", "database file is unavailable for exclusive maintenance")
        initial_sidecars = _scan_sidecars(canonical)
        main_handle = None
        main_oplock = None
        sidecars: dict[str, tuple[Any, WindowsFileIdentity]] = {}
        try:
            main_handle = _open_file(
                canonical,
                desired_access=_GENERIC_READ,
                share_mode=_FILE_SHARE_DELETE,
                creation_disposition=_OPEN_EXISTING,
                contention_code="MAINTENANCE_HOLD_CONTENTION",
                contention_message="database is still held by a runtime or another process",
                flags_and_attributes=_FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_OVERLAPPED,
            )
            main_oplock = _WindowsBatchOplock.acquire(main_handle)
            main_identity = _handle_identity(main_handle, expected_path=canonical)
            main_oplock.require_unbroken()

            after_main = _scan_sidecars(canonical)
            if after_main != initial_sidecars:
                raise WindowsMaintenanceError("MAINTENANCE_HOLD_SIDECAR_UNCERTAIN", "database sidecar set changed during exclusive acquisition")
            for sidecar_path in after_main:
                handle = _open_file(
                    sidecar_path,
                    desired_access=_GENERIC_READ,
                    share_mode=0,
                    creation_disposition=_OPEN_EXISTING,
                    contention_code="MAINTENANCE_HOLD_CONTENTION",
                    contention_message="database WAL/SHM sidecar is still held by another process",
                )
                try:
                    identity = _handle_identity(handle, expected_path=sidecar_path)
                    _require_identity(
                        sidecar_path,
                        identity,
                        code="MAINTENANCE_HOLD_IDENTITY_DRIFT",
                        message="database sidecar path changed during exclusive acquisition",
                    )
                except BaseException:
                    _close_handle(handle)
                    raise
                sidecars[sidecar_path] = (handle, identity)

            final_sidecars = _scan_sidecars(canonical)
            if final_sidecars != after_main:
                raise WindowsMaintenanceError("MAINTENANCE_HOLD_SIDECAR_UNCERTAIN", "database sidecar set was unstable during exclusive acquisition")
            for sidecar_path, (_handle, identity) in sidecars.items():
                _require_identity(
                    sidecar_path,
                    identity,
                    code="MAINTENANCE_HOLD_IDENTITY_DRIFT",
                    message="database sidecar identity drifted during exclusive acquisition",
                )
            current_main = _handle_identity(main_handle, expected_path=canonical)
            if not _same_file(current_main, main_identity):
                raise WindowsMaintenanceError(
                    "MAINTENANCE_HOLD_IDENTITY_DRIFT",
                    "database identity drifted during exclusive acquisition",
                )
            main_oplock.require_unbroken()
            return cls(canonical, main_handle, main_identity, main_oplock, sidecars)
        except BaseException:
            for handle, _identity in sidecars.values():
                _close_handle(handle)
            _close_handle(main_handle)
            if main_oplock is not None:
                main_oplock.close()
            raise

    def revalidate(self) -> None:
        self._require_open()
        if self._main_oplock is not None:
            self._main_oplock.require_unbroken()
            current_main = _handle_identity(self._main_handle, expected_path=self.db_path)
            if not _same_file(current_main, self.main_identity):
                raise WindowsMaintenanceError(
                    "MAINTENANCE_HOLD_IDENTITY_DRIFT",
                    "database path no longer names the retained maintenance file",
                )
            self._main_oplock.require_unbroken()
        else:
            _require_identity(
                self.db_path,
                self.main_identity,
                code="MAINTENANCE_HOLD_IDENTITY_DRIFT",
                message="database path no longer names the retained maintenance file",
            )
        current_sidecars = _scan_sidecars(self.db_path)
        if tuple(self._sidecar_handles) != current_sidecars:
            raise WindowsMaintenanceError("MAINTENANCE_HOLD_SIDECAR_UNCERTAIN", "database sidecar set changed while maintenance exclusion was held")
        for path, identity in self.sidecar_identities.items():
            _require_identity(
                path,
                identity,
                code="MAINTENANCE_HOLD_IDENTITY_DRIFT",
                message="database sidecar path no longer names the retained file",
            )

    def promote_replacement(self, replacement_path: str | os.PathLike[str]) -> WindowsPromotionResult:
        raise WindowsMaintenanceError(
            "PROMOTION_UNSUPPORTED",
            "same-path Windows database promotion is disabled; use in-place maintenance instead",
        )

        self._require_open()
        self.revalidate()
        source_path = _canonical_native_path(replacement_path, label="replacement database")
        if _path_key(source_path) == _path_key(self.db_path):
            raise WindowsMaintenanceError("PROMOTION_SOURCE_INVALID", "replacement database must be a distinct file")
        _reject_reparse_components(source_path, label="replacement database")
        api = _require_windows()
        attributes = _get_file_attributes(api, source_path)
        if attributes is None or attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise WindowsMaintenanceError("PROMOTION_SOURCE_INVALID", "replacement database is unavailable")

        source_handle = _open_file(
            source_path,
            desired_access=_GENERIC_READ | _GENERIC_WRITE | _DELETE,
            share_mode=0,
            creation_disposition=_OPEN_EXISTING,
            contention_code="MAINTENANCE_HOLD_CONTENTION",
            contention_message="replacement database is still held by another process",
        )
        rename_started = False
        try:
            source_identity = _handle_identity(source_handle, expected_path=source_path)
            _require_identity(
                source_path,
                source_identity,
                code="PROMOTION_SOURCE_IDENTITY_DRIFT",
                message="replacement database path changed before promotion",
            )
            if (
                source_identity.volume_serial_number == self.main_identity.volume_serial_number
                and source_identity.file_id == self.main_identity.file_id
            ):
                raise WindowsMaintenanceError("PROMOTION_SOURCE_INVALID", "replacement database must have a distinct file identity")

            try:
                api.win32file.FlushFileBuffers(source_handle)
            except Exception as exc:
                durability = WindowsPromotionDurability(False, False, False, "PRE_RENAME_FLUSH_FAILED")
                raise WindowsMaintenanceError(
                    "PROMOTION_DURABILITY_UNPROVEN",
                    "replacement database could not be flushed before promotion",
                    durability=durability,
                ) from exc

            self.revalidate()
            oplock = self._main_oplock
            if oplock is None:
                raise WindowsMaintenanceError(
                    "MAINTENANCE_HOLD_OPLOCK_UNAVAILABLE",
                    "database batch oplock custody is unavailable before promotion",
                )
            oplock.require_unbroken()

            rename_done = threading.Event()
            rename_errors: list[BaseException] = []

            def run_rename() -> None:
                try:
                    self._rename_handle_replace(source_handle, self.db_path)
                except BaseException as exc:
                    rename_errors.append(exc)
                finally:
                    rename_done.set()

            rename_thread = threading.Thread(target=run_rename, name="factlane-windows-promotion", daemon=True)
            rename_started = True
            rename_thread.start()

            break_seen = False
            for _ in range(100):
                if oplock.wait_for_break(50):
                    break_seen = True
                    break
                if rename_done.is_set():
                    break
            if not break_seen:
                if rename_done.is_set() and rename_errors:
                    raise rename_errors[0]
                if source_handle is not None:
                    with _RETAINED_EXCLUSION_LOCK:
                        _RETAINED_EXCLUSION_HANDLES.append(source_handle)
                    source_handle = None
                self.retain_fail_closed()
                raise WindowsMaintenanceError(
                    "PROMOTION_FAILED",
                    "database replacement did not reach a controlled oplock break",
                )

            break_info = oplock.consume_break()
            if (
                break_info.original_level != _OPLOCK_LEVEL_BATCH_EQUIVALENT
                or break_info.new_level != _OPLOCK_LEVEL_EXPECTED_RENAME_BREAK
                or not break_info.acknowledgment_required
                or break_info.modes_provided
            ):
                if source_handle is not None:
                    with _RETAINED_EXCLUSION_LOCK:
                        _RETAINED_EXCLUSION_HANDLES.append(source_handle)
                    source_handle = None
                self.retain_fail_closed()
                raise WindowsMaintenanceError(
                    "MAINTENANCE_HOLD_CONTENTION",
                    "database oplock break was not the controlled replacement operation",
                )
            oplock.acknowledge_to_none()
            rename_thread.join()
            if rename_errors:
                raise rename_errors[0]

            old_identity = self.main_identity
            old_handle = self._main_handle
            old_oplock = self._main_oplock
            self._retired_handles.append(old_handle)
            self._main_handle = source_handle
            self._main_oplock = None
            source_handle = None
            if old_oplock is not None:
                old_oplock.close()

            try:
                new_identity = _handle_identity(self._main_handle, expected_path=self.db_path)
                _require_identity(
                    self.db_path,
                    new_identity,
                    code="PROMOTION_IDENTITY_UNPROVEN",
                    message="promoted database name does not bind to the replacement file identity",
                )
                if (
                    new_identity.volume_serial_number != source_identity.volume_serial_number
                    or new_identity.file_id != source_identity.file_id
                ):
                    raise WindowsMaintenanceError("PROMOTION_IDENTITY_UNPROVEN", "promoted database file identity does not match the replacement")
                self.main_identity = new_identity
            except BaseException as exc:
                self.retain_fail_closed()
                if isinstance(exc, WindowsMaintenanceError):
                    raise
                raise WindowsMaintenanceError("PROMOTION_IDENTITY_UNPROVEN", "promoted database identity could not be proven") from exc

            post_flushed = True
            post_error: BaseException | None = None
            try:
                api.win32file.FlushFileBuffers(self._main_handle)
            except Exception as exc:
                post_flushed = False
                post_error = exc
            durability = WindowsPromotionDurability(
                pre_rename_file_flushed=True,
                post_rename_file_flushed=post_flushed,
                directory_durability_proven=False,
                error_code=None if post_flushed else "POST_RENAME_FLUSH_FAILED",
            )
            result = WindowsPromotionResult(old_identity, self.main_identity, durability)
            self.last_promotion_result = result
            if post_error is not None:
                self.retain_fail_closed()
                raise WindowsMaintenanceError(
                    "PROMOTION_DURABILITY_UNPROVEN",
                    "database replacement was renamed but the promoted file could not be flushed afterward",
                    durability=durability,
                ) from post_error
            return result
        except BaseException:
            if rename_started and not self._retained:
                self.retain_fail_closed()
            raise
        finally:
            _close_handle(source_handle)

    @staticmethod
    def _rename_handle_replace(source_handle: Any, target_path: str) -> None:
        api = _require_windows()
        encoded = target_path.encode("utf-16-le")
        filename_offset = _FILE_RENAME_INFO_EX_HEADER.FileNameLength.offset + ctypes.sizeof(wintypes.DWORD)
        buffer = ctypes.create_string_buffer(filename_offset + len(encoded) + ctypes.sizeof(wintypes.WCHAR))
        header = _FILE_RENAME_INFO_EX_HEADER.from_buffer(buffer)
        header.Flags = _FILE_RENAME_FLAG_REPLACE_IF_EXISTS | _FILE_RENAME_FLAG_POSIX_SEMANTICS
        header.RootDirectory = None
        header.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + filename_offset, encoded, len(encoded))

        set_info = api.kernel32.SetFileInformationByHandle
        set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        set_info.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        if set_info(
            _handle_value(source_handle),
            _FILE_RENAME_INFO_EX_CLASS,
            ctypes.byref(buffer),
            filename_offset + len(encoded),
        ):
            return
        error = ctypes.get_last_error()
        if error in {_ERROR_NOT_SUPPORTED, _ERROR_INVALID_PARAMETER, _ERROR_CALL_NOT_IMPLEMENTED}:
            raise WindowsMaintenanceError(
                "PROMOTION_UNSUPPORTED",
                "Windows FileRenameInfoEx POSIX replacement is unavailable",
            )
        if error in {_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION}:
            raise WindowsMaintenanceError(
                "MAINTENANCE_HOLD_CONTENTION",
                "database promotion is blocked by another open handle",
            )
        raise WindowsMaintenanceError("PROMOTION_FAILED", "Windows database replacement could not be promoted atomically")

    def retain_fail_closed(self) -> None:
        if self._retained:
            return
        handles: list[Any] = []
        if self._main_handle is not None:
            handles.append(self._main_handle)
            self._main_handle = None
        if self._main_oplock is not None:
            handles.append(self._main_oplock)
            self._main_oplock = None
        handles.extend(self._sidecar_handles.values())
        self._sidecar_handles = {}
        handles.extend(self._retired_handles)
        self._retired_handles = []
        with _RETAINED_EXCLUSION_LOCK:
            _RETAINED_EXCLUSION_HANDLES.extend(handles)
        self._retained = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._retained:
            return
        main_handle = self._main_handle
        main_oplock = self._main_oplock
        handles: Iterable[Any] = list(self._sidecar_handles.values()) + list(self._retired_handles)
        self._main_handle = None
        self._main_oplock = None
        self._sidecar_handles = {}
        self._retired_handles = []
        _close_handle(main_handle)
        if main_oplock is not None:
            main_oplock.close()
        for handle in handles:
            _close_handle(handle)

    def _require_open(self) -> None:
        if self._closed:
            raise WindowsMaintenanceError("MAINTENANCE_LEASE_CLOSED", "exclusive maintenance lease is already closed")
        if self._retained:
            raise WindowsMaintenanceError("MAINTENANCE_LEASE_RETAINED", "exclusive maintenance lease was retained fail-closed")

    def __enter__(self) -> WindowsExclusiveMaintenanceLease:
        self._require_open()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


__all__ = [
    "WindowsBarrierGuard",
    "WindowsExclusiveMaintenanceLease",
    "WindowsFileIdentity",
    "WindowsInPlaceMaintenanceLease",
    "WindowsMigrationArtifactCustody",
    "WindowsMaintenanceError",
    "WindowsPromotionDurability",
    "WindowsPromotionResult",
    "WindowsRuntimeDBLease",
    "WindowsStagedMigrationLease",
]
