from __future__ import annotations

import ctypes
import hashlib
import json
import ntpath
import os
import re
import secrets
import ssl
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .windows_security import (
    WindowsSecurityError,
    current_user_sid,
    owner_only_security_attributes,
    validate_owner_only_kernel_object,
    validate_owner_private_path,
    validate_windows_path_no_reparse,
)


_HOST_RE = re.compile(r"factlane-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.test")
_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MANIFEST_NAME = "manifest.json"
_SCHEMA_VERSION = 1
_ROOT_VALIDITY = timedelta(days=730)
_LEAF_VALIDITY = timedelta(days=90)
_ROOT_RENEW_BEFORE = timedelta(days=60)
_LEAF_RENEW_BEFORE = timedelta(days=14)
_NOT_BEFORE_SKEW = timedelta(minutes=5)
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_OBJ_CASE_INSENSITIVE = 0x00000040
_FILE_CREATE = 2
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_SDDL_REVISION_1 = 1
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_RENAME_INFORMATION_CLASS = 10
_FILE_ID_INFO_CLASS = 18


class WindowsTLSError(RuntimeError):
    """Fail-closed native-Windows TLS issuance or custody failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(frozen=True)
class WindowsTLSMaterial:
    root_dir: Path
    hostname: str
    manifest_path: Path
    root_key_path: Path
    root_cert_path: Path
    leaf_key_path: Path
    leaf_cert_path: Path
    root_fingerprint_sha256: str
    leaf_fingerprint_sha256: str
    root_not_after_utc: datetime
    leaf_not_after_utc: datetime


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
    _fields_ = [("Status", wintypes.LONG), ("Pointer", wintypes.LPVOID)]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("u", _IO_STATUS_BLOCK_STATUS), ("Information", ctypes.c_size_t)]


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong), ("FileId", _FILE_ID_128)]


class _FILE_RENAME_INFORMATION_HEADER(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
    ]


class _NativeHandle:
    def __init__(self, value: int) -> None:
        self._value = value

    def __int__(self) -> int:
        return self._value

    def Close(self) -> None:
        if self._value == 0:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        value, self._value = self._value, 0
        close_handle(wintypes.HANDLE(value))


@dataclass(frozen=True)
class _LoadedMaterial:
    public: WindowsTLSMaterial
    manifest: dict[str, Any]
    root_key: Any
    root_cert: Any
    leaf_key: Any
    leaf_cert: Any


@dataclass(frozen=True)
class _WindowsTLSFileIdentity:
    volume_serial_number: int
    file_id: bytes
    final_path: str


def _raise(code: str, message: str, exc: BaseException | None = None) -> None:
    error = WindowsTLSError(code, message)
    if exc is None:
        raise error
    raise error from exc


def _require_windows() -> tuple[Any, Any]:
    if os.name != "nt":
        _raise("WINDOWS_TLS_UNAVAILABLE", "Windows TLS custody is unavailable on this platform")
    try:
        import win32con
        import win32file
    except ImportError as exc:  # pragma: no cover - native-Windows packaging guard
        _raise("WINDOWS_TLS_UNAVAILABLE", "Windows TLS custody requires pywin32", exc)
    return win32con, win32file


def _crypto() -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    _require_windows()
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as exc:  # pragma: no cover - native-Windows packaging guard
        _raise("WINDOWS_TLS_UNAVAILABLE", "Windows TLS custody requires cryptography 50.0.1", exc)
    return x509, hashes, serialization, padding, rsa, ExtendedKeyUsageOID, NameOID


def _validate_hostname(hostname: str) -> str:
    if not isinstance(hostname, str) or _HOST_RE.fullmatch(hostname) is None or len(hostname) > 253:
        raise ValueError("hostname must be one canonical factlane-<id>.test name")
    return hostname


def _utc_now(now: datetime | None) -> datetime:
    value = datetime.now(timezone.utc) if now is None else now
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _path_guard(path: Path) -> Path:
    try:
        return validate_windows_path_no_reparse(path)
    except WindowsSecurityError as exc:
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS path contains unsafe custody state", exc)


def _ensure_private_directory(path: Path, owner_sid: str) -> None:
    _path_guard(path)
    if path.exists():
        try:
            validate_owner_private_path(path, owner_sid=owner_sid, directory=True)
        except WindowsSecurityError as exc:
            _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS state directory is not owner-private", exc)
        return
    parent = path.parent
    if not parent.exists():
        _ensure_private_directory(parent, owner_sid)
    _path_guard(parent)
    win32con, win32file = _require_windows()
    parent_handle = None
    try:
        parent_handle = win32file.CreateFile(
            str(parent),
            0,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
            None,
            win32con.OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        _path_guard(parent)
        try:
            win32file.CreateDirectory(str(path), owner_only_security_attributes(owner_sid))
        except Exception as exc:
            if getattr(exc, "winerror", None) not in {80, 183}:
                _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS state directory could not be created privately", exc)
        _path_guard(path)
        try:
            validate_owner_private_path(path, owner_sid=owner_sid, directory=True)
        except WindowsSecurityError as exc:
            _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS state directory is not owner-private", exc)
    finally:
        if parent_handle is not None:
            parent_handle.Close()


class _TLSDirectoryGuard:
    def __init__(self, root: Path, owner_sid: str, handle: Any) -> None:
        self.root = root
        self.owner_sid = owner_sid
        self.handle = handle

    @classmethod
    def open(cls, root: Path, owner_sid: str, *, create: bool) -> _TLSDirectoryGuard:
        if create:
            _ensure_private_directory(root, owner_sid)
        else:
            _path_guard(root)
            if not root.exists():
                _raise("WINDOWS_TLS_MANIFEST_MISSING", "Windows TLS state does not exist")
        win32con, win32file = _require_windows()
        handle = None
        try:
            handle = win32file.CreateFile(
                str(root),
                0,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                None,
                win32con.OPEN_EXISTING,
                _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
            guard = cls(root, owner_sid, handle)
            guard.revalidate()
            handle = None
            return guard
        except WindowsTLSError:
            raise
        except Exception as exc:
            _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS state directory could not be pinned safely", exc)
        finally:
            if handle is not None:
                handle.Close()

    def revalidate(self) -> None:
        _path_guard(self.root)
        try:
            raw_handle = int(self.handle)
            if raw_handle in {0, -1}:
                raise ValueError("invalid handle")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            get_final = kernel32.GetFinalPathNameByHandleW
            get_final.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
            get_final.restype = ctypes.c_uint32
            needed = int(get_final(ctypes.c_void_p(raw_handle), None, 0, 0))
            if needed <= 0 or needed > 32768:
                raise OSError("final path unavailable")
            buffer = ctypes.create_unicode_buffer(needed + 1)
            written = int(get_final(ctypes.c_void_p(raw_handle), buffer, len(buffer), 0))
            if written <= 0 or written >= len(buffer):
                raise OSError("final path unreadable")
            final_path = buffer.value
            if final_path.startswith("\\\\?\\UNC\\"):
                final_path = "\\\\" + final_path[8:]
            elif final_path.startswith("\\\\?\\"):
                final_path = final_path[4:]
            if ntpath.normcase(ntpath.abspath(final_path)) != ntpath.normcase(ntpath.abspath(str(self.root))):
                _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS state directory identity changed")
        except WindowsTLSError:
            raise
        except Exception as exc:
            _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS state directory identity could not be verified", exc)
        try:
            validate_owner_private_path(self.root, owner_sid=self.owner_sid, directory=True)
        except WindowsSecurityError as exc:
            _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS state directory is not owner-private", exc)

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            handle.Close()

    def __enter__(self) -> _TLSDirectoryGuard:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


def _new_generation_token() -> str:
    return secrets.token_hex(16)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _handle_value(handle: Any) -> wintypes.HANDLE:
    try:
        value = int(handle)
    except Exception as exc:
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS file handle is invalid", exc)
    if value in {0, -1}:
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS file handle is invalid")
    return wintypes.HANDLE(value)


def _strip_extended_prefix(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _handle_identity(handle: Any, *, expected_path: Path | None = None) -> _WindowsTLSFileIdentity:
    raw_handle = _handle_value(handle)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_info.restype = wintypes.BOOL

    tag = _FILE_ATTRIBUTE_TAG_INFO()
    if not get_info(raw_handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(tag), ctypes.sizeof(tag)):
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS file attributes could not be verified by handle")
    attributes = int(tag.FileAttributes)
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT or attributes & _FILE_ATTRIBUTE_DIRECTORY:
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS file handle does not refer to a regular non-reparse file")

    file_id = _FILE_ID_INFO()
    if not get_info(raw_handle, _FILE_ID_INFO_CLASS, ctypes.byref(file_id), ctypes.sizeof(file_id)):
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS file identity could not be verified by handle")

    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final.restype = wintypes.DWORD
    needed = int(get_final(raw_handle, None, 0, 0))
    if needed <= 0 or needed > 32768:
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS file final path could not be determined")
    buffer = ctypes.create_unicode_buffer(needed + 1)
    written = int(get_final(raw_handle, buffer, len(buffer), 0))
    if written <= 0 or written >= len(buffer):
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS file final path could not be read")
    final_path = _strip_extended_prefix(buffer.value)
    if expected_path is not None and ntpath.normcase(ntpath.abspath(final_path)) != ntpath.normcase(
        ntpath.abspath(str(expected_path))
    ):
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS file handle does not map to the expected destination")
    return _WindowsTLSFileIdentity(
        volume_serial_number=int(file_id.VolumeSerialNumber),
        file_id=bytes(file_id.FileId.Identifier),
        final_path=final_path,
    )


def _same_file_identity(left: _WindowsTLSFileIdentity, right: _WindowsTLSFileIdentity) -> bool:
    return left.volume_serial_number == right.volume_serial_number and left.file_id == right.file_id


def _relative_create_new_handle(guard: _TLSDirectoryGuard, name: str, *, delete_access: bool = False) -> _NativeHandle:
    if Path(name).name != name or not name or "\\" in name or "/" in name:
        raise ValueError("TLS state filename must be one basename")
    guard.revalidate()
    return _nt_create_relative_file(guard, name, delete_access=delete_access)


def _nt_create_relative_file(guard: _TLSDirectoryGuard, name: str, *, delete_access: bool = False) -> _NativeHandle:
    name_buffer = ctypes.create_unicode_buffer(name)
    unicode_name = _UNICODE_STRING(
        Length=len(name.encode("utf-16-le")),
        MaximumLength=ctypes.sizeof(name_buffer),
        Buffer=ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    descriptor = wintypes.LPVOID()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    if not convert(
        f"O:{guard.owner_sid}D:P(A;;GA;;;{guard.owner_sid})",
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        None,
    ):
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "owner-only Windows TLS security descriptor is unavailable")
    attributes = _OBJECT_ATTRIBUTES(
        Length=ctypes.sizeof(_OBJECT_ATTRIBUTES),
        RootDirectory=_handle_value(guard.handle),
        ObjectName=ctypes.pointer(unicode_name),
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
    desired_access = _GENERIC_WRITE | _SYNCHRONIZE | _FILE_READ_ATTRIBUTES | (_DELETE if delete_access else 0)
    try:
        status = int(
            nt_create(
                ctypes.byref(raw_handle),
                desired_access,
                ctypes.byref(attributes),
                ctypes.byref(io_status),
                None,
                _FILE_ATTRIBUTE_NORMAL,
                0,
                _FILE_CREATE,
                _FILE_NON_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_FLAG_OPEN_REPARSE_POINT,
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
        if error in {80, 183}:
            raise FileExistsError(str(guard.root / name))
        _raise("WINDOWS_TLS_WRITE_FAILED", "Windows TLS state file could not be created relative to the pinned directory")
    value = int(raw_handle.value or 0)
    if value == 0:
        _raise("WINDOWS_TLS_WRITE_FAILED", "Windows TLS state file handle is invalid")
    return _NativeHandle(value)


def _write_handle_bytes(handle: Any, data: bytes) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    write_file = kernel32.WriteFile
    write_file.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    write_file.restype = wintypes.BOOL
    flush = kernel32.FlushFileBuffers
    flush.argtypes = [wintypes.HANDLE]
    flush.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(data)
    written = wintypes.DWORD()
    if not write_file(_handle_value(handle), buffer, len(data), ctypes.byref(written), None) or int(written.value) != len(data):
        _raise("WINDOWS_TLS_WRITE_FAILED", "Windows TLS state file could not be written completely")
    if not flush(_handle_value(handle)):
        _raise("WINDOWS_TLS_WRITE_FAILED", "Windows TLS state file could not be flushed durably")


def _rename_handle_manifest_replace(source_handle: Any, guard: _TLSDirectoryGuard, target_name: str) -> None:
    if Path(target_name).name != target_name or not target_name:
        raise ValueError("TLS manifest target must be one basename")
    encoded = target_name.encode("utf-16-le")
    filename_offset = _FILE_RENAME_INFORMATION_HEADER.FileNameLength.offset + ctypes.sizeof(wintypes.DWORD)
    buffer = ctypes.create_string_buffer(filename_offset + len(encoded))
    header = _FILE_RENAME_INFORMATION_HEADER.from_buffer(buffer)
    header.ReplaceIfExists = 1
    header.RootDirectory = _handle_value(guard.handle)
    header.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + filename_offset, encoded, len(encoded))
    io_status = _IO_STATUS_BLOCK()
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    nt_set = ntdll.NtSetInformationFile
    nt_set.argtypes = [wintypes.HANDLE, ctypes.POINTER(_IO_STATUS_BLOCK), wintypes.LPVOID, wintypes.ULONG, wintypes.ULONG]
    nt_set.restype = wintypes.LONG
    status = int(
        nt_set(
            _handle_value(source_handle),
            ctypes.byref(io_status),
            ctypes.byref(buffer),
            len(buffer),
            _FILE_RENAME_INFORMATION_CLASS,
        )
    )
    if status < 0:
        _raise("WINDOWS_TLS_WRITE_FAILED", "Windows TLS manifest could not be published atomically")


def _create_new_private_file(guard: _TLSDirectoryGuard, name: str, data: bytes) -> Path:
    if Path(name).name != name or not name:
        raise ValueError("TLS state filename must be one basename")
    path = guard.root / name
    handle = None
    try:
        handle = _relative_create_new_handle(guard, name)
        identity = _handle_identity(handle, expected_path=path)
        validate_owner_only_kernel_object(handle, guard.owner_sid)
        _write_handle_bytes(handle, data)
        validate_owner_only_kernel_object(handle, guard.owner_sid)
        guard.revalidate()
        after = _handle_identity(handle, expected_path=path)
        if not _same_file_identity(identity, after):
            _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS file identity changed during publication")
    except FileExistsError:
        raise
    except WindowsSecurityError as exc:
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS file handle is not owner-private", exc)
    except WindowsTLSError:
        raise
    except Exception as exc:
        _raise("WINDOWS_TLS_WRITE_FAILED", "Windows TLS state file could not be created privately", exc)
    finally:
        if handle is not None:
            handle.Close()
    guard.revalidate()
    try:
        validate_owner_private_path(path, owner_sid=guard.owner_sid, directory=False)
    except WindowsSecurityError as exc:
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS state file is not owner-private", exc)
    return path


def _read_private_file(guard: _TLSDirectoryGuard, path: Path, *, missing_invalid: bool = True) -> bytes:
    guard.revalidate()
    _path_guard(path)
    if not path.exists():
        code = "WINDOWS_TLS_MATERIAL_INVALID" if missing_invalid else "WINDOWS_TLS_MANIFEST_MISSING"
        _raise(code, "Windows TLS state is incomplete")
    try:
        validate_owner_private_path(path, owner_sid=guard.owner_sid, directory=False)
    except WindowsSecurityError as exc:
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS state file is not owner-private", exc)
    try:
        data = path.read_bytes()
    except OSError as exc:
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS state file could not be read safely", exc)
    guard.revalidate()
    return data


def _file_entry(name: str, data: bytes) -> dict[str, str]:
    return {"name": name, "sha256": _sha256(data)}


def _cert_times(cert: Any) -> tuple[datetime, datetime]:
    before = cert.not_valid_before_utc
    after = cert.not_valid_after_utc
    return before.astimezone(timezone.utc), after.astimezone(timezone.utc)


def _public_key_digest(key: Any, serialization: Any) -> str:
    data = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return _sha256(data)


def _validate_manifest_shape(raw: Any, hostname: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _raise("WINDOWS_TLS_MANIFEST_INVALID", "Windows TLS manifest must be one JSON object")
    expected = {
        "schema_version",
        "hostname",
        "ca_generation",
        "leaf_generation",
        "files",
        "root_fingerprint_sha256",
        "leaf_fingerprint_sha256",
        "root_public_key_sha256",
        "leaf_public_key_sha256",
        "root_not_after_utc",
        "leaf_not_after_utc",
    }
    if set(raw) != expected or raw.get("schema_version") != _SCHEMA_VERSION:
        _raise("WINDOWS_TLS_MANIFEST_INVALID", "Windows TLS manifest schema is invalid")
    if raw.get("hostname") != hostname:
        _raise("WINDOWS_TLS_HOST_MISMATCH", "Windows TLS state is bound to a different hostname")
    for field in ("ca_generation", "leaf_generation"):
        if not isinstance(raw.get(field), str) or _TOKEN_RE.fullmatch(raw[field]) is None:
            _raise("WINDOWS_TLS_MANIFEST_INVALID", "Windows TLS manifest generation identifier is invalid")
    files = raw.get("files")
    if not isinstance(files, dict) or set(files) != {"root_key", "root_cert", "leaf_key", "leaf_cert"}:
        _raise("WINDOWS_TLS_MANIFEST_INVALID", "Windows TLS manifest file set is invalid")
    for entry in files.values():
        if not isinstance(entry, dict) or set(entry) != {"name", "sha256"}:
            _raise("WINDOWS_TLS_MANIFEST_INVALID", "Windows TLS manifest file entry is invalid")
        name = entry.get("name")
        digest = entry.get("sha256")
        if not isinstance(name, str) or Path(name).name != name or not name:
            _raise("WINDOWS_TLS_MANIFEST_INVALID", "Windows TLS manifest filename is invalid")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            _raise("WINDOWS_TLS_MANIFEST_INVALID", "Windows TLS manifest file hash is invalid")
    for field in (
        "root_fingerprint_sha256",
        "leaf_fingerprint_sha256",
        "root_public_key_sha256",
        "leaf_public_key_sha256",
    ):
        if not isinstance(raw.get(field), str) or _SHA256_RE.fullmatch(raw[field]) is None:
            _raise("WINDOWS_TLS_MANIFEST_INVALID", "Windows TLS manifest fingerprint binding is invalid")
    for field in ("root_not_after_utc", "leaf_not_after_utc"):
        try:
            parsed = datetime.fromisoformat(raw[field])
        except (TypeError, ValueError) as exc:
            _raise("WINDOWS_TLS_MANIFEST_INVALID", "Windows TLS manifest validity binding is invalid", exc)
        if parsed.tzinfo is None:
            _raise("WINDOWS_TLS_MANIFEST_INVALID", "Windows TLS manifest validity binding is invalid")
    return raw


def _validate_crypto_material(
    *,
    hostname: str,
    root_key_pem: bytes,
    root_cert_pem: bytes,
    leaf_key_pem: bytes,
    leaf_cert_pem: bytes,
    manifest: dict[str, Any],
    now: datetime,
    require_current: bool,
) -> tuple[Any, Any, Any, Any, datetime, datetime]:
    x509, hashes, serialization, padding, rsa, ExtendedKeyUsageOID, _NameOID = _crypto()
    try:
        root_key = serialization.load_pem_private_key(root_key_pem, password=None)
        leaf_key = serialization.load_pem_private_key(leaf_key_pem, password=None)
        root_cert = x509.load_pem_x509_certificate(root_cert_pem)
        leaf_cert = x509.load_pem_x509_certificate(leaf_cert_pem)
    except Exception as exc:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS key or certificate encoding is invalid", exc)
    if not isinstance(root_key, rsa.RSAPrivateKey) or root_key.key_size < 3072:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS root key role is invalid")
    if not isinstance(leaf_key, rsa.RSAPrivateKey) or leaf_key.key_size < 2048:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS leaf key role is invalid")
    root_public = root_cert.public_key()
    leaf_public = leaf_cert.public_key()
    if not isinstance(root_public, rsa.RSAPublicKey) or not isinstance(leaf_public, rsa.RSAPublicKey):
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS certificate key algorithm is invalid")
    root_pub_digest = _public_key_digest(root_public, serialization)
    leaf_pub_digest = _public_key_digest(leaf_public, serialization)
    if root_pub_digest != _public_key_digest(root_key.public_key(), serialization):
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS root certificate does not match its private key")
    if leaf_pub_digest != _public_key_digest(leaf_key.public_key(), serialization):
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS leaf certificate does not match its private key")
    if root_pub_digest == leaf_pub_digest:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS root and leaf must use distinct keys")
    if root_pub_digest != manifest["root_public_key_sha256"] or leaf_pub_digest != manifest["leaf_public_key_sha256"]:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS manifest key binding does not match certificate material")
    try:
        root_bc = root_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        leaf_bc = leaf_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        root_ku = root_cert.extensions.get_extension_for_class(x509.KeyUsage).value
        leaf_ku = leaf_cert.extensions.get_extension_for_class(x509.KeyUsage).value
        san = leaf_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        eku = leaf_cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound as exc:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS certificate role extensions are incomplete", exc)
    if not root_bc.ca or root_bc.path_length != 0 or not root_ku.key_cert_sign or not root_ku.crl_sign:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS root certificate CA role is invalid")
    if leaf_bc.ca or not leaf_ku.digital_signature or not leaf_ku.key_encipherment:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS leaf certificate server role is invalid")
    names = list(san)
    if len(names) != 1 or not isinstance(names[0], x509.DNSName) or names[0].value != hostname:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS leaf SAN must contain exactly the configured hostname")
    if list(eku) != [ExtendedKeyUsageOID.SERVER_AUTH]:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS leaf extended key usage must be serverAuth only")
    if root_cert.subject != root_cert.issuer or leaf_cert.issuer != root_cert.subject:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS certificate issuer binding is invalid")
    try:
        root_public.verify(
            root_cert.signature,
            root_cert.tbs_certificate_bytes,
            padding.PKCS1v15(),
            root_cert.signature_hash_algorithm,
        )
        root_public.verify(
            leaf_cert.signature,
            leaf_cert.tbs_certificate_bytes,
            padding.PKCS1v15(),
            leaf_cert.signature_hash_algorithm,
        )
    except Exception as exc:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS certificate signature validation failed", exc)
    root_before, root_after = _cert_times(root_cert)
    leaf_before, leaf_after = _cert_times(leaf_cert)
    if root_after <= root_before or root_after - root_before > _ROOT_VALIDITY + _NOT_BEFORE_SKEW:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS root certificate validity is outside the bounded policy")
    if leaf_after <= leaf_before or leaf_after - leaf_before > _LEAF_VALIDITY + _NOT_BEFORE_SKEW:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS leaf certificate validity is outside the bounded policy")
    if leaf_before < root_before or leaf_after > root_after:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS leaf validity is outside the root validity window")
    if require_current and not (root_before <= now < root_after and leaf_before <= now < leaf_after):
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS certificate material is not currently valid")
    root_fp = root_cert.fingerprint(hashes.SHA256()).hex()
    leaf_fp = leaf_cert.fingerprint(hashes.SHA256()).hex()
    if root_fp != manifest["root_fingerprint_sha256"] or leaf_fp != manifest["leaf_fingerprint_sha256"]:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS manifest certificate fingerprint does not match")
    if manifest["root_not_after_utc"] != root_after.isoformat() or manifest["leaf_not_after_utc"] != leaf_after.isoformat():
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS manifest validity does not match certificate material")
    return root_key, root_cert, leaf_key, leaf_cert, root_after, leaf_after


def _load_under_guard(
    guard: _TLSDirectoryGuard,
    hostname: str,
    *,
    now: datetime,
    require_current: bool,
) -> _LoadedMaterial:
    manifest_path = guard.root / _MANIFEST_NAME
    manifest_bytes = _read_private_file(guard, manifest_path, missing_invalid=False)
    try:
        raw = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _raise("WINDOWS_TLS_MANIFEST_INVALID", "Windows TLS manifest encoding is invalid", exc)
    manifest = _validate_manifest_shape(raw, hostname)
    files: dict[str, tuple[Path, bytes]] = {}
    for role, entry in manifest["files"].items():
        path = guard.root / entry["name"]
        data = _read_private_file(guard, path)
        if _sha256(data) != entry["sha256"]:
            _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS manifest file hash does not match")
        files[role] = (path, data)
    root_key, root_cert, leaf_key, leaf_cert, root_after, leaf_after = _validate_crypto_material(
        hostname=hostname,
        root_key_pem=files["root_key"][1],
        root_cert_pem=files["root_cert"][1],
        leaf_key_pem=files["leaf_key"][1],
        leaf_cert_pem=files["leaf_cert"][1],
        manifest=manifest,
        now=now,
        require_current=require_current,
    )
    public = WindowsTLSMaterial(
        root_dir=guard.root,
        hostname=hostname,
        manifest_path=manifest_path,
        root_key_path=files["root_key"][0],
        root_cert_path=files["root_cert"][0],
        leaf_key_path=files["leaf_key"][0],
        leaf_cert_path=files["leaf_cert"][0],
        root_fingerprint_sha256=manifest["root_fingerprint_sha256"],
        leaf_fingerprint_sha256=manifest["leaf_fingerprint_sha256"],
        root_not_after_utc=root_after,
        leaf_not_after_utc=leaf_after,
    )
    return _LoadedMaterial(public, manifest, root_key, root_cert, leaf_key, leaf_cert)


def _issue_root(hostname: str, now: datetime, ca_generation: str) -> tuple[Any, Any, bytes, bytes]:
    x509, hashes, serialization, _padding, rsa, _ExtendedKeyUsageOID, NameOID = _crypto()
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"FactLane Private CA {ca_generation[:12]}")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _NOT_BEFORE_SKEW)
        .not_valid_after(now + _ROOT_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key, cert, key_pem, cert_pem


def _issue_leaf(hostname: str, now: datetime, root_key: Any, root_cert: Any) -> tuple[Any, Any, bytes, bytes]:
    x509, hashes, serialization, _padding, rsa, ExtendedKeyUsageOID, NameOID = _crypto()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_after = _cert_times(root_cert)[1]
    leaf_after = min(now + _LEAF_VALIDITY, root_after)
    if leaf_after <= now + _NOT_BEFORE_SKEW:
        _raise("WINDOWS_TLS_MATERIAL_INVALID", "Windows TLS root certificate is too close to expiry for leaf issuance")
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(root_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _NOT_BEFORE_SKEW)
        .not_valid_after(leaf_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key, cert, key_pem, cert_pem


def _publish_manifest(guard: _TLSDirectoryGuard, manifest: dict[str, Any]) -> None:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    manifest_path = guard.root / _MANIFEST_NAME
    _path_guard(manifest_path)
    if manifest_path.exists():
        _read_private_file(guard, manifest_path, missing_invalid=False)
    temp_name = None
    temp_handle = None
    for _attempt in range(8):
        candidate = f".manifest-{_new_generation_token()}.tmp"
        try:
            temp_handle = _relative_create_new_handle(guard, candidate, delete_access=True)
            temp_name = candidate
            break
        except FileExistsError:
            continue
    if temp_handle is None or temp_name is None:
        _raise("WINDOWS_TLS_COLLISION", "Windows TLS manifest staging collided repeatedly")
    temp_path = guard.root / temp_name
    try:
        source_identity = _handle_identity(temp_handle, expected_path=temp_path)
        try:
            validate_owner_only_kernel_object(temp_handle, guard.owner_sid)
        except WindowsSecurityError as exc:
            _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS manifest source handle is not owner-private", exc)
        _write_handle_bytes(temp_handle, payload)
        try:
            validate_owner_only_kernel_object(temp_handle, guard.owner_sid)
        except WindowsSecurityError as exc:
            _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS manifest source handle is not owner-private", exc)
        guard.revalidate()
        _rename_handle_manifest_replace(temp_handle, guard, _MANIFEST_NAME)
        published_identity = _handle_identity(temp_handle, expected_path=manifest_path)
        if not _same_file_identity(source_identity, published_identity):
            _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS manifest publication changed file identity")
        try:
            validate_owner_only_kernel_object(temp_handle, guard.owner_sid)
        except WindowsSecurityError as exc:
            _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS manifest destination handle is not owner-private", exc)
        guard.revalidate()
    finally:
        temp_handle.Close()
    guard.revalidate()
    try:
        validate_owner_private_path(manifest_path, owner_sid=guard.owner_sid, directory=False)
    except WindowsSecurityError as exc:
        _raise("WINDOWS_TLS_CUSTODY_UNSAFE", "Windows TLS manifest is not owner-private after publication", exc)


def _publish_generation(
    guard: _TLSDirectoryGuard,
    hostname: str,
    *,
    now: datetime,
    existing: _LoadedMaterial | None,
) -> None:
    _x509, hashes, serialization, _padding, _rsa, _eku, _name = _crypto()
    for _attempt in range(8):
        ca_generation = existing.manifest["ca_generation"] if existing is not None else _new_generation_token()
        leaf_generation = _new_generation_token()
        if existing is None:
            root_key, root_cert, root_key_pem, root_cert_pem = _issue_root(hostname, now, ca_generation)
            root_key_name = f"ca-{ca_generation}.key.pem"
            root_cert_name = f"ca-{ca_generation}.cert.pem"
        else:
            root_key = existing.root_key
            root_cert = existing.root_cert
            root_key_name = existing.manifest["files"]["root_key"]["name"]
            root_cert_name = existing.manifest["files"]["root_cert"]["name"]
            root_key_pem = _read_private_file(guard, guard.root / root_key_name)
            root_cert_pem = _read_private_file(guard, guard.root / root_cert_name)
        leaf_key, leaf_cert, leaf_key_pem, leaf_cert_pem = _issue_leaf(hostname, now, root_key, root_cert)
        leaf_key_name = f"leaf-{leaf_generation}.key.pem"
        leaf_cert_name = f"leaf-{leaf_generation}.cert.pem"
        try:
            if existing is None:
                _create_new_private_file(guard, root_key_name, root_key_pem)
                _create_new_private_file(guard, root_cert_name, root_cert_pem)
            _create_new_private_file(guard, leaf_key_name, leaf_key_pem)
            _create_new_private_file(guard, leaf_cert_name, leaf_cert_pem)
        except FileExistsError:
            existing = None
            continue
        root_after = _cert_times(root_cert)[1]
        leaf_after = _cert_times(leaf_cert)[1]
        root_fp = root_cert.fingerprint(hashes.SHA256()).hex()
        leaf_fp = leaf_cert.fingerprint(hashes.SHA256()).hex()
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "hostname": hostname,
            "ca_generation": ca_generation,
            "leaf_generation": leaf_generation,
            "files": {
                "root_key": _file_entry(root_key_name, root_key_pem),
                "root_cert": _file_entry(root_cert_name, root_cert_pem),
                "leaf_key": _file_entry(leaf_key_name, leaf_key_pem),
                "leaf_cert": _file_entry(leaf_cert_name, leaf_cert_pem),
            },
            "root_fingerprint_sha256": root_fp,
            "leaf_fingerprint_sha256": leaf_fp,
            "root_public_key_sha256": _public_key_digest(root_cert.public_key(), serialization),
            "leaf_public_key_sha256": _public_key_digest(leaf_cert.public_key(), serialization),
            "root_not_after_utc": root_after.isoformat(),
            "leaf_not_after_utc": leaf_after.isoformat(),
        }
        _publish_manifest(guard, manifest)
        return
    _raise("WINDOWS_TLS_COLLISION", "Windows TLS generation filenames collided repeatedly")


def load_windows_tls_material(
    root_dir: str | os.PathLike[str],
    hostname: str,
    *,
    owner_sid: str | None = None,
    now: datetime | None = None,
) -> WindowsTLSMaterial:
    _require_windows()
    exact_hostname = _validate_hostname(hostname)
    current = _utc_now(now)
    sid = current_user_sid() if owner_sid is None else owner_sid
    root = Path(root_dir)
    with _TLSDirectoryGuard.open(root, sid, create=False) as guard:
        return _load_under_guard(guard, exact_hostname, now=current, require_current=True).public


def ensure_windows_tls_material(
    root_dir: str | os.PathLike[str],
    hostname: str,
    *,
    owner_sid: str | None = None,
    now: datetime | None = None,
) -> WindowsTLSMaterial:
    _require_windows()
    exact_hostname = _validate_hostname(hostname)
    current = _utc_now(now)
    sid = current_user_sid() if owner_sid is None else owner_sid
    root = Path(root_dir)
    with _TLSDirectoryGuard.open(root, sid, create=True) as guard:
        existing: _LoadedMaterial | None = None
        manifest_path = root / _MANIFEST_NAME
        _path_guard(manifest_path)
        if manifest_path.exists():
            try:
                existing = _load_under_guard(guard, exact_hostname, now=current, require_current=False)
            except WindowsTLSError as exc:
                if exc.code in {"WINDOWS_TLS_CUSTODY_UNSAFE", "WINDOWS_TLS_HOST_MISMATCH"}:
                    raise
                existing = None
        if existing is None:
            _publish_generation(guard, exact_hostname, now=current, existing=None)
        elif existing.public.root_not_after_utc - current <= _ROOT_RENEW_BEFORE:
            _publish_generation(guard, exact_hostname, now=current, existing=None)
        elif existing.public.leaf_not_after_utc - current <= _LEAF_RENEW_BEFORE:
            _publish_generation(guard, exact_hostname, now=current, existing=existing)
        return _load_under_guard(guard, exact_hostname, now=current, require_current=True).public


def build_windows_server_ssl_context(material: WindowsTLSMaterial) -> ssl.SSLContext:
    if not isinstance(material, WindowsTLSMaterial):
        raise TypeError("material must be WindowsTLSMaterial")
    _require_windows()
    validated = load_windows_tls_material(material.root_dir, material.hostname)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(validated.leaf_cert_path), keyfile=str(validated.leaf_key_path))
    return context


def build_windows_client_ssl_context(material: WindowsTLSMaterial) -> ssl.SSLContext:
    if not isinstance(material, WindowsTLSMaterial):
        raise TypeError("material must be WindowsTLSMaterial")
    _require_windows()
    validated = load_windows_tls_material(material.root_dir, material.hostname)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    if hasattr(context, "hostname_checks_common_name"):
        context.hostname_checks_common_name = False
    context.load_verify_locations(cafile=str(validated.root_cert_path))
    return context
