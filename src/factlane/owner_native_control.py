from __future__ import annotations

import errno
import html
import json
import os
import socket
import socketserver
import stat
import struct
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .owner_auth import AuthError, NativeConfirmationRequest, OwnerAuthState
from .windows_security import (
    WindowsProcessIdentity,
    WindowsSecurityError,
    current_user_sid,
    owner_only_security_attributes,
    process_identity,
    require_process_identity,
    validate_local_named_pipe_name,
    validate_owner_only_kernel_object,
)


MAX_FRAME_BYTES = 64 * 1024
FRAME_HEADER_BYTES = 4
CONTROL_TIMEOUT_SECONDS = 10.0
SENSITIVE_CONFIRMATION_RESPONSE_TIMEOUT_SECONDS = 120.0
LOCAL_ENROLLMENT_SOURCE = "LOCAL_NATIVE"
_FORBIDDEN_SERVICE_RESULT_KEY_PARTS = ("secret", "token", "csrf", "preauth", "cookie", "session_secret")


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


class LocalServiceControl(Protocol):
    def status(self) -> Mapping[str, Any] | Any: ...

    def start(self, request_id: str) -> Mapping[str, Any] | Any: ...

    def stop(self, request_id: str) -> Mapping[str, Any] | Any: ...

    def restart(self, request_id: str) -> Mapping[str, Any] | Any: ...


PeerCredentialProvider = Callable[[socket.socket], PeerCredentials]
CommandRunner = Callable[[tuple[str, ...]], int]


def _linux_peer_credentials(conn: socket.socket) -> PeerCredentials:
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise RuntimeError("SO_PEERCRED is unavailable on this platform")
    raw = conn.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
    if len(raw) != struct.calcsize("3i"):
        raise RuntimeError("SO_PEERCRED returned an invalid credential record")
    pid, uid, gid = struct.unpack("3i", raw)
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def _run_zenity_argv(argv: tuple[str, ...]) -> int:
    result = subprocess.run(
        argv,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return int(result.returncode)


class ZenityNativeConfirmation:
    """Native Owner confirmation using a fixed argv-only Zenity invocation."""

    def __init__(
        self,
        *,
        helper: str = "zenity",
        command_runner: CommandRunner | None = None,
    ) -> None:
        if not isinstance(helper, str) or not helper or "/" in helper or len(helper) > 128:
            raise ValueError("helper must be a bounded executable name")
        self._helper = helper
        self._command_runner = command_runner or _run_zenity_argv

    def confirm(self, request: NativeConfirmationRequest) -> bool:
        text = self._confirmation_text(request)
        argv = (
            self._helper,
            "--question",
            "--title=FactLane Owner confirmation",
            f"--text={text}",
            "--ok-label=Approve",
            "--cancel-label=Deny",
        )
        try:
            return self._command_runner(argv) == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _confirmation_text(request: NativeConfirmationRequest) -> str:
        parts = [
            f"Action: {request.kind}",
            f"Verification phrase: {request.verification_phrase}",
        ]
        if request.device_label:
            parts.append(f"Device: {request.device_label}")
        if request.origin:
            parts.append(f"Origin: {request.origin}")
        if request.locator:
            parts.append(f"Request: {request.locator}")
        if request.preview:
            parts.append(f"Preview: {request.preview}")
        return html.escape("\n".join(parts), quote=False)


class _NativeControlHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.server.handle_connection(self.request)  # type: ignore[attr-defined]


_UnixStreamServerBase = getattr(socketserver, "UnixStreamServer", object)


class OwnerNativeControlServer(_UnixStreamServerBase):
    """Sequential local Owner launcher/control server with mandatory peer UID auth."""

    allow_reuse_address = False
    request_queue_size = 8

    def __init__(
        self,
        *,
        socket_path: str | os.PathLike[str],
        owner_uid: int,
        auth_state: OwnerAuthState,
        service_control: LocalServiceControl,
        peer_credentials: PeerCredentialProvider | None = None,
        request_timeout_seconds: float = CONTROL_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(owner_uid, bool) or not isinstance(owner_uid, int) or owner_uid < 0:
            raise ValueError("owner_uid must be a non-negative integer")
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or request_timeout_seconds <= 0
            or request_timeout_seconds > CONTROL_TIMEOUT_SECONDS
        ):
            raise ValueError("request_timeout_seconds must be greater than zero and at most 10 seconds")
        if peer_credentials is None and getattr(socket, "SO_PEERCRED", None) is None:
            raise RuntimeError("SO_PEERCRED is required for Owner native control")
        self.socket_path = Path(socket_path)
        self.owner_uid = owner_uid
        self.auth_state = auth_state
        self.service_control = service_control
        self.peer_credentials = peer_credentials or _linux_peer_credentials
        self.request_timeout_seconds = float(request_timeout_seconds)
        self._socket_identity: tuple[int, int] | None = None
        self._prepare_socket_path()
        try:
            super().__init__(str(self.socket_path), _NativeControlHandler)
            os.chmod(self.socket_path, 0o600)
            bound = self.socket_path.stat()
            self._socket_identity = (bound.st_dev, bound.st_ino)
        except BaseException:
            self._unlink_own_socket()
            raise

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout_seconds)
        return request, client_address

    def handle_error(self, request: socket.socket, client_address: Any) -> None:
        # Never emit stdlib tracebacks that could include local request material.
        return

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self._unlink_own_socket()

    def _prepare_socket_path(self) -> None:
        parent = self.socket_path.parent
        if parent.exists():
            if not parent.is_dir():
                raise NotADirectoryError(parent)
            if stat.S_IMODE(parent.stat().st_mode) != 0o700:
                raise PermissionError("Owner native-control socket directory must be mode 0700")
        else:
            parent.mkdir(parents=True, mode=0o700)
            os.chmod(parent, 0o700)

        if not self.socket_path.exists() and not self.socket_path.is_symlink():
            return
        current = self.socket_path.lstat()
        if not stat.S_ISSOCK(current.st_mode):
            raise FileExistsError(f"refusing to replace non-socket path: {self.socket_path}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(self.socket_path))
        except OSError as exc:
            if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise
        else:
            raise OSError(errno.EADDRINUSE, "Owner native-control socket is already accepting connections")
        finally:
            probe.close()
        self.socket_path.unlink()

    def _unlink_own_socket(self) -> None:
        if self._socket_identity is None:
            return
        try:
            current = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(current.st_mode):
            return
        if (current.st_dev, current.st_ino) != self._socket_identity:
            return
        self.socket_path.unlink()

    def handle_connection(self, conn: socket.socket) -> None:
        try:
            peer = self.peer_credentials(conn)
        except Exception:
            self._send(conn, {"status": "ERROR", "code": "PEER_CREDENTIALS_UNAVAILABLE"})
            return
        if not isinstance(peer, PeerCredentials) or peer.uid != self.owner_uid:
            self._send(conn, {"status": "ERROR", "code": "PEER_UID_DENIED"})
            return

        try:
            request = self._receive_request(conn)
            if request.get("op") == "APPROVE_PENDING_SENSITIVE":
                conn.settimeout(
                    max(
                        self.request_timeout_seconds,
                        SENSITIVE_CONFIRMATION_RESPONSE_TIMEOUT_SECONDS,
                    )
                )
            response = self._dispatch(request)
        except _NativeFailure as exc:
            response = {"status": "ERROR", "code": exc.code}
        except AuthError as exc:
            response = {"status": "ERROR", "code": exc.code, "retryable": exc.retryable}
        except (TimeoutError, socket.timeout):
            response = {"status": "ERROR", "code": "REQUEST_TIMEOUT"}
        except Exception:
            response = {"status": "ERROR", "code": "OPERATION_FAILED"}
        self._send(conn, response)

    def _receive_request(self, conn: socket.socket) -> dict[str, Any]:
        header = self._recv_exact(conn, FRAME_HEADER_BYTES)
        length = struct.unpack("!I", header)[0]
        if length == 0:
            raise _NativeFailure("INVALID_ENVELOPE")
        if length > MAX_FRAME_BYTES:
            raise _NativeFailure("FRAME_TOO_LARGE")
        payload = self._recv_exact(conn, length)
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _NativeFailure("INVALID_JSON") from exc
        if not isinstance(decoded, dict):
            raise _NativeFailure("INVALID_ENVELOPE")
        return decoded

    @staticmethod
    def _recv_exact(conn: socket.socket, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            chunk = conn.recv(remaining)
            if not chunk:
                raise _NativeFailure("INCOMPLETE_FRAME")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        return _dispatch_native_request(
            request,
            owner_uid=self.owner_uid,
            auth_state=self.auth_state,
            service_control=self.service_control,
        )

    @staticmethod
    def _exact_fields(request: dict[str, Any], expected: set[str]) -> None:
        if set(request) != expected:
            raise _NativeFailure("INVALID_ENVELOPE")

    @staticmethod
    def _required_string(request: dict[str, Any], field: str, maximum: int) -> str:
        value = request.get(field)
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise _NativeFailure("INVALID_ENVELOPE")
        return value

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        return _native_json_safe(value)

    @staticmethod
    def _send(conn: socket.socket, response: dict[str, Any]) -> None:
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_FRAME_BYTES:
            payload = b'{"status":"ERROR","code":"RESPONSE_TOO_LARGE"}'
        frame = struct.pack("!I", len(payload)) + payload
        try:
            conn.sendall(frame)
        except OSError:
            return


class _NativeFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _native_exact_fields(request: dict[str, Any], expected: set[str]) -> None:
    if set(request) != expected:
        raise _NativeFailure("INVALID_ENVELOPE")


def _native_required_string(request: dict[str, Any], field: str, maximum: int) -> str:
    value = request.get(field)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _NativeFailure("INVALID_ENVELOPE")
    return value


def _native_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _native_json_safe(value.value)
    if is_dataclass(value):
        return _native_json_safe(asdict(value))
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            folded = key_text.casefold()
            if any(part in folded for part in _FORBIDDEN_SERVICE_RESULT_KEY_PARTS):
                raise _NativeFailure("SERVICE_RESULT_INVALID")
            result[key_text] = _native_json_safe(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_native_json_safe(item) for item in value]
    raise _NativeFailure("SERVICE_RESULT_INVALID")


def _dispatch_native_request(
    request: dict[str, Any],
    *,
    owner_uid: int,
    auth_state: OwnerAuthState,
    service_control: LocalServiceControl,
) -> dict[str, Any]:
    op = request.get("op")
    if not isinstance(op, str) or not op:
        raise _NativeFailure("INVALID_ENVELOPE")

    if op == "BEGIN_LOCAL_ENROLLMENT":
        _native_exact_fields(request, {"op", "device_label"})
        device_label = _native_required_string(request, "device_label", 256)
        pending = auth_state.begin_enrollment(
            caller_uid=owner_uid,
            source=LOCAL_ENROLLMENT_SOURCE,
            device_label=device_label,
        )
        return {
            "status": "OK",
            "locator": pending.locator,
            "verification_phrase": pending.verification_phrase,
            "expires_at": pending.expires_at,
        }

    if op == "APPROVE_PENDING":
        _native_exact_fields(request, {"op", "locator"})
        locator = _native_required_string(request, "locator", 512)
        auth_state.approve_pending(locator=locator, caller_uid=owner_uid)
        return {"status": "OK", "locator": locator}

    if op == "APPROVE_PENDING_SENSITIVE":
        _native_exact_fields(request, {"op"})
        auth_state.approve_single_pending_sensitive_challenge(caller_uid=owner_uid)
        return {"status": "OK", "state": "SENSITIVE_APPROVED"}

    if op == "RECOVER_CREDENTIALS":
        _native_exact_fields(request, {"op"})
        epoch = auth_state.recover_credentials(caller_uid=owner_uid)
        return {"status": "OK", "auth_epoch": epoch}

    if op == "SERVICE_STATUS":
        _native_exact_fields(request, {"op"})
        return {"status": "OK", "service": _native_json_safe(service_control.status())}

    if op in {"SERVICE_START", "SERVICE_STOP", "SERVICE_RESTART"}:
        _native_exact_fields(request, {"op", "request_id"})
        request_id = _native_required_string(request, "request_id", 512)
        method = {
            "SERVICE_START": service_control.start,
            "SERVICE_STOP": service_control.stop,
            "SERVICE_RESTART": service_control.restart,
        }[op]
        result = method(request_id)
        return {"status": "OK", "service": _native_json_safe(result)}

    raise _NativeFailure("UNKNOWN_OPERATION")


def _windows_pipe_modules() -> tuple[Any, Any, Any]:
    if os.name != "nt":
        raise RuntimeError("Windows Named Pipes are unavailable on this platform")
    try:
        import pywintypes
        import win32file
        import win32pipe
    except ImportError as exc:  # pragma: no cover - native-Windows packaging guard
        raise RuntimeError("pywin32 Named Pipe primitives are unavailable") from exc
    return pywintypes, win32file, win32pipe


def _windows_pipe_read_exact(handle: Any, length: int, *, deadline: float) -> bytes:
    pywintypes, win32file, _win32pipe = _windows_pipe_modules()
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        try:
            _result, chunk = win32file.ReadFile(handle, remaining)
        except pywintypes.error as exc:
            if getattr(exc, "winerror", None) in {232, 536}:
                if time.monotonic() >= deadline:
                    raise _NativeFailure("REQUEST_TIMEOUT") from exc
                time.sleep(0.01)
                continue
            raise _NativeFailure("INCOMPLETE_FRAME") from exc
        except Exception as exc:
            raise _NativeFailure("INCOMPLETE_FRAME") from exc
        if not chunk:
            raise _NativeFailure("INCOMPLETE_FRAME")
        chunks.append(bytes(chunk))
        remaining -= len(chunk)
    return b"".join(chunks)


def _windows_pipe_send(handle: Any, response: dict[str, Any]) -> None:
    _pywintypes, win32file, _win32pipe = _windows_pipe_modules()
    payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        payload = b'{"status":"ERROR","code":"RESPONSE_TOO_LARGE"}'
    try:
        win32file.WriteFile(handle, struct.pack("!I", len(payload)) + payload)
    except Exception:
        return


class WindowsOwnerNativeControlServer:
    """Sequential owner-only Windows Named Pipe native-control server."""

    def __init__(
        self,
        *,
        pipe_name: str,
        owner_uid: int,
        auth_state: OwnerAuthState,
        service_control: LocalServiceControl,
        owner_sid: str | None = None,
        request_timeout_seconds: float = CONTROL_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(owner_uid, bool) or not isinstance(owner_uid, int) or owner_uid < 0:
            raise ValueError("owner_uid must be a non-negative integer")
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or request_timeout_seconds <= 0
            or request_timeout_seconds > CONTROL_TIMEOUT_SECONDS
        ):
            raise ValueError("request_timeout_seconds must be greater than zero and at most 10 seconds")
        self.pipe_name = validate_local_named_pipe_name(pipe_name, prefix="factlane-owner-")
        self.owner_uid = owner_uid
        self.auth_state = auth_state
        self.service_control = service_control
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.owner_sid = current_user_sid() if owner_sid is None else owner_sid
        current = process_identity(os.getpid())
        if current.sid.casefold() != self.owner_sid.casefold():
            raise WindowsSecurityError(
                "WINDOWS_OWNER_MISMATCH",
                "Owner native-control server process does not belong to the configured Owner SID",
            )
        self.process_identity: WindowsProcessIdentity = current
        self._shutdown = threading.Event()
        self._active_handle: Any | None = None

    def _create_pipe(self) -> Any:
        _pywintypes, win32file, win32pipe = _windows_pipe_modules()
        security = owner_only_security_attributes(self.owner_sid)
        open_mode = win32pipe.PIPE_ACCESS_DUPLEX | getattr(win32file, "FILE_FLAG_FIRST_PIPE_INSTANCE", 0x00080000)
        pipe_mode = (
            win32pipe.PIPE_TYPE_BYTE
            | win32pipe.PIPE_READMODE_BYTE
            | win32pipe.PIPE_WAIT
            | getattr(win32pipe, "PIPE_REJECT_REMOTE_CLIENTS", 0x00000008)
        )
        try:
            handle = win32pipe.CreateNamedPipe(
                self.pipe_name,
                open_mode,
                pipe_mode,
                1,
                MAX_FRAME_BYTES + FRAME_HEADER_BYTES,
                MAX_FRAME_BYTES + FRAME_HEADER_BYTES,
                int(self.request_timeout_seconds * 1000),
                security,
            )
            validate_owner_only_kernel_object(handle, self.owner_sid)
            return handle
        except WindowsSecurityError:
            raise
        except Exception as exc:
            raise WindowsSecurityError(
                "WINDOWS_PIPE_ENDPOINT_UNAVAILABLE",
                "Owner native-control Named Pipe could not be created exclusively",
            ) from exc

    def serve_forever(self, poll_interval: float = 0.05) -> None:
        if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        pywintypes, win32file, win32pipe = _windows_pipe_modules()
        error_pipe_connected = getattr(pywintypes, "ERROR_PIPE_CONNECTED", 535)
        handle = self._create_pipe()
        self._active_handle = handle
        try:
            while not self._shutdown.is_set():
                win32pipe.SetNamedPipeHandleState(
                    handle,
                    win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                    None,
                    None,
                )
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                except pywintypes.error as exc:
                    if getattr(exc, "winerror", None) != error_pipe_connected:
                        if self._shutdown.is_set():
                            return
                        raise
                if self._shutdown.is_set():
                    return
                try:
                    validate_owner_only_kernel_object(handle, self.owner_sid)
                    try:
                        peer_pid = int(win32pipe.GetNamedPipeClientProcessId(handle))
                        require_process_identity(peer_pid, owner_sid=self.owner_sid)
                    except Exception:
                        _windows_pipe_send(handle, {"status": "ERROR", "code": "PEER_CREDENTIALS_UNAVAILABLE"})
                        continue
                    win32pipe.SetNamedPipeHandleState(
                        handle,
                        win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_NOWAIT,
                        None,
                        None,
                    )
                    self._handle_pipe(handle)
                    try:
                        win32file.FlushFileBuffers(handle)
                    except Exception:
                        pass
                finally:
                    try:
                        win32pipe.DisconnectNamedPipe(handle)
                    except Exception:
                        pass
        finally:
            self._active_handle = None
            try:
                win32pipe.DisconnectNamedPipe(handle)
            except Exception:
                pass
            close = getattr(handle, "Close", None)
            if callable(close):
                close()

    def _handle_pipe(self, handle: Any) -> None:
        try:
            deadline = time.monotonic() + self.request_timeout_seconds
            header = _windows_pipe_read_exact(handle, FRAME_HEADER_BYTES, deadline=deadline)
            length = struct.unpack("!I", header)[0]
            if length == 0:
                raise _NativeFailure("INVALID_ENVELOPE")
            if length > MAX_FRAME_BYTES:
                raise _NativeFailure("FRAME_TOO_LARGE")
            payload = _windows_pipe_read_exact(handle, length, deadline=deadline)
            try:
                request = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _NativeFailure("INVALID_JSON") from exc
            if not isinstance(request, dict):
                raise _NativeFailure("INVALID_ENVELOPE")
            response = _dispatch_native_request(
                request,
                owner_uid=self.owner_uid,
                auth_state=self.auth_state,
                service_control=self.service_control,
            )
        except _NativeFailure as exc:
            response = {"status": "ERROR", "code": exc.code}
        except AuthError as exc:
            response = {"status": "ERROR", "code": exc.code, "retryable": exc.retryable}
        except Exception:
            response = {"status": "ERROR", "code": "OPERATION_FAILED"}
        _windows_pipe_send(handle, response)

    def shutdown(self) -> None:
        self._shutdown.set()
        try:
            _pywintypes, win32file, win32pipe = _windows_pipe_modules()
            win32pipe.WaitNamedPipe(self.pipe_name, 200)
            wake = win32file.CreateFile(
                self.pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
        except Exception:
            return
        try:
            # This connection exists only to release a blocking ConnectNamedPipe
            # after shutdown authority is already local/in-process.  The server
            # validates DACL and peer identity on every real request connection.
            return
        finally:
            try:
                close = getattr(wake, "Close", None)
                if callable(close):
                    close()
            except Exception:
                pass

    def server_close(self) -> None:
        self.shutdown()
