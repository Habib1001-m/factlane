from __future__ import annotations

import os
import json
import struct
import sys
import threading
import time
from dataclasses import replace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows Named Pipe qualification only")

from factlane.owner_auth import OwnerAuthState
from factlane.owner_native_control import WindowsOwnerNativeControlServer
from factlane.owner_native_control import MAX_FRAME_BYTES
from factlane.webui_platform import WebUIPlatformError, WindowsNativeLauncherClient
from factlane.windows_security import (
    WindowsSecurityError,
    current_user_sid,
    process_identity,
    validate_owner_only_kernel_object,
)


OWNER_UID = 1000


class _Confirmation:
    def confirm(self, _request) -> bool:
        return True


class _Service:
    def __init__(self, *, status_delay: float = 0.0) -> None:
        self.status_delay = status_delay

    def status(self):
        if self.status_delay:
            time.sleep(self.status_delay)
        return {"state": "READY"}

    def start(self, request_id: str):
        return {"state": "READY", "request_id": request_id}

    def stop(self, request_id: str):
        return {"state": "STOPPED_UNTIL_REBOOT", "request_id": request_id}

    def restart(self, request_id: str):
        return {"state": "READY", "request_id": request_id}


def _pipe_name(label: str) -> str:
    return rf"\\.\pipe\factlane-owner-{label}-{os.getpid()}-{time.time_ns()}"


def _start_server(*, service: _Service | None = None):
    sid = current_user_sid()
    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=_Confirmation())
    server = WindowsOwnerNativeControlServer(
        pipe_name=_pipe_name("test"),
        owner_uid=OWNER_UID,
        owner_sid=sid,
        auth_state=auth,
        service_control=service or _Service(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, sid


def _stop_server(server: WindowsOwnerNativeControlServer, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=2.0)
    server.server_close()
    assert not thread.is_alive()


def _raw_pipe_exchange(pipe_name: str, payload: bytes, *, timeout_ms: int = 2000) -> dict[str, object]:
    import win32file
    import win32pipe

    win32pipe.WaitNamedPipe(pipe_name, timeout_ms)
    handle = win32file.CreateFile(
        pipe_name,
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0,
        None,
        win32file.OPEN_EXISTING,
        0,
        None,
    )
    try:
        win32file.WriteFile(handle, payload)
        _result, header = win32file.ReadFile(handle, 4)
        length = struct.unpack("!I", bytes(header))[0]
        data = bytearray()
        while len(data) < length:
            _result, chunk = win32file.ReadFile(handle, length - len(data))
            data.extend(chunk)
        return json.loads(bytes(data).decode("utf-8"))
    finally:
        close = getattr(handle, "Close", None)
        if callable(close):
            close()


def test_windows_owner_named_pipe_same_user_round_trip_and_server_identity() -> None:
    server, thread, sid = _start_server()
    try:
        assert server.process_identity.sid == sid
        assert server.process_identity.pid == os.getpid()
        client = WindowsNativeLauncherClient(
            server.pipe_name,
            owner_sid=sid,
            expected_server=server.process_identity,
            timeout_seconds=2.0,
        )
        assert client.status() == {"state": "READY"}
        assert client.start(request_id="start-1")["request_id"] == "start-1"
        assert client.stop(request_id="stop-1")["request_id"] == "stop-1"
        assert client.restart(request_id="restart-1")["request_id"] == "restart-1"
    finally:
        _stop_server(server, thread)


def test_windows_owner_named_pipe_rejects_replaced_server_process_identity() -> None:
    server, thread, sid = _start_server()
    try:
        replaced = replace(server.process_identity, creation_time_us=server.process_identity.creation_time_us + 1)
        client = WindowsNativeLauncherClient(
            server.pipe_name,
            owner_sid=sid,
            expected_server=replaced,
            timeout_seconds=2.0,
        )
        with pytest.raises(WebUIPlatformError) as exc_info:
            client.status()
        assert exc_info.value.code == "CONTROL_ENDPOINT_REPLACED"
    finally:
        _stop_server(server, thread)


def test_windows_owner_named_pipe_rejects_unverifiable_server_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    server, thread, sid = _start_server()
    try:
        def unavailable(*_args, **_kwargs):
            raise WindowsSecurityError(
                "WINDOWS_PROCESS_IDENTITY_UNAVAILABLE",
                "synthetic unavailable process identity",
            )

        monkeypatch.setattr("factlane.webui_platform.require_process_identity", unavailable)
        client = WindowsNativeLauncherClient(
            server.pipe_name,
            owner_sid=sid,
            expected_server=server.process_identity,
            timeout_seconds=2.0,
        )
        with pytest.raises(WebUIPlatformError) as exc_info:
            client.status()
        assert exc_info.value.code == "CONTROL_PEER_IDENTITY_UNAVAILABLE"
    finally:
        _stop_server(server, thread)


def test_windows_owner_named_pipe_rejects_foreign_owner_sid_before_authority() -> None:
    server, thread, sid = _start_server()
    try:
        foreign = replace(server.process_identity, sid="S-1-5-18")
        with pytest.raises(WebUIPlatformError) as exc_info:
            WindowsNativeLauncherClient(
                server.pipe_name,
                owner_sid=sid,
                expected_server=foreign,
                timeout_seconds=2.0,
            )
        assert exc_info.value.code == "CONTROL_PEER_IDENTITY_DENIED"
    finally:
        _stop_server(server, thread)


def test_windows_owner_named_pipe_rejects_unsafe_default_dacl() -> None:
    import win32file
    import win32pipe

    sid = current_user_sid()
    pipe = _pipe_name("unsafe-dacl")
    handle = win32pipe.CreateNamedPipe(
        pipe,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1,
        4096,
        4096,
        1000,
        None,
    )
    errors: list[BaseException] = []

    def accept() -> None:
        try:
            win32pipe.ConnectNamedPipe(handle, None)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        expected = process_identity(os.getpid())
        client = WindowsNativeLauncherClient(pipe, owner_sid=sid, expected_server=expected, timeout_seconds=2.0)
        with pytest.raises(WebUIPlatformError) as exc_info:
            client.status()
        assert exc_info.value.code == "CONTROL_SOCKET_UNSAFE"
    finally:
        close = getattr(handle, "Close", None)
        if callable(close):
            close()
        thread.join(timeout=2.0)
    assert not errors


def test_windows_owner_named_pipe_first_instance_rejects_precreated_endpoint() -> None:
    import win32pipe

    pipe = _pipe_name("precreated")
    precreated = win32pipe.CreateNamedPipe(
        pipe,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1,
        4096,
        4096,
        1000,
        None,
    )
    try:
        server = WindowsOwnerNativeControlServer(
            pipe_name=pipe,
            owner_uid=OWNER_UID,
            owner_sid=current_user_sid(),
            auth_state=OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=_Confirmation()),
            service_control=_Service(),
        )
        with pytest.raises(WindowsSecurityError) as exc_info:
            server._create_pipe()
        assert exc_info.value.code == "WINDOWS_PIPE_ENDPOINT_UNAVAILABLE"
    finally:
        close = getattr(precreated, "Close", None)
        if callable(close):
            close()


def test_windows_owner_named_pipe_response_timeout_is_bounded() -> None:
    server, thread, sid = _start_server(service=_Service(status_delay=0.25))
    try:
        client = WindowsNativeLauncherClient(
            server.pipe_name,
            owner_sid=sid,
            expected_server=server.process_identity,
            timeout_seconds=0.05,
        )
        started = time.monotonic()
        with pytest.raises(WebUIPlatformError) as exc_info:
            client.status()
        elapsed = time.monotonic() - started
        assert exc_info.value.code == "CONTROL_UNAVAILABLE"
        assert elapsed < 1.0
    finally:
        _stop_server(server, thread)


def test_windows_owner_named_pipe_rejects_invalid_json_and_oversized_frame() -> None:
    server, thread, _sid = _start_server()
    try:
        invalid = _raw_pipe_exchange(server.pipe_name, struct.pack("!I", 1) + b"{")
        assert invalid == {"status": "ERROR", "code": "INVALID_JSON"}

        oversized = _raw_pipe_exchange(server.pipe_name, struct.pack("!I", MAX_FRAME_BYTES + 1))
        assert oversized == {"status": "ERROR", "code": "FRAME_TOO_LARGE"}
    finally:
        _stop_server(server, thread)


def test_windows_owner_named_pipe_identity_failure_does_not_wedge_persistent_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import factlane.owner_native_control as native_control
    import win32file
    import win32pipe

    server, thread, sid = _start_server()
    original = native_control.require_process_identity
    calls = 0
    rejected = threading.Event()

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            rejected.set()
            raise WindowsSecurityError(
                "WINDOWS_PROCESS_IDENTITY_UNAVAILABLE",
                "synthetic unavailable client identity",
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(native_control, "require_process_identity", fail_once)
    try:
        win32pipe.WaitNamedPipe(server.pipe_name, 2000)
        unverified = win32file.CreateFile(
            server.pipe_name,
            win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0,
            None,
            win32file.OPEN_EXISTING,
            0,
            None,
        )
        try:
            assert rejected.wait(1.0)
        finally:
            close = getattr(unverified, "Close", None)
            if callable(close):
                close()

        client = WindowsNativeLauncherClient(
            server.pipe_name,
            owner_sid=sid,
            expected_server=server.process_identity,
            timeout_seconds=2.0,
        )
        assert client.status() == {"state": "READY"}
    finally:
        _stop_server(server, thread)


def test_windows_owner_control_does_not_accept_tcp_endpoint_substitution() -> None:
    expected = process_identity(os.getpid())
    with pytest.raises(WindowsSecurityError) as exc_info:
        WindowsNativeLauncherClient(
            "127.0.0.1:8765",
            owner_sid=current_user_sid(),
            expected_server=expected,
        )
    assert exc_info.value.code == "WINDOWS_PIPE_NAME_INVALID"
