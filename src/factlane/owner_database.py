from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from typing import Any, Awaitable, Callable
import urllib.parse
import uuid

from .adapter import MemoryAdapter
from .contract import (
    AdapterError,
    LIFECYCLE_STATES,
    MEMORY_TYPES,
    VERIFIED_BY,
    canonical_json,
    iso_now,
    native_hash,
    parse_iso,
    validate_fact,
    validate_freshness,
    validate_identifier,
    validate_provenance,
    validate_scope,
)
from .windows_security import (
    WindowsSecurityError,
    current_user_sid,
    owner_only_security_attributes,
    protect_owner_private_path,
    validate_owner_private_path,
    validate_windows_path_no_reparse,
)


LOGICAL_FORMAT = "factlane.logical.v1"
LOGICAL_IMPORT_MAX_BYTES = 10 * 1024 * 1024
LOGICAL_IMPORT_MAX_RECORDS = 1000

_LOGICAL_ENVELOPE_FIELDS = {"format", "exported_at", "producer", "records"}
_PRODUCER_FIELDS = {"factlane_version", "storage_contract_version", "embedding_profile_id"}
_LOGICAL_RECORD_FIELDS = {
    "source_memory_id",
    "source_record_id",
    "source_revision",
    "scope",
    "memory_type",
    "fact",
    "tags",
    "source_provenance",
    "freshness_policy",
    "source_timestamp",
    "source_lifecycle",
    "source_verified_by",
    "parent_source_record_id",
    "supersedes_source_memory_ids",
    "active",
}
_SCOPE_IDENTITY_FIELDS = ("project_id", "worktree_id", "workflow_id", "agent_id")
_SCOPE_FIELDS = {"scope", *_SCOPE_IDENTITY_FIELDS}
_ACTIVE_SOURCE_STATES = {"CANDIDATE", "VALIDATED_CURRENT"}
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

VectorPrepareCallback = Callable[[list[dict[str, Any]], dict[str, str]], Any | Awaitable[Any]]
CandidateAdmissionCallback = Callable[
    [list[dict[str, Any]], Any, dict[str, str]],
    dict[str, Any] | Awaitable[dict[str, Any]],
]
DestinationProbeCallback = Callable[[dict[str, Any]], Any | Awaitable[Any]]


def _raise(code: str, message: str) -> None:
    raise AdapterError(code, message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_artifact_id(value: str) -> str:
    if not isinstance(value, str) or not _ARTIFACT_ID_RE.fullmatch(value):
        _raise("INVALID_ARTIFACT", "artifact identity is invalid")
    return value


def _windows_path_guard(
    path: Path,
    *,
    directory: bool,
    protect: bool,
    code: str,
    message: str,
) -> None:
    try:
        owner_sid = current_user_sid()
        if protect:
            protect_owner_private_path(path, owner_sid=owner_sid, directory=directory)
        else:
            validate_owner_private_path(path, owner_sid=owner_sid, directory=directory)
    except WindowsSecurityError as exc:
        raise AdapterError(code, message) from exc


def _validate_private_regular_file(
    path: Path,
    *,
    code: str,
    message: str,
    type_message: str | None = None,
) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        _raise(code, type_message or message)
    if os.name == "nt":
        _windows_path_guard(path, directory=False, protect=False, code=code, message=message)
    elif info.st_uid != os.getuid():
        _raise(code, message)
    return info


def _protect_created_private_file(path: Path, *, code: str, message: str) -> None:
    if os.name == "nt":
        _windows_path_guard(path, directory=False, protect=True, code=code, message=message)
    else:
        os.chmod(path, 0o600)


class _WindowsParentGuard:
    def __init__(
        self,
        *,
        path: Path,
        handle: Any,
        identity: tuple[int, int, int],
        owner_sid: str,
        code: str,
        message: str,
    ) -> None:
        self.path = path
        self._handle = handle
        self._identity = identity
        self._owner_sid = owner_sid
        self._code = code
        self._message = message

    def require_current(self) -> None:
        if self._handle is None:
            _raise(self._code, self._message)
        current = _windows_parent_identity(
            self._handle,
            self.path,
            code=self._code,
            message=self._message,
        )
        if current != self._identity:
            _raise(self._code, self._message)
        try:
            validate_owner_private_path(self.path, owner_sid=self._owner_sid, directory=True)
        except WindowsSecurityError as exc:
            raise AdapterError(self._code, self._message) from exc

    def raw_handle(self) -> int:
        if self._handle is None:
            _raise(self._code, self._message)
        value = int(self._handle)
        if value in {0, -1}:
            _raise(self._code, self._message)
        return value

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.Close()
            except Exception:
                pass


def _windows_handle_final_path(raw_handle: int, *, code: str, message: str) -> str:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_final = kernel32.GetFinalPathNameByHandleW
        get_final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_final.restype = wintypes.DWORD
        required = int(get_final(raw_handle, None, 0, 0))
        if required <= 0 or required > 32768:
            _raise(code, message)
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = int(get_final(raw_handle, buffer, len(buffer), 0))
        if written <= 0 or written >= len(buffer):
            _raise(code, message)
        final_path = buffer.value
        if final_path.startswith("\\\\?\\UNC\\"):
            final_path = "\\\\" + final_path[8:]
        elif final_path.startswith("\\\\?\\"):
            final_path = final_path[4:]
        return final_path
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError(code, message) from exc


def _windows_parent_identity(
    handle: Any,
    expected_path: Path,
    *,
    code: str,
    message: str,
) -> tuple[int, int, int]:
    try:
        import win32con
        import win32file

        info = win32file.GetFileInformationByHandle(handle)
        attributes = int(info[0])
        directory_flag = int(getattr(win32con, "FILE_ATTRIBUTE_DIRECTORY", 0x10))
        reparse_flag = int(getattr(win32con, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if not attributes & directory_flag or attributes & reparse_flag:
            _raise(code, message)
        final_path = _windows_handle_final_path(int(handle), code=code, message=message)
        expected_key = os.path.normcase(os.path.normpath(os.path.abspath(str(expected_path))))
        observed_key = os.path.normcase(os.path.normpath(os.path.abspath(final_path)))
        if observed_key != expected_key:
            _raise(code, message)
        return int(info[4]), int(info[8]), int(info[9])
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError(code, message) from exc


def _open_windows_parent_guard(path: Path, *, code: str, message: str) -> _WindowsParentGuard:
    parent = path.parent
    try:
        import win32con
        import win32file

        owner_sid = current_user_sid()
        validate_windows_path_no_reparse(parent)
        validate_owner_private_path(parent, owner_sid=owner_sid, directory=True)
        handle = win32file.CreateFile(
            str(parent),
            int(getattr(win32con, "FILE_READ_ATTRIBUTES", 0x80))
            | int(getattr(win32con, "FILE_ADD_FILE", 0x2))
            | int(getattr(win32con, "FILE_DELETE_CHILD", 0x40)),
            int(win32con.FILE_SHARE_READ) | int(win32con.FILE_SHARE_WRITE),
            None,
            win32con.OPEN_EXISTING,
            int(getattr(win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0x00200000))
            | int(getattr(win32con, "FILE_FLAG_BACKUP_SEMANTICS", 0x02000000)),
            None,
        )
    except (ImportError, OSError, WindowsSecurityError) as exc:
        raise AdapterError(code, message) from exc
    try:
        identity = _windows_parent_identity(handle, parent, code=code, message=message)
        validate_owner_private_path(parent, owner_sid=owner_sid, directory=True)
        guard = _WindowsParentGuard(
            path=parent,
            handle=handle,
            identity=identity,
            owner_sid=owner_sid,
            code=code,
            message=message,
        )
        guard.require_current()
        return guard
    except BaseException:
        try:
            handle.Close()
        except Exception:
            pass
        raise


def _flush_private_file(fd: int) -> None:
    if os.name != "nt":
        os.fsync(fd)
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt

    raw_handle = msvcrt.get_osfhandle(fd)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    flush = kernel32.FlushFileBuffers
    flush.argtypes = [wintypes.HANDLE]
    flush.restype = wintypes.BOOL
    if not flush(wintypes.HANDLE(raw_handle)):
        error = ctypes.get_last_error()
        raise OSError(error, "FlushFileBuffers failed")


def _write_private_bytes(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("private file write made no progress")
        written += count
    _flush_private_file(fd)


def _unlink_private_file(
    path: Path,
    *,
    parent_guard: _WindowsParentGuard | None,
    expected_identity: tuple[int, int] | None = None,
) -> bool:
    if os.name == "nt":
        if parent_guard is None or parent_guard.path != path.parent:
            return False
        try:
            parent_guard.require_current()
        except AdapterError:
            return False
        try:
            info = path.lstat()
        except FileNotFoundError:
            return True
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return False
        if expected_identity is not None and (info.st_dev, info.st_ino) != expected_identity:
            return False
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    return True


def _open_new_private_file(
    path: Path,
    flags: int,
    *,
    code: str,
    message: str,
) -> tuple[int, _WindowsParentGuard | None]:
    parent_guard: _WindowsParentGuard | None = None
    if os.name == "nt":
        validate_windows_path_no_reparse(path)
        try:
            import msvcrt
            import win32api
            import win32con
            import win32file
        except ImportError as exc:  # pragma: no cover - native-Windows packaging guard
            raise AdapterError("WINDOWS_SECURITY_UNAVAILABLE", "Windows private file creation is unavailable") from exc
        parent_guard = _open_windows_parent_guard(path, code=code, message=message)
        try:
            parent_guard.require_current()
            sid = current_user_sid()
            desired_access = int(win32con.GENERIC_WRITE) | int(getattr(win32con, "DELETE", 0x00010000))
            if flags & os.O_RDWR:
                desired_access |= int(win32con.GENERIC_READ)
            handle = None
            raw_handle: int | None = None
            try:
                handle = win32file.CreateFile(
                    str(path),
                    desired_access,
                    0,
                    owner_only_security_attributes(sid),
                    win32con.CREATE_NEW,
                    win32con.FILE_ATTRIBUTE_NORMAL,
                    None,
                )
                raw_handle = int(handle.Detach())
                handle = None
                fd = msvcrt.open_osfhandle(
                    raw_handle,
                    flags | getattr(os, "O_BINARY", 0),
                )
                raw_handle = None
            except FileExistsError:
                raise
            except Exception as exc:
                winerror = getattr(exc, "winerror", None)
                if winerror in {80, 183}:
                    raise FileExistsError(str(path)) from exc
                raise AdapterError(code, message) from exc
            finally:
                if handle is not None:
                    handle.Close()
                if raw_handle is not None:
                    try:
                        win32api.CloseHandle(raw_handle)
                    except Exception:
                        pass
        except BaseException:
            parent_guard.close()
            raise
    else:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _protect_created_private_file(path, code=code, message=message)
        path_info = _validate_private_regular_file(path, code=code, message=message)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (path_info.st_dev, path_info.st_ino):
            _raise(code, message)
        return fd, parent_guard
    except BaseException:
        os.close(fd)
        _unlink_private_file(path, parent_guard=parent_guard)
        if parent_guard is not None:
            parent_guard.close()
        raise


def _windows_move_file(
    source: Path,
    destination: Path,
    *,
    replace: bool,
    source_fd: int,
    source_parent_guard: _WindowsParentGuard,
    destination_parent_guard: _WindowsParentGuard,
) -> None:
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt
    except ImportError as exc:  # pragma: no cover - native-Windows packaging guard
        raise AdapterError("WINDOWS_SECURITY_UNAVAILABLE", "Windows durable file publication is unavailable") from exc
    try:
        if source_parent_guard.path != source.parent or destination_parent_guard.path != destination.parent:
            _raise("ARTIFACT_PATH_INVALID", "Windows publication parent binding changed")
        source_parent_guard.require_current()
        if destination_parent_guard is not source_parent_guard:
            destination_parent_guard.require_current()
        source_info = os.fstat(source_fd)
        source_path_info = _validate_private_regular_file(
            source,
            code="ARTIFACT_PATH_INVALID",
            message="Windows publication source identity changed",
        )
        if (source_info.st_dev, source_info.st_ino) != (source_path_info.st_dev, source_path_info.st_ino):
            _raise("ARTIFACT_PATH_INVALID", "Windows publication source identity changed")
        raw_handle = int(msvcrt.get_osfhandle(source_fd))
        observed_source = _windows_handle_final_path(
            raw_handle,
            code="ARTIFACT_PATH_INVALID",
            message="Windows publication source handle changed identity",
        )
        if os.path.normcase(os.path.abspath(observed_source)) != os.path.normcase(os.path.abspath(str(source))):
            _raise("ARTIFACT_PATH_INVALID", "Windows publication source handle changed identity")

        class _FileRenameChoice(ctypes.Union):
            _fields_ = [("ReplaceIfExists", ctypes.c_ubyte), ("Flags", wintypes.ULONG)]

        class _FileRenameInfoHeader(ctypes.Structure):
            _fields_ = [
                ("Choice", _FileRenameChoice),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.ULONG),
            ]

        class _IoStatusBlockStatus(ctypes.Union):
            _fields_ = [("Status", wintypes.LONG), ("Pointer", wintypes.LPVOID)]

        class _IoStatusBlock(ctypes.Structure):
            _fields_ = [("u", _IoStatusBlockStatus), ("Information", ctypes.c_size_t)]

        encoded = destination.name.encode("utf-16-le")
        filename_offset = _FileRenameInfoHeader.FileNameLength.offset + ctypes.sizeof(wintypes.ULONG)
        buffer = ctypes.create_string_buffer(filename_offset + len(encoded))
        header = _FileRenameInfoHeader.from_buffer(buffer)
        header.Choice.ReplaceIfExists = 1 if replace else 0
        header.RootDirectory = wintypes.HANDLE(destination_parent_guard.raw_handle())
        header.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + filename_offset, encoded, len(encoded))
        io_status = _IoStatusBlock()
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        nt_set_information = ntdll.NtSetInformationFile
        nt_set_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_IoStatusBlock),
            wintypes.LPVOID,
            wintypes.ULONG,
            ctypes.c_int,
        ]
        nt_set_information.restype = wintypes.LONG
        status = int(
            nt_set_information(
                wintypes.HANDLE(raw_handle),
                ctypes.byref(io_status),
                ctypes.byref(buffer),
                len(buffer),
                10,
            )
        )
        if status < 0:
            rtl_status_to_dos = ntdll.RtlNtStatusToDosError
            rtl_status_to_dos.argtypes = [wintypes.LONG]
            rtl_status_to_dos.restype = wintypes.ULONG
            error = int(rtl_status_to_dos(status))
            if not replace and error in {80, 183}:
                raise FileExistsError(str(destination))
            raise OSError(error, "NtSetInformationFile rename failed")

        destination_parent_guard.require_current()
        destination_info = _validate_private_regular_file(
            destination,
            code="ARTIFACT_PATH_INVALID",
            message="Windows publication destination identity changed",
        )
        current_info = os.fstat(source_fd)
        if (current_info.st_dev, current_info.st_ino) != (source_info.st_dev, source_info.st_ino):
            _raise("ARTIFACT_PATH_INVALID", "Windows publication source handle changed identity")
        if (destination_info.st_dev, destination_info.st_ino) != (source_info.st_dev, source_info.st_ino):
            _raise("ARTIFACT_PATH_INVALID", "Windows publication did not preserve source identity")
        observed_destination = _windows_handle_final_path(
            raw_handle,
            code="ARTIFACT_PATH_INVALID",
            message="Windows publication handle did not become the destination",
        )
        if os.path.normcase(os.path.abspath(observed_destination)) != os.path.normcase(os.path.abspath(str(destination))):
            _raise("ARTIFACT_PATH_INVALID", "Windows publication handle did not become the destination")
        _flush_private_file(source_fd)
    except Exception:
        raise


def _replace_private_file(
    source: Path,
    destination: Path,
    *,
    source_fd: int | None = None,
    parent_guard: _WindowsParentGuard | None = None,
) -> None:
    if os.name == "nt":
        if source_fd is None or parent_guard is None or source.parent != destination.parent:
            _raise("ARTIFACT_PATH_INVALID", "Windows replacement requires one retained parent guard")
        _windows_move_file(
            source,
            destination,
            replace=True,
            source_fd=source_fd,
            source_parent_guard=parent_guard,
            destination_parent_guard=parent_guard,
        )
    else:
        os.replace(source, destination)


def _publish_new_private_file(
    source: Path,
    destination: Path,
    *,
    source_fd: int | None = None,
    source_parent_guard: _WindowsParentGuard | None = None,
    destination_parent_guard: _WindowsParentGuard | None = None,
) -> None:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(str(destination))
    if os.name == "nt":
        if source_fd is None or source_parent_guard is None or destination_parent_guard is None:
            _raise("ARTIFACT_PATH_INVALID", "Windows publication requires retained parent guards")
        _windows_move_file(
            source,
            destination,
            replace=False,
            source_fd=source_fd,
            source_parent_guard=source_parent_guard,
            destination_parent_guard=destination_parent_guard,
        )
    else:
        os.link(source, destination, follow_symlinks=False)


def _private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            _raise("ARTIFACT_PATH_INVALID", "managed artifact root must be a real directory")
        if os.name == "nt":
            _windows_path_guard(
                path,
                directory=True,
                protect=False,
                code="ARTIFACT_PATH_INVALID",
                message="managed artifact root must be owner-controlled",
            )
        else:
            if path.stat().st_uid != os.getuid():
                _raise("ARTIFACT_PATH_INVALID", "managed artifact root must be owned by the current user")
            os.chmod(path, 0o700)
        return
    path.mkdir(parents=True, mode=0o700)
    if os.name == "nt":
        _windows_path_guard(
            path,
            directory=True,
            protect=True,
            code="ARTIFACT_PATH_INVALID",
            message="managed artifact root could not be protected for the current user",
        )
    else:
        os.chmod(path, 0o700)


class ArtifactRegistry:
    """Owner-private registry for managed database artifacts.

    Callers address artifacts only by registry identity. Paths are generated or
    registered locally and are never accepted as deletion targets from browser input.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        supplied_root = Path(root).absolute()
        if supplied_root.is_symlink():
            _raise("ARTIFACT_PATH_INVALID", "managed artifact root cannot be a symlink")
        if os.name == "nt":
            try:
                validate_windows_path_no_reparse(supplied_root)
            except WindowsSecurityError as exc:
                raise AdapterError("ARTIFACT_PATH_INVALID", "managed artifact root contains an unsafe Windows path") from exc
            _private_directory(supplied_root)
        self.root = supplied_root.resolve(strict=False)
        _private_directory(self.root)
        self._registry_path = self.root / ".factlane-artifacts.json"
        self._lock = threading.RLock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._registry_path.exists():
            return {}
        if self._registry_path.is_symlink() or not self._registry_path.is_file():
            _raise("ARTIFACT_REGISTRY_INVALID", "managed artifact registry is not a regular file")
        if os.name == "nt":
            _validate_private_regular_file(
                self._registry_path,
                code="ARTIFACT_REGISTRY_INVALID",
                message="managed artifact registry is not owner-controlled",
            )
        elif self._registry_path.stat().st_uid != os.getuid():
            _raise("ARTIFACT_REGISTRY_INVALID", "managed artifact registry is not owner-controlled")
        try:
            value = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError("ARTIFACT_REGISTRY_INVALID", "managed artifact registry cannot be read") from exc
        if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, dict) for key, item in value.items()):
            _raise("ARTIFACT_REGISTRY_INVALID", "managed artifact registry is malformed")
        return value

    def _save(self, entries: dict[str, dict[str, Any]]) -> None:
        stage = self.root / f".factlane-artifacts-{uuid.uuid4()}.tmp"
        payload = canonical_json(entries).encode("utf-8")
        fd, parent_guard = _open_new_private_file(
            stage,
            os.O_WRONLY,
            code="ARTIFACT_REGISTRY_INVALID",
            message="managed artifact registry staging file is not owner-controlled",
        )
        fd_open = True
        try:
            _write_private_bytes(fd, payload)
            if os.name != "nt":
                os.close(fd)
                fd_open = False
            if self._registry_path.exists() or self._registry_path.is_symlink():
                _validate_private_regular_file(
                    self._registry_path,
                    code="ARTIFACT_REGISTRY_INVALID",
                    message="managed artifact registry changed to an unsafe path",
                )
            if parent_guard is not None:
                parent_guard.require_current()
            _replace_private_file(
                stage,
                self._registry_path,
                source_fd=fd if os.name == "nt" else None,
                parent_guard=parent_guard,
            )
            _protect_created_private_file(
                self._registry_path,
                code="ARTIFACT_REGISTRY_INVALID",
                message="managed artifact registry is not owner-controlled",
            )
        finally:
            if fd_open:
                try:
                    os.close(fd)
                except OSError:
                    pass
            _unlink_private_file(stage, parent_guard=parent_guard)
            if parent_guard is not None:
                parent_guard.close()

    def managed_path(self, filename: str) -> Path:
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            _raise("ARTIFACT_PATH_INVALID", "managed artifact filename is invalid")
        candidate = self.root / filename
        if candidate.parent != self.root:
            _raise("ARTIFACT_PATH_INVALID", "managed artifact path escapes private storage")
        return candidate

    def _path_from_entry(self, entry: dict[str, Any]) -> Path:
        relative = entry.get("relative_path")
        if not isinstance(relative, str):
            _raise("ARTIFACT_REGISTRY_INVALID", "registered artifact path is invalid")
        return self.managed_path(relative)

    def register(
        self,
        path: str | os.PathLike[str],
        *,
        kind: str,
        artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_id = _validate_artifact_id(artifact_id or str(uuid.uuid4()))
        if not isinstance(kind, str) or not kind.strip() or len(kind.encode("utf-8")) > 64:
            _raise("INVALID_ARTIFACT", "artifact kind is invalid")
        candidate = Path(path).absolute()
        if candidate.parent != self.root:
            _raise("ARTIFACT_PATH_INVALID", "only a managed artifact path can be registered")
        try:
            info = _validate_private_regular_file(
                candidate,
                code="ARTIFACT_PATH_INVALID",
                message="managed artifact must be owned by the current user",
                type_message="managed artifact must be a regular file",
            )
        except FileNotFoundError as exc:
            raise AdapterError("ARTIFACT_NOT_FOUND", "managed artifact does not exist") from exc
        if os.name != "nt":
            os.chmod(candidate, 0o600)

        with self._lock:
            entries = self._load()
            if artifact_id in entries:
                _raise("ARTIFACT_ALREADY_REGISTERED", "artifact identity is already registered")
            if any(item.get("relative_path") == candidate.name for item in entries.values()):
                _raise("ARTIFACT_ALREADY_REGISTERED", "managed artifact path is already registered")
            receipt: dict[str, Any] = {
                "artifact_id": artifact_id,
                "kind": kind.strip(),
                "relative_path": candidate.name,
                "size_bytes": candidate.stat().st_size,
                "sha256": _sha256_file(candidate),
                "registered_at": iso_now(),
            }
            if metadata:
                reserved = set(receipt).intersection(metadata)
                if reserved:
                    _raise("INVALID_ARTIFACT", "artifact metadata cannot replace receipt fields")
                receipt.update(metadata)
            entries[artifact_id] = receipt
            self._save(entries)
            return dict(receipt)

    def get(self, artifact_id: str) -> dict[str, Any]:
        artifact_id = _validate_artifact_id(artifact_id)
        with self._lock:
            entry = self._load().get(artifact_id)
            if entry is None:
                _raise("ARTIFACT_NOT_REGISTERED", "artifact identity is not registered")
            path = self._path_from_entry(entry)
            try:
                info = _validate_private_regular_file(
                    path,
                    code="ARTIFACT_PATH_INVALID",
                    message="registered artifact is no longer owner-controlled",
                    type_message="registered artifact is no longer a regular managed file",
                )
            except FileNotFoundError as exc:
                raise AdapterError("ARTIFACT_NOT_FOUND", "registered artifact is missing") from exc
            if info.st_size != entry.get("size_bytes"):
                _raise("ARTIFACT_CHECKSUM_MISMATCH", "registered artifact size no longer matches its receipt")
            actual_digest = _sha256_file(path)
            if actual_digest != entry.get("sha256"):
                _raise("ARTIFACT_CHECKSUM_MISMATCH", "registered artifact checksum no longer matches its receipt")
            return dict(entry)

    @staticmethod
    def _metadata_projection(entry: dict[str, Any]) -> dict[str, Any]:
        """Return browser/API-safe registry metadata without a filesystem locator."""

        return {key: value for key, value in entry.items() if key != "relative_path"}

    def metadata(self, artifact_id: str) -> dict[str, Any]:
        """Return verified registered artifact metadata without exposing its managed path."""

        return self._metadata_projection(self.get(artifact_id))

    def list_metadata(self) -> list[dict[str, Any]]:
        """List verified registered artifacts in stable identity order, path-free."""

        with self._lock:
            entries = self._load()
            return [self.metadata(artifact_id) for artifact_id in sorted(entries)]

    def preview_delete(self, artifact_id: str) -> dict[str, Any]:
        """Bind deletion to the exact registered artifact identity and immutable receipt fields."""

        entry = self.get(artifact_id)
        return {
            "artifact_id": entry["artifact_id"],
            "sha256": entry["sha256"],
            "size_bytes": entry["size_bytes"],
            "kind": entry["kind"],
        }

    def delete_previewed(self, preview: dict[str, Any]) -> dict[str, Any]:
        """Delete only the registered artifact matching one exact server-issued preview."""

        required = {"artifact_id", "sha256", "size_bytes", "kind"}
        if not isinstance(preview, dict) or set(preview) != required:
            _raise("ARTIFACT_DELETE_PREVIEW_INVALID", "artifact deletion preview fields are not exact")
        artifact_id = _validate_artifact_id(preview["artifact_id"])
        sha256 = preview["sha256"]
        size_bytes = preview["size_bytes"]
        kind = preview["kind"]
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            _raise("ARTIFACT_DELETE_PREVIEW_INVALID", "artifact deletion preview checksum is invalid")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            _raise("ARTIFACT_DELETE_PREVIEW_INVALID", "artifact deletion preview size is invalid")
        if not isinstance(kind, str) or not kind:
            _raise("ARTIFACT_DELETE_PREVIEW_INVALID", "artifact deletion preview kind is invalid")
        return self.delete(
            artifact_id,
            expected_sha256=sha256,
            expected_size_bytes=size_bytes,
            expected_kind=kind,
        )

    def delete(
        self,
        artifact_id: str,
        *,
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
        expected_kind: str | None = None,
    ) -> dict[str, Any]:
        artifact_id = _validate_artifact_id(artifact_id)
        with self._lock:
            entries = self._load()
            entry = entries.get(artifact_id)
            if entry is None:
                _raise("ARTIFACT_NOT_REGISTERED", "only a registered artifact identity can be deleted")
            path = self._path_from_entry(entry)
            try:
                info = _validate_private_regular_file(
                    path,
                    code="ARTIFACT_PATH_INVALID",
                    message="registered artifact is no longer owner-controlled",
                    type_message="registered artifact is no longer a regular managed file",
                )
            except FileNotFoundError as exc:
                raise AdapterError("ARTIFACT_NOT_FOUND", "registered artifact is missing") from exc
            digest = _sha256_file(path)
            if digest != entry.get("sha256"):
                _raise("ARTIFACT_CHECKSUM_MISMATCH", "registered artifact checksum no longer matches its receipt")
            if expected_sha256 is not None and digest != expected_sha256:
                _raise("ARTIFACT_CHECKSUM_MISMATCH", "artifact deletion preview no longer matches the registered artifact")
            if expected_size_bytes is not None and info.st_size != expected_size_bytes:
                _raise("ARTIFACT_DELETE_PREVIEW_MISMATCH", "artifact deletion preview size no longer matches the registered artifact")
            if expected_size_bytes is not None and entry.get("size_bytes") != expected_size_bytes:
                _raise("ARTIFACT_DELETE_PREVIEW_MISMATCH", "artifact deletion preview size no longer matches the registry receipt")
            if expected_kind is not None and entry.get("kind") != expected_kind:
                _raise("ARTIFACT_DELETE_PREVIEW_MISMATCH", "artifact deletion preview kind no longer matches the registry receipt")
            path.unlink()
            entries.pop(artifact_id)
            self._save(entries)
            return dict(entry)

    def promote_restore_upload(self, artifact_id: str) -> dict[str, Any]:
        """Verify and promote one managed restore upload to a SQLite backup identity in place."""

        artifact_id = _validate_artifact_id(artifact_id)
        with self._lock:
            before = self.get(artifact_id)
            kind = before.get("kind")
            if kind == "SQLITE_BACKUP":
                if (
                    before.get("promoted_from_kind") != "RESTORE_UPLOAD"
                    and before.get("local_database_registration") is not True
                ):
                    _raise("INVALID_ARTIFACT", "registered artifact is not a promoted restore upload")
                path = self._path_from_entry(before)
                verification = _fresh_backup_verification(path)
                current = self.get(artifact_id)
                if current != before:
                    _raise("ARTIFACT_BINDING_CHANGED", "restore artifact receipt changed during verification")
                for field in ("artifact_format", "storage_contract_version", "embedding_profile"):
                    if before.get(field) != verification.get(field):
                        _raise("BACKUP_VALIDATION_FAILED", "promoted restore metadata no longer matches the verified image")
                return dict(before)
            if kind != "RESTORE_UPLOAD":
                _raise("INVALID_ARTIFACT", "restore promotion requires a RESTORE_UPLOAD artifact")

            path = self._path_from_entry(before)
            verification = _fresh_backup_verification(path)
            current = self.get(artifact_id)
            if current != before:
                _raise("ARTIFACT_BINDING_CHANGED", "restore artifact receipt changed during verification")
            entries = self._load()
            if entries.get(artifact_id) != current:
                _raise("ARTIFACT_BINDING_CHANGED", "restore artifact receipt changed before promotion")
            promoted = dict(current)
            promoted.update(
                {
                    "kind": "SQLITE_BACKUP",
                    "promoted_from_kind": "RESTORE_UPLOAD",
                    "artifact_format": verification["artifact_format"],
                    "sqlite_runtime_version": verification["sqlite_runtime_version"],
                    "storage_contract_version": verification["storage_contract_version"],
                    "embedding_profile": verification["embedding_profile"],
                    "verification": {
                        "fresh_reopen": verification["fresh_reopen"],
                        "readable": verification["readable"],
                        "integrity_check": verification["integrity_check"],
                        "verified_at": verification["verified_at"],
                    },
                }
            )
            entries[artifact_id] = promoted
            self._save(entries)
            return dict(promoted)


def _sqlite_uri(path: Path) -> str:
    return "file:" + urllib.parse.quote(str(path), safe="/") + "?mode=ro"


def _backup_metadata(conn: sqlite3.Connection) -> dict[str, Any]:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adapter_meta'"
    ).fetchone()
    contract_version: int | None = None
    embedding_profile: Any = None
    if has_meta is not None:
        row = conn.execute("SELECT value FROM adapter_meta WHERE key='contract_version'").fetchone()
        if row is not None:
            try:
                contract_version = int(row[0])
            except (TypeError, ValueError):
                _raise("BACKUP_VALIDATION_FAILED", "storage contract metadata is malformed")
        row = conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone()
        if row is not None:
            try:
                embedding_profile = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                _raise("BACKUP_VALIDATION_FAILED", "embedding profile metadata is malformed")
    return {
        "artifact_format": "sqlite3",
        "sqlite_runtime_version": sqlite3.sqlite_version,
        "storage_contract_version": contract_version,
        "embedding_profile": embedding_profile,
    }


def _fresh_backup_verification(path: Path) -> dict[str, Any]:
    """Freshly reopen one backup read-only and prove SQLite readability/integrity."""

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(_sqlite_uri(path), uri=True, timeout=5.0)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            _raise("BACKUP_VALIDATION_FAILED", "backup integrity verification failed")
        metadata = _backup_metadata(conn)
    except AdapterError:
        raise
    except sqlite3.Error as exc:
        raise AdapterError("BACKUP_VALIDATION_FAILED", "backup could not be reopened and verified") from exc
    finally:
        if conn is not None:
            conn.close()
    return {
        "fresh_reopen": True,
        "readable": True,
        "integrity_check": "ok",
        "verified_at": iso_now(),
        **metadata,
    }


def verify_registered_backup(registry: ArtifactRegistry, artifact_id: str) -> dict[str, Any]:
    """Freshly verify one registered SQLite backup by identity, never by caller path."""

    entry = registry.get(artifact_id)
    if entry.get("kind") != "SQLITE_BACKUP":
        _raise("INVALID_ARTIFACT", "registered artifact is not a SQLite backup")
    path = registry._path_from_entry(entry)
    verification = _fresh_backup_verification(path)
    return {
        "artifact_id": entry["artifact_id"],
        "sha256": entry["sha256"],
        "size_bytes": entry["size_bytes"],
        "kind": entry["kind"],
        **verification,
    }


def create_consistent_backup(
    source_db: str | os.PathLike[str],
    registry: ArtifactRegistry,
    *,
    artifact_id: str | None = None,
    metadata_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create and verify one SQLite-aware snapshot, including committed WAL state."""

    artifact_id = _validate_artifact_id(artifact_id or str(uuid.uuid4()))
    source = Path(source_db).resolve(strict=True)
    if not source.is_file():
        _raise("BACKUP_SOURCE_INVALID", "active database is not a regular file")
    destination = registry.managed_path(f"backup-{artifact_id}.sqlite3")
    if destination.exists() or destination.is_symlink():
        _raise("ARTIFACT_ALREADY_EXISTS", "backup destination already exists")

    fd, parent_guard = _open_new_private_file(
        destination,
        os.O_WRONLY,
        code="ARTIFACT_PATH_INVALID",
        message="backup destination could not be protected for the current user",
    )
    os.close(fd)
    source_conn: sqlite3.Connection | None = None
    backup_conn: sqlite3.Connection | None = None
    metadata: dict[str, Any] = {}
    try:
        try:
            source_conn = sqlite3.connect(_sqlite_uri(source), uri=True, timeout=5.0)
            backup_conn = sqlite3.connect(destination, timeout=5.0)
            source_conn.backup(backup_conn)
            backup_conn.commit()
            integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                _raise("BACKUP_VALIDATION_FAILED", "backup integrity verification failed")
            metadata = _backup_metadata(backup_conn)
        except sqlite3.Error as exc:
            _unlink_private_file(destination, parent_guard=parent_guard)
            raise AdapterError("BACKUP_FAILED", "SQLite backup could not be completed") from exc
        except Exception:
            _unlink_private_file(destination, parent_guard=parent_guard)
            raise
        finally:
            if backup_conn is not None:
                backup_conn.close()
            if source_conn is not None:
                source_conn.close()

        try:
            if os.name == "nt":
                if parent_guard is not None:
                    parent_guard.require_current()
                _validate_private_regular_file(
                    destination,
                    code="ARTIFACT_PATH_INVALID",
                    message="backup destination is not owner-controlled",
                )
            else:
                os.chmod(destination, 0o600)
            verification = _fresh_backup_verification(destination)
            for field in ("artifact_format", "sqlite_runtime_version", "storage_contract_version", "embedding_profile"):
                if metadata.get(field) != verification.get(field):
                    _raise("BACKUP_VALIDATION_FAILED", "freshly reopened backup metadata does not match the created snapshot")
            metadata["verification"] = {
                "fresh_reopen": verification["fresh_reopen"],
                "readable": verification["readable"],
                "integrity_check": verification["integrity_check"],
                "verified_at": verification["verified_at"],
            }
            if metadata_extra is not None:
                if not isinstance(metadata_extra, dict):
                    _raise("INVALID_ARTIFACT", "backup metadata extension must be an object")
                reserved = {
                    "artifact_id",
                    "kind",
                    "relative_path",
                    "size_bytes",
                    "sha256",
                    "registered_at",
                }
                if reserved.intersection(metadata_extra) or set(metadata).intersection(metadata_extra):
                    _raise("INVALID_ARTIFACT", "backup metadata extension cannot replace verified receipt fields")
                metadata.update(metadata_extra)
            return registry.register(
                destination,
                kind="SQLITE_BACKUP",
                artifact_id=artifact_id,
                metadata=metadata,
            )
        except Exception:
            _unlink_private_file(destination, parent_guard=parent_guard)
            raise
    finally:
        if parent_guard is not None:
            parent_guard.close()


def _decode_json_field(value: Any, field: str) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise AdapterError("LOGICAL_EXPORT_INVALID", f"{field} is not valid stored JSON") from exc


def _normalize_timestamp(value: Any, field: str, *, nullable: bool) -> str | None:
    if value is None:
        if nullable:
            return None
        _raise("LOGICAL_IMPORT_INVALID", f"{field} must be a timestamp")
    if not isinstance(value, str):
        _raise("LOGICAL_IMPORT_INVALID", f"{field} must be an ISO-8601 string")
    try:
        parsed = parse_iso(value, required=True)
    except AdapterError as exc:
        raise AdapterError("LOGICAL_IMPORT_INVALID", f"{field} must include a valid UTC offset") from exc
    assert parsed is not None
    return parsed.isoformat().replace("+00:00", "Z")


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        _raise("LOGICAL_IMPORT_INVALID", f"{field} must be a canonical UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise AdapterError("LOGICAL_IMPORT_INVALID", f"{field} must be a canonical UUID string") from exc
    canonical = str(parsed)
    if canonical != value:
        _raise("LOGICAL_IMPORT_INVALID", f"{field} must use canonical UUID spelling")
    return canonical


def _bounded_identifier(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        _raise("LOGICAL_IMPORT_INVALID", f"{field} must be a non-empty bounded string")
    try:
        normalized = validate_identifier(value.strip(), field, required=True)
    except AdapterError as exc:
        raise AdapterError("LOGICAL_IMPORT_INVALID", f"{field} is not a supported identifier") from exc
    assert normalized is not None
    return normalized


def _normalize_scope(raw_scope: Any) -> dict[str, Any]:
    if not isinstance(raw_scope, dict):
        _raise("LOGICAL_IMPORT_INVALID", "scope must be an object")
    unknown = set(raw_scope) - _SCOPE_FIELDS
    if unknown:
        _raise("LOGICAL_IMPORT_INVALID", "scope contains unsupported identity fields")
    if "scope" not in raw_scope or not isinstance(raw_scope["scope"], str):
        _raise("LOGICAL_IMPORT_INVALID", "scope must contain its exact scope enum")
    scope_name = raw_scope["scope"]
    if scope_name == "CROSS_PROJECT_WORKFLOW":
        present = set(raw_scope).intersection(_SCOPE_IDENTITY_FIELDS)
        if present:
            _raise(
                "CROSS_SCOPE_DENIED",
                "CROSS_PROJECT_WORKFLOW identity keys must be absent, including null values",
            )
    scope = validate_scope(
        scope_name,
        raw_scope.get("project_id"),
        raw_scope.get("worktree_id"),
        raw_scope.get("workflow_id"),
        raw_scope.get("agent_id"),
    )
    normalized: dict[str, Any] = {"scope": scope.scope}
    for field in _SCOPE_IDENTITY_FIELDS:
        value = getattr(scope, field)
        if value is not None:
            normalized[field] = value
    return normalized


def _storage_scope(record: dict[str, Any]) -> dict[str, Any]:
    raw_scope = record.get("scope")
    if isinstance(raw_scope, dict):
        candidate = dict(raw_scope)
    else:
        candidate = {"scope": raw_scope}
        for field in _SCOPE_IDENTITY_FIELDS:
            if record.get(field) is not None:
                candidate[field] = record.get(field)
    return _normalize_scope(candidate)


def _wire_record_from_storage(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        _raise("LOGICAL_EXPORT_INVALID", "logical export records must be objects")
    lifecycle = record.get("lifecycle_state", record.get("source_lifecycle"))
    active = record.get("active", lifecycle in _ACTIVE_SOURCE_STATES)
    supersedes = _decode_json_field(
        record.get("supersedes", record.get("supersedes_source_memory_ids", [])),
        "supersedes",
    )
    return {
        "source_memory_id": record.get("memory_id", record.get("source_memory_id")),
        "source_record_id": record.get("record_id", record.get("source_record_id")),
        "source_revision": record.get("revision", record.get("source_revision")),
        "scope": _storage_scope(record),
        "memory_type": record.get("memory_type"),
        "fact": record.get("fact"),
        "tags": _decode_json_field(record.get("tags", []), "tags"),
        "source_provenance": _decode_json_field(record.get("source_provenance"), "source_provenance"),
        "freshness_policy": _decode_json_field(record.get("freshness_policy"), "freshness_policy"),
        "source_timestamp": record.get("source_timestamp"),
        "source_lifecycle": lifecycle,
        "source_verified_by": record.get("verified_by", record.get("source_verified_by")),
        "parent_source_record_id": record.get("parent_record_id", record.get("parent_source_record_id")),
        "supersedes_source_memory_ids": supersedes,
        "active": active,
    }


def build_logical_export(
    records: list[dict[str, Any]],
    *,
    factlane_version: str,
    storage_contract_version: int,
    embedding_profile_id: str,
    exported_at: str | None = None,
) -> bytes:
    if not isinstance(records, list):
        _raise("LOGICAL_EXPORT_INVALID", "logical export records must be an array")
    if len(records) > LOGICAL_IMPORT_MAX_RECORDS:
        _raise("LOGICAL_IMPORT_LIMIT", "logical interchange supports at most 1000 records")
    document = {
        "format": LOGICAL_FORMAT,
        "exported_at": exported_at or iso_now(),
        "producer": {
            "factlane_version": factlane_version,
            "storage_contract_version": storage_contract_version,
            "embedding_profile_id": embedding_profile_id,
        },
        "records": [_wire_record_from_storage(record) for record in records],
    }
    payload = canonical_json(document).encode("utf-8")
    if len(payload) > LOGICAL_IMPORT_MAX_BYTES:
        _raise("LOGICAL_IMPORT_LIMIT", "logical interchange exceeds the 10 MiB limit")
    normalized = validate_logical_document(payload)
    return canonical_json(normalized).encode("utf-8")


def create_registered_logical_export(
    records: list[dict[str, Any]],
    registry: ArtifactRegistry,
    *,
    factlane_version: str,
    storage_contract_version: int,
    embedding_profile_id: str,
    artifact_id: str | None = None,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """Create one registered owner-private logical JSON artifact from explicit records."""

    artifact_id = _validate_artifact_id(artifact_id or str(uuid.uuid4()))
    payload = build_logical_export(
        records,
        factlane_version=factlane_version,
        storage_contract_version=storage_contract_version,
        embedding_profile_id=embedding_profile_id,
        exported_at=exported_at,
    )
    document = validate_logical_document(payload)
    _scope_digest, exact_scopes = _scope_identity_digest(document["records"])
    destination = registry.managed_path(f"export-{artifact_id}.json")
    if destination.exists() or destination.is_symlink():
        _raise("ARTIFACT_ALREADY_EXISTS", "logical export destination already exists")
    fd, parent_guard = _open_new_private_file(
        destination,
        os.O_WRONLY,
        code="ARTIFACT_PATH_INVALID",
        message="logical export destination could not be protected for the current user",
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            _flush_private_file(handle.fileno())
        if os.name == "nt":
            if parent_guard is not None:
                parent_guard.require_current()
            _validate_private_regular_file(
                destination,
                code="ARTIFACT_PATH_INVALID",
                message="logical export destination is not owner-controlled",
            )
        else:
            os.chmod(destination, 0o600)
        return registry.register(
            destination,
            kind="LOGICAL_EXPORT",
            artifact_id=artifact_id,
            metadata={
                "logical_format": LOGICAL_FORMAT,
                "record_count": len(document["records"]),
                "exported_at": document["exported_at"],
                "producer": document["producer"],
                "exact_scopes": exact_scopes,
            },
        )
    except Exception:
        _unlink_private_file(destination, parent_guard=parent_guard)
        raise
    finally:
        if parent_guard is not None:
            parent_guard.close()


def _strict_json(payload: bytes) -> Any:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdapterError("LOGICAL_IMPORT_INVALID", "logical import must be UTF-8 JSON") from exc

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _raise("LOGICAL_IMPORT_INVALID", f"duplicate JSON key is not allowed: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        _raise("LOGICAL_IMPORT_INVALID", f"non-finite JSON number is not allowed: {value}")

    try:
        return json.loads(text, object_pairs_hook=pairs_hook, parse_constant=reject_constant)
    except AdapterError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise AdapterError("LOGICAL_IMPORT_INVALID", "logical import is malformed JSON") from exc


def _validate_producer(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PRODUCER_FIELDS:
        _raise("LOGICAL_IMPORT_INVALID", "producer must contain exactly the supported fields")
    contract_version = value["storage_contract_version"]
    if isinstance(contract_version, bool) or not isinstance(contract_version, int) or contract_version < 1:
        _raise("LOGICAL_IMPORT_INVALID", "producer.storage_contract_version must be a positive integer")
    return {
        "factlane_version": _bounded_identifier(value["factlane_version"], "producer.factlane_version", 64),
        "storage_contract_version": contract_version,
        "embedding_profile_id": _bounded_identifier(value["embedding_profile_id"], "producer.embedding_profile_id", 128),
    }


def _validate_logical_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _LOGICAL_RECORD_FIELDS:
        _raise("LOGICAL_IMPORT_INVALID", "logical record fields are not exact")
    source_memory_id = _canonical_uuid(value["source_memory_id"], "source_memory_id")
    source_record_id = _canonical_uuid(value["source_record_id"], "source_record_id")
    revision = value["source_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        _raise("LOGICAL_IMPORT_INVALID", "source_revision must be a positive integer")

    scope = _normalize_scope(value["scope"])
    memory_type = value["memory_type"]
    if memory_type not in MEMORY_TYPES:
        _raise("LOGICAL_IMPORT_INVALID", "memory_type is not supported")
    if scope["scope"] == "CROSS_PROJECT_WORKFLOW" and memory_type not in {"WORKFLOW_RULE", "DECISION_RATIONALE"}:
        _raise("SCOPE_TYPE_MISMATCH", "memory_type is incompatible with CROSS_PROJECT_WORKFLOW")

    fact = validate_fact(value["fact"])
    tags = MemoryAdapter._tags(value["tags"])
    provenance = validate_provenance(value["source_provenance"])
    freshness = validate_freshness(value["freshness_policy"])
    MemoryAdapter._validate_on_change_freshness(provenance, freshness)
    if scope["scope"] == "CROSS_PROJECT_WORKFLOW":
        MemoryAdapter._validate_cross_project_freshness(provenance, freshness)

    timestamp = _normalize_timestamp(value["source_timestamp"], "source_timestamp", nullable=True)
    lifecycle = value["source_lifecycle"]
    if lifecycle not in LIFECYCLE_STATES:
        _raise("LOGICAL_IMPORT_INVALID", "source_lifecycle is not supported")
    verified_by = value["source_verified_by"]
    if verified_by not in VERIFIED_BY:
        _raise("LOGICAL_IMPORT_INVALID", "source_verified_by is not supported")
    if timestamp is None and verified_by != "UNVERIFIED":
        _raise("LOGICAL_IMPORT_INVALID", "source_timestamp may be null only for an unverified source")

    parent = value["parent_source_record_id"]
    if parent is not None:
        parent = _canonical_uuid(parent, "parent_source_record_id")
        if parent == source_record_id:
            _raise("LOGICAL_IMPORT_INVALID", "record lineage cannot point to itself")
        if revision == 1:
            _raise("LOGICAL_IMPORT_INVALID", "revision 1 cannot assert a parent record")

    supersedes = value["supersedes_source_memory_ids"]
    if not isinstance(supersedes, list):
        _raise("LOGICAL_IMPORT_INVALID", "supersedes_source_memory_ids must be an array")
    if len(supersedes) > LOGICAL_IMPORT_MAX_RECORDS:
        _raise("LOGICAL_IMPORT_LIMIT", "supersedes_source_memory_ids exceeds 1000 entries")
    normalized_supersedes: list[str] = []
    for item in supersedes:
        normalized = _canonical_uuid(item, "supersedes_source_memory_ids item")
        if normalized == source_memory_id:
            _raise("LOGICAL_IMPORT_INVALID", "a memory cannot supersede its own source identity")
        if normalized in normalized_supersedes:
            _raise("LOGICAL_IMPORT_INVALID", "supersedes_source_memory_ids must be unique")
        normalized_supersedes.append(normalized)

    active = value["active"]
    if not isinstance(active, bool):
        _raise("LOGICAL_IMPORT_INVALID", "active must be a JSON boolean")
    if active and lifecycle not in _ACTIVE_SOURCE_STATES:
        _raise("LOGICAL_IMPORT_INVALID", "active can be true only for source Candidate/current records")

    return {
        "source_memory_id": source_memory_id,
        "source_record_id": source_record_id,
        "source_revision": revision,
        "scope": scope,
        "memory_type": memory_type,
        "fact": fact,
        "tags": tags,
        "source_provenance": provenance,
        "freshness_policy": freshness,
        "source_timestamp": timestamp,
        "source_lifecycle": lifecycle,
        "source_verified_by": verified_by,
        "parent_source_record_id": parent,
        "supersedes_source_memory_ids": normalized_supersedes,
        "active": active,
    }


def validate_logical_document(payload: bytes | bytearray) -> dict[str, Any]:
    if not isinstance(payload, (bytes, bytearray)):
        _raise("LOGICAL_IMPORT_INVALID", "logical import must be supplied as UTF-8 bytes")
    raw = bytes(payload)
    if len(raw) > LOGICAL_IMPORT_MAX_BYTES:
        _raise("LOGICAL_IMPORT_LIMIT", "logical import exceeds the 10 MiB limit")
    value = _strict_json(raw)
    if not isinstance(value, dict) or set(value) != _LOGICAL_ENVELOPE_FIELDS:
        _raise("LOGICAL_IMPORT_INVALID", "logical import envelope fields are not exact")
    if value["format"] != LOGICAL_FORMAT:
        _raise("LOGICAL_IMPORT_FORMAT", f"logical import format must be {LOGICAL_FORMAT}")
    exported_at = _normalize_timestamp(value["exported_at"], "exported_at", nullable=False)
    producer = _validate_producer(value["producer"])
    records = value["records"]
    if not isinstance(records, list):
        _raise("LOGICAL_IMPORT_INVALID", "records must be an array")
    if len(records) > LOGICAL_IMPORT_MAX_RECORDS:
        _raise("LOGICAL_IMPORT_LIMIT", "logical import supports at most 1000 record entries")

    normalized_records: list[dict[str, Any]] = []
    record_ids: set[str] = set()
    memory_revisions: set[tuple[str, int]] = set()
    for record in records:
        normalized = _validate_logical_record(record)
        record_id = normalized["source_record_id"]
        if record_id in record_ids:
            _raise("LOGICAL_IMPORT_INVALID", "duplicate source_record_id is not allowed")
        record_ids.add(record_id)
        memory_revision = (normalized["source_memory_id"], normalized["source_revision"])
        if memory_revision in memory_revisions:
            _raise("LOGICAL_IMPORT_INVALID", "duplicate source memory revision is not allowed")
        memory_revisions.add(memory_revision)
        normalized_records.append(normalized)

    by_record_id = {record["source_record_id"]: record for record in normalized_records}
    for record in normalized_records:
        parent_id = record["parent_source_record_id"]
        if parent_id is None or parent_id not in by_record_id:
            continue
        parent = by_record_id[parent_id]
        if parent["source_memory_id"] != record["source_memory_id"]:
            _raise("LOGICAL_IMPORT_INVALID", "included parent record belongs to a different source memory")
        if parent["source_revision"] + 1 != record["source_revision"]:
            _raise("LOGICAL_IMPORT_INVALID", "included parent record has a contradictory revision")

    return {
        "format": LOGICAL_FORMAT,
        "exported_at": exported_at,
        "producer": producer,
        "records": normalized_records,
    }


def _scope_identity_digest(records: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    unique = {canonical_json(record["scope"]): record["scope"] for record in records}
    identities = [unique[key] for key in sorted(unique)]
    digest = hashlib.sha256(canonical_json(identities).encode("utf-8")).hexdigest()
    return digest, identities


def preview_logical_import(payload: bytes | bytearray) -> dict[str, Any]:
    raw = bytes(payload)
    document = validate_logical_document(raw)
    records = document["records"]
    scope_digest_value, scopes = _scope_identity_digest(records)
    candidate_admissions: list[dict[str, Any]] = []
    external_history: list[dict[str, Any]] = []
    missing_lineage: list[dict[str, str]] = []
    present_record_ids = {record["source_record_id"] for record in records}

    for record in records:
        source_claim = {
            "source_memory_id": record["source_memory_id"],
            "source_record_id": record["source_record_id"],
            "source_revision": record["source_revision"],
            "source_lifecycle": record["source_lifecycle"],
            "source_verified_by": record["source_verified_by"],
            "source_provenance": record["source_provenance"],
            "freshness_policy": record["freshness_policy"],
            "source_timestamp": record["source_timestamp"],
            "parent_source_record_id": record["parent_source_record_id"],
            "supersedes_source_memory_ids": record["supersedes_source_memory_ids"],
        }
        parent = record["parent_source_record_id"]
        if parent is not None and parent not in present_record_ids:
            missing_lineage.append({"source_record_id": record["source_record_id"], "missing_parent_source_record_id": parent})
        if record["active"] and record["source_lifecycle"] in _ACTIVE_SOURCE_STATES:
            candidate_admissions.append(
                {
                    "source_record_id": record["source_record_id"],
                    "scope": record["scope"],
                    "memory_type": record["memory_type"],
                    "fact": record["fact"],
                    "tags": record["tags"],
                    "target_lifecycle": "CANDIDATE",
                    "target_verified_by": "UNVERIFIED",
                    "target_authority": "UNRESOLVED",
                    "external_source_claim": source_claim,
                }
            )
        else:
            external_history.append({"scope": record["scope"], "external_source_claim": source_claim})

    scope_counts: dict[str, int] = {}
    for record in records:
        key = canonical_json(record["scope"])
        scope_counts[key] = scope_counts.get(key, 0) + 1

    return {
        "format": LOGICAL_FORMAT,
        "file_digest": hashlib.sha256(raw).hexdigest(),
        "exported_at": document["exported_at"],
        "producer": document["producer"],
        "record_count": len(records),
        "candidate_admission_count": len(candidate_admissions),
        "external_history_count": len(external_history),
        "candidate_admissions": candidate_admissions,
        "external_history": external_history,
        "missing_lineage": missing_lineage,
        "exact_scopes": scopes,
        "scope_counts": scope_counts,
        "scope_identity_digest": scope_digest_value,
        "source_approval_policy": "EXTERNAL_CLAIM_ONLY",
        "destination_analysis": "PENDING_DESTINATION_PROBE",
        "destination_duplicates": [],
        "destination_conflicts": [],
    }


def _destination_record_scope(record: dict[str, Any]) -> dict[str, Any]:
    raw_scope = record.get("scope")
    if isinstance(raw_scope, dict):
        candidate = {
            key: value
            for key, value in raw_scope.items()
            if key == "scope" or value is not None
        }
    else:
        candidate = {"scope": raw_scope}
        for field in _SCOPE_IDENTITY_FIELDS:
            if record.get(field) is not None:
                candidate[field] = record.get(field)
    return _normalize_scope(candidate)


def _normalized_fact(value: Any) -> str:
    if not isinstance(value, str):
        _raise("DESTINATION_PROBE_INVALID", "destination probe record fact must be a string")
    return " ".join(value.casefold().split())


def _probe_request(candidate: dict[str, Any]) -> dict[str, Any]:
    scope = candidate["scope"]
    scope_context = validate_scope(
        scope["scope"],
        scope.get("project_id"),
        scope.get("worktree_id"),
        scope.get("workflow_id"),
        scope.get("agent_id"),
    )
    subject = MemoryAdapter._subject(None, candidate["tags"], candidate["fact"])
    return {
        "source_record_id": candidate["source_record_id"],
        "scope": dict(scope),
        "memory_type": candidate["memory_type"],
        "fact": candidate["fact"],
        "subject": subject,
        "native_content_hash": native_hash(scope_context, candidate["fact"]),
        "contradiction_key": MemoryAdapter._contradiction_identity_key(
            scope_context,
            candidate["memory_type"],
            subject,
        ),
        "mutation_allowed": False,
    }


def _validate_probe_record_scope(record: dict[str, Any], expected_scope: dict[str, Any]) -> None:
    if _destination_record_scope(record) != expected_scope:
        _raise("DESTINATION_PROBE_INVALID", "destination probe returned a record outside the candidate exact scope")


async def probe_logical_import_destination(
    payload: bytes | bytearray,
    *,
    destination_probe: DestinationProbeCallback,
) -> dict[str, Any]:
    """Augment logical-import preview with read-only exact-scope destination classification.

    The injected probe receives the already-normalized exact scope plus the same
    native-content and contradiction identities used by ordinary store semantics.
    It must return {"exact": record-or-None, "contradictions": [records...]}; this
    layer rejects any result that crosses the candidate's exact scope.
    """

    preview = preview_logical_import(payload)
    results: list[dict[str, Any]] = []
    duplicates: list[str] = []
    conflicts: list[str] = []
    summary = {"EXACT_DUPLICATE": 0, "EQUIVALENT": 0, "CONFLICT": 0, "NEW": 0}

    for candidate in preview["candidate_admissions"]:
        request = _probe_request(candidate)
        response = await _await_if_needed(destination_probe(dict(request)))
        if not isinstance(response, dict) or set(response) != {"exact", "contradictions"}:
            _raise("DESTINATION_PROBE_INVALID", "destination probe returned an invalid result envelope")
        exact = response["exact"]
        contradiction_rows = response["contradictions"]
        if exact is not None and not isinstance(exact, dict):
            _raise("DESTINATION_PROBE_INVALID", "destination probe exact result must be an object or null")
        if not isinstance(contradiction_rows, list) or not all(isinstance(row, dict) for row in contradiction_rows):
            _raise("DESTINATION_PROBE_INVALID", "destination probe contradictions must be a list of records")

        match_ids: list[str] = []
        if exact is not None:
            _validate_probe_record_scope(exact, request["scope"])
            if exact.get("native_content_hash") != request["native_content_hash"]:
                _raise("DESTINATION_PROBE_INVALID", "destination probe exact record does not match the requested content identity")
            if isinstance(exact.get("record_id"), str):
                match_ids.append(exact["record_id"])

        incompatible = False
        equivalent = False
        candidate_fact = _normalized_fact(request["fact"])
        for row in contradiction_rows:
            _validate_probe_record_scope(row, request["scope"])
            if row.get("contradiction_key") != request["contradiction_key"]:
                _raise("DESTINATION_PROBE_INVALID", "destination probe returned a record outside the requested contradiction identity")
            row_fact = _normalized_fact(row.get("fact"))
            equivalent = equivalent or row_fact == candidate_fact
            incompatible = incompatible or row_fact != candidate_fact
            if isinstance(row.get("record_id"), str):
                match_ids.append(row["record_id"])

        if exact is not None:
            classification = "EXACT_DUPLICATE"
            duplicates.append(candidate["source_record_id"])
        elif incompatible:
            classification = "CONFLICT"
            conflicts.append(candidate["source_record_id"])
        elif equivalent:
            classification = "EQUIVALENT"
            duplicates.append(candidate["source_record_id"])
        else:
            classification = "NEW"
        summary[classification] += 1
        results.append(
            {
                "source_record_id": candidate["source_record_id"],
                "scope": dict(request["scope"]),
                "classification": classification,
                "destination_record_ids": match_ids,
            }
        )

    return {
        **preview,
        "destination_analysis": "COMPLETE",
        "destination_results": results,
        "destination_summary": summary,
        "destination_duplicates": duplicates,
        "destination_conflicts": conflicts,
    }


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _import_binding(preview: dict[str, Any], operation_id: str) -> dict[str, str]:
    operation = validate_identifier(operation_id, "operation_id", required=True)
    assert operation is not None
    identity = {
        "operation_id": operation,
        "file_digest": preview["file_digest"],
        "scope_identity_digest": preview["scope_identity_digest"],
    }
    identity["idempotency_key"] = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return identity


async def apply_logical_import(
    payload: bytes | bytearray,
    *,
    operation_id: str,
    vector_prepare: VectorPrepareCallback,
    candidate_admit: CandidateAdmissionCallback,
) -> dict[str, Any]:
    """Prepare a logical import and delegate one atomic Candidate-admission batch.

    This layer never restores source approval. The injected Candidate callback owns
    the destination transaction and durable replay ledger. It receives one binding
    derived from operation id, raw-file digest, and exact normalized scope identities;
    callers must use that idempotency key for the all-or-none transaction.
    """

    preview = preview_logical_import(payload)
    binding = _import_binding(preview, operation_id)
    source_admissions = preview["candidate_admissions"]
    if not source_admissions:
        return {
            "status": "OK",
            "admitted": 0,
            "idempotent_replay": False,
            **binding,
        }

    admissions: list[dict[str, Any]] = []
    for item in source_admissions:
        admissions.append(
            {
                **item,
                "source_provenance": {
                    "source_class": "LOGICAL_IMPORT",
                    "source_ref": f"{LOGICAL_FORMAT}:{item['source_record_id']}",
                    "source_hash": binding["file_digest"],
                    "review_ref": f"operation:{binding['operation_id']}",
                    "extraction_method": LOGICAL_FORMAT,
                },
                "freshness_policy": {"kind": "manual"},
                "source_timestamp": preview["exported_at"],
            }
        )

    prepared = await _await_if_needed(vector_prepare(admissions, dict(binding)))
    if isinstance(prepared, dict):
        expected = {item["source_record_id"] for item in admissions}
        vector_map = prepared.get("vectors") if set(prepared) == {"profile", "vectors"} else prepared
        if not isinstance(vector_map, dict) or set(vector_map) != expected:
            _raise("IMPORT_VECTOR_PREPARATION_INVALID", "prepared vectors do not match the complete admission batch")

    result = await _await_if_needed(candidate_admit(admissions, prepared, dict(binding)))
    if not isinstance(result, dict):
        _raise("IMPORT_ACTIVATION_INVALID", "Candidate admission callback returned an invalid result")
    if "idempotency_key" in result and result["idempotency_key"] != binding["idempotency_key"]:
        _raise("IDEMPOTENCY_CONFLICT", "Candidate admission result is bound to a different import identity")
    return {**result, **binding}


async def apply_logical_import_to_adapter(
    payload: bytes | bytearray,
    *,
    operation_id: str,
    adapter: MemoryAdapter,
) -> dict[str, Any]:
    """Production logical-import path using the governed adapter batch primitive."""

    preview = preview_logical_import(payload)
    source_records = validate_logical_document(bytes(payload))["records"]
    binding = _import_binding(preview, operation_id)
    replay = await adapter.logical_import_candidate_batch_replay(dict(binding))
    if replay is not None:
        return {**replay, **binding}

    async def prepare(
        admissions: list[dict[str, Any]],
        _binding: dict[str, str],
    ) -> dict[str, Any]:
        return await adapter.prepare_logical_import_candidate_vectors(admissions)

    async def admit(
        admissions: list[dict[str, Any]],
        prepared: dict[str, Any],
        binding: dict[str, str],
    ) -> dict[str, Any]:
        return await adapter.admit_logical_import_candidate_batch(
            admissions,
            prepared,
            binding,
            source_records=source_records,
        )

    return await apply_logical_import(
        payload,
        operation_id=operation_id,
        vector_prepare=prepare,
        candidate_admit=admit,
    )


async def retrieve_logical_import_evidence(
    *,
    adapter: MemoryAdapter,
    operation_id: str,
) -> dict[str, Any]:
    """Retrieve durable non-authoritative evidence for one logical-import operation."""

    return await adapter.retrieve_logical_import_evidence(operation_id)
