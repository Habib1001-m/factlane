from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_PIPE_PREFIX = "\\\\.\\pipe\\"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class WindowsSecurityError(RuntimeError):
    """Fail-closed Windows owner-identity or custody failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(frozen=True)
class WindowsProcessIdentity:
    pid: int
    sid: str
    creation_time_us: int


def _require_windows() -> tuple[Any, Any, Any, Any, Any]:
    if os.name != "nt":
        raise WindowsSecurityError(
            "WINDOWS_SECURITY_UNAVAILABLE",
            "Windows security primitives are unavailable on this platform",
        )
    try:
        import pywintypes
        import win32api
        import win32con
        import win32process
        import win32security
    except ImportError as exc:  # pragma: no cover - native-Windows packaging guard
        raise WindowsSecurityError(
            "WINDOWS_SECURITY_UNAVAILABLE",
            "pywin32 security primitives are unavailable",
        ) from exc
    return pywintypes, win32api, win32con, win32process, win32security


def _close_handle(handle: Any) -> None:
    close = getattr(handle, "Close", None)
    if callable(close):
        close()


def _sid_string(win32security: Any, sid: Any) -> str:
    try:
        value = win32security.ConvertSidToStringSid(sid)
    except Exception as exc:
        raise WindowsSecurityError("WINDOWS_SID_UNAVAILABLE", "Windows SID could not be resolved") from exc
    if not isinstance(value, str) or not value.startswith("S-"):
        raise WindowsSecurityError("WINDOWS_SID_UNAVAILABLE", "Windows SID is invalid")
    return value


def current_user_sid() -> str:
    _pywintypes, win32api, win32con, _win32process, win32security = _require_windows()
    token = None
    try:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        return _sid_string(win32security, sid)
    except WindowsSecurityError:
        raise
    except Exception as exc:
        raise WindowsSecurityError("WINDOWS_SID_UNAVAILABLE", "current-user SID could not be resolved") from exc
    finally:
        if token is not None:
            _close_handle(token)


def process_identity(pid: int) -> WindowsProcessIdentity:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    _pywintypes, win32api, win32con, win32process, win32security = _require_windows()
    process = None
    token = None
    try:
        access = getattr(win32con, "PROCESS_QUERY_LIMITED_INFORMATION", _PROCESS_QUERY_LIMITED_INFORMATION)
        process = win32api.OpenProcess(access, False, pid)
        token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
        sid = _sid_string(
            win32security,
            win32security.GetTokenInformation(token, win32security.TokenUser)[0],
        )
        times = win32process.GetProcessTimes(process)
        creation = times.get("CreationTime") if isinstance(times, dict) else None
        timestamp = getattr(creation, "timestamp", None)
        if creation is None or not callable(timestamp):
            raise WindowsSecurityError(
                "WINDOWS_PROCESS_IDENTITY_UNAVAILABLE",
                "Windows process creation identity is unavailable",
            )
        creation_time_us = int(round(float(timestamp()) * 1_000_000))
        if creation_time_us <= 0:
            raise WindowsSecurityError(
                "WINDOWS_PROCESS_IDENTITY_UNAVAILABLE",
                "Windows process creation identity is invalid",
            )
        return WindowsProcessIdentity(pid=pid, sid=sid, creation_time_us=creation_time_us)
    except WindowsSecurityError:
        raise
    except Exception as exc:
        raise WindowsSecurityError(
            "WINDOWS_PROCESS_IDENTITY_UNAVAILABLE",
            "Windows process identity could not be verified",
        ) from exc
    finally:
        if token is not None:
            _close_handle(token)
        if process is not None:
            _close_handle(process)


def require_process_identity(
    pid: int,
    *,
    owner_sid: str,
    creation_time_us: int | None = None,
) -> WindowsProcessIdentity:
    identity = process_identity(pid)
    if identity.sid.casefold() != owner_sid.casefold():
        raise WindowsSecurityError(
            "WINDOWS_PROCESS_SID_MISMATCH",
            "Windows process belongs to a different user",
        )
    if creation_time_us is not None and identity.creation_time_us != creation_time_us:
        raise WindowsSecurityError(
            "WINDOWS_PROCESS_IDENTITY_MISMATCH",
            "Windows process creation identity changed",
        )
    return identity


def owner_only_security_attributes(owner_sid: str) -> Any:
    pywintypes, _win32api, _win32con, _win32process, win32security = _require_windows()
    if not isinstance(owner_sid, str) or not owner_sid.startswith("S-"):
        raise ValueError("owner_sid must be a SID string")
    try:
        descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            f"O:{owner_sid}D:P(A;;GA;;;{owner_sid})",
            win32security.SDDL_REVISION_1,
        )
        attributes = pywintypes.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = descriptor
        attributes.bInheritHandle = False
        return attributes
    except Exception as exc:
        raise WindowsSecurityError(
            "WINDOWS_DACL_UNAVAILABLE",
            "owner-only Windows security descriptor could not be created",
        ) from exc


def _validate_descriptor(descriptor: Any, owner_sid: str, win32security: Any) -> None:
    try:
        owner = descriptor.GetSecurityDescriptorOwner()
        dacl = descriptor.GetSecurityDescriptorDacl()
        control, _revision = descriptor.GetSecurityDescriptorControl()
    except Exception as exc:
        raise WindowsSecurityError("WINDOWS_DACL_UNAVAILABLE", "Windows DACL could not be inspected") from exc
    if owner is None or _sid_string(win32security, owner).casefold() != owner_sid.casefold():
        raise WindowsSecurityError("WINDOWS_OWNER_MISMATCH", "Windows object owner SID is not the expected Owner")
    protected_flag = getattr(win32security, "SE_DACL_PROTECTED", 0x1000)
    if not control & protected_flag or dacl is None:
        raise WindowsSecurityError("WINDOWS_DACL_UNSAFE", "Windows object DACL is not protected owner-only state")
    try:
        ace_count = dacl.GetAceCount()
    except Exception as exc:
        raise WindowsSecurityError("WINDOWS_DACL_UNAVAILABLE", "Windows DACL could not be enumerated") from exc
    if ace_count != 1:
        raise WindowsSecurityError("WINDOWS_DACL_UNSAFE", "Windows object DACL contains unexpected principals")
    try:
        header, _mask, ace_sid = dacl.GetAce(0)
        ace_type = header[0]
    except Exception as exc:
        raise WindowsSecurityError("WINDOWS_DACL_UNAVAILABLE", "Windows DACL ACE could not be inspected") from exc
    if ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE:
        raise WindowsSecurityError("WINDOWS_DACL_UNSAFE", "Windows object DACL does not grant Owner access explicitly")
    if _sid_string(win32security, ace_sid).casefold() != owner_sid.casefold():
        raise WindowsSecurityError("WINDOWS_DACL_UNSAFE", "Windows object DACL grants access to another principal")


def validate_owner_only_kernel_object(handle: Any, owner_sid: str) -> None:
    _pywintypes, _win32api, _win32con, _win32process, win32security = _require_windows()
    try:
        descriptor = win32security.GetSecurityInfo(
            handle,
            win32security.SE_KERNEL_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )
    except Exception as exc:
        raise WindowsSecurityError("WINDOWS_DACL_UNAVAILABLE", "Windows kernel-object DACL could not be read") from exc
    _validate_descriptor(descriptor, owner_sid, win32security)


def _path_has_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise WindowsSecurityError("WINDOWS_PATH_UNAVAILABLE", "Windows path identity is unavailable") from exc
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def validate_windows_path_no_reparse(path: str | os.PathLike[str]) -> Path:
    """Reject any existing reparse component in one absolute local-drive path.

    This is deliberately lexical: resolving the path first would erase the exact
    junction/symlink crossing that custody validation needs to detect.  Missing
    trailing components are allowed only after every existing ancestor has been
    checked, which makes this suitable for pre-creation validation.
    """

    _require_windows()
    target = Path(path)
    raw = os.fspath(target)
    if (
        not isinstance(raw, str)
        or not raw
        or "\x00" in raw
        or any(char in raw for char in ("\n", "\r"))
        or not target.is_absolute()
        or not target.drive
        or raw.startswith(("\\\\", "\\?\\", "\\.\\"))
    ):
        raise WindowsSecurityError(
            "WINDOWS_PATH_UNSAFE",
            "Windows owner-private path must be one absolute local-drive path",
        )

    current = Path(target.anchor)
    try:
        anchor_info = current.lstat()
    except OSError as exc:
        raise WindowsSecurityError(
            "WINDOWS_PATH_UNAVAILABLE",
            "Windows path anchor identity is unavailable",
        ) from exc
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if int(getattr(anchor_info, "st_file_attributes", 0)) & reparse_flag:
        raise WindowsSecurityError(
            "WINDOWS_PATH_REPARSE_DENIED",
            "Windows owner-private path contains a reparse point",
        )

    for component in target.parts[1:]:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise WindowsSecurityError(
                "WINDOWS_PATH_UNAVAILABLE",
                "Windows path component identity is unavailable",
            ) from exc
        if int(getattr(info, "st_file_attributes", 0)) & reparse_flag:
            raise WindowsSecurityError(
                "WINDOWS_PATH_REPARSE_DENIED",
                "Windows owner-private path contains a reparse point",
            )
    return target


def validate_owner_private_path(path: str | os.PathLike[str], *, owner_sid: str, directory: bool) -> None:
    _pywintypes, _win32api, _win32con, _win32process, win32security = _require_windows()
    target = validate_windows_path_no_reparse(path)
    if _path_has_reparse_point(target):
        raise WindowsSecurityError("WINDOWS_PATH_REPARSE_DENIED", "Windows owner-private path is a reparse point")
    if directory and not target.is_dir():
        raise WindowsSecurityError("WINDOWS_PATH_UNSAFE", "Windows owner-private directory is not a directory")
    if not directory and not target.is_file():
        raise WindowsSecurityError("WINDOWS_PATH_UNSAFE", "Windows owner-private file is not a regular file")
    try:
        descriptor = win32security.GetNamedSecurityInfo(
            str(target),
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )
    except Exception as exc:
        raise WindowsSecurityError("WINDOWS_DACL_UNAVAILABLE", "Windows path DACL could not be read") from exc
    _validate_descriptor(descriptor, owner_sid, win32security)


def protect_owner_private_path(path: str | os.PathLike[str], *, owner_sid: str, directory: bool) -> None:
    _pywintypes, _win32api, _win32con, _win32process, win32security = _require_windows()
    target = validate_windows_path_no_reparse(path)
    if _path_has_reparse_point(target):
        raise WindowsSecurityError("WINDOWS_PATH_REPARSE_DENIED", "refusing to protect a Windows reparse point")
    if directory and not target.is_dir():
        raise WindowsSecurityError("WINDOWS_PATH_UNSAFE", "Windows owner-private directory is not a directory")
    if not directory and not target.is_file():
        raise WindowsSecurityError("WINDOWS_PATH_UNSAFE", "Windows owner-private file is not a regular file")
    try:
        sid = win32security.ConvertStringSidToSid(owner_sid)
        descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            f"O:{owner_sid}D:P(A;;GA;;;{owner_sid})",
            win32security.SDDL_REVISION_1,
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        flags = (
            win32security.OWNER_SECURITY_INFORMATION
            | win32security.DACL_SECURITY_INFORMATION
            | getattr(win32security, "PROTECTED_DACL_SECURITY_INFORMATION", 0x80000000)
        )
        win32security.SetNamedSecurityInfo(
            str(target),
            win32security.SE_FILE_OBJECT,
            flags,
            sid,
            None,
            dacl,
            None,
        )
    except WindowsSecurityError:
        raise
    except Exception as exc:
        raise WindowsSecurityError("WINDOWS_DACL_UNAVAILABLE", "Windows owner-only DACL could not be applied") from exc
    validate_owner_private_path(target, owner_sid=owner_sid, directory=directory)


def ensure_owner_private_directory(path: str | os.PathLike[str], *, owner_sid: str) -> None:
    target = validate_windows_path_no_reparse(path)
    target.mkdir(parents=True, exist_ok=True)
    protect_owner_private_path(target, owner_sid=owner_sid, directory=True)


def validate_local_named_pipe_name(pipe_name: str, *, prefix: str = "factlane-") -> str:
    if not isinstance(pipe_name, str) or not pipe_name.startswith(_PIPE_PREFIX):
        raise WindowsSecurityError("WINDOWS_PIPE_NAME_INVALID", "named pipe must use the local Windows pipe namespace")
    tail = pipe_name[len(_PIPE_PREFIX) :]
    if (
        not tail
        or len(tail) > 240
        or not tail.casefold().startswith(prefix.casefold())
        or "\\" in tail
        or "/" in tail
        or any(ord(char) < 32 for char in tail)
    ):
        raise WindowsSecurityError("WINDOWS_PIPE_NAME_INVALID", "named pipe name is outside the FactLane local namespace")
    return pipe_name
