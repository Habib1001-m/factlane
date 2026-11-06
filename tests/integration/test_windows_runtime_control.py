from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows runtime-control qualification only")

from factlane.runtime_control import (
    MAX_CONTROL_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    RuntimeControlError,
    RuntimeControlRequest,
    RuntimeIdentity,
    RuntimeRegistry,
    WindowsRuntimeControlClient,
    WindowsRuntimeControlServer,
    decode_request,
    make_response,
    windows_runtime_pipe_name,
)
from factlane.windows_security import (
    WindowsSecurityError,
    current_user_sid,
    process_identity,
    validate_owner_private_path,
)


OWNER_UID = 1000


class _Controller:
    async def handle(self, request: RuntimeControlRequest) -> dict[str, object]:
        return make_response(
            request,
            state="OPEN",
            actual_generation=1,
            status_code="OK",
            lease_open=True,
        )


def _runtime_identity(tmp_path: Path) -> tuple[RuntimeIdentity, RuntimeRegistry]:
    db = tmp_path / "memory.db"
    db.touch()
    info = db.stat()
    process = process_identity(os.getpid())
    instance_id = str(uuid.uuid4())
    identity = RuntimeIdentity(
        protocol_version=PROTOCOL_VERSION,
        instance_id=instance_id,
        owner_uid=OWNER_UID,
        pid=os.getpid(),
        process_start_ticks=process.creation_time_us,
        boot_id="windows-a1-test-boot",
        database_path=str(db.resolve()),
        device=info.st_dev,
        inode=info.st_ino,
        generation=1,
        control_socket_path=windows_runtime_pipe_name(instance_id),
        lifecycle_state="OPEN",
    )
    registry = RuntimeRegistry(
        tmp_path / "runtime",
        owner_uid=OWNER_UID,
        boot_id_provider=lambda: identity.boot_id,
        process_start_provider=lambda pid: identity.process_start_ticks if pid == identity.pid else None,
    )
    registry.register(identity)
    return identity, registry


def _request(identity: RuntimeIdentity) -> RuntimeControlRequest:
    return RuntimeControlRequest.from_mapping(
        {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": str(uuid.uuid4()),
            "operation_id": str(uuid.uuid4()),
            "instance_id": identity.instance_id,
            "expected_generation": identity.generation,
            "command": "INSPECT",
        }
    )


def test_windows_runtime_registry_state_is_owner_sid_private(tmp_path: Path) -> None:
    identity, registry = _runtime_identity(tmp_path)
    registration = registry.registry_root / f"{identity.instance_id}.json"
    validate_owner_private_path(registry.runtime_root, owner_sid=current_user_sid(), directory=True)
    validate_owner_private_path(registry.registry_root, owner_sid=current_user_sid(), directory=True)
    validate_owner_private_path(registration, owner_sid=current_user_sid(), directory=False)
    assert registry.load(identity.instance_id) == identity


def test_windows_runtime_named_pipe_round_trip_authenticates_registered_process(tmp_path: Path) -> None:
    async def run() -> None:
        identity, registry = _runtime_identity(tmp_path)
        server = WindowsRuntimeControlServer(
            identity=identity,
            registry=registry,
            controller=_Controller(),
            owner_sid=current_user_sid(),
        )
        await server.start()
        try:
            result = await WindowsRuntimeControlClient(registry, owner_sid=current_user_sid()).call(
                identity,
                _request(identity),
            )
            assert result["status_code"] == "OK"
            assert result["state"] == "OPEN"
            assert result["lease_open"] is True
        finally:
            await server.close()

    asyncio.run(run())


def test_windows_runtime_client_rejects_replaced_server_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def run() -> None:
        import win32pipe

        identity, registry = _runtime_identity(tmp_path)
        server = WindowsRuntimeControlServer(
            identity=identity,
            registry=registry,
            controller=_Controller(),
            owner_sid=current_user_sid(),
        )
        await server.start()
        try:
            monkeypatch.setattr(win32pipe, "GetNamedPipeServerProcessId", lambda _handle: identity.pid + 1)
            with pytest.raises(RuntimeControlError) as exc_info:
                await WindowsRuntimeControlClient(registry, owner_sid=current_user_sid()).call(
                    identity,
                    _request(identity),
                )
            assert exc_info.value.code == "PEER_PROCESS_MISMATCH"
        finally:
            await server.close()

    asyncio.run(run())


def test_windows_runtime_client_rejects_foreign_sid_custody(tmp_path: Path) -> None:
    async def run() -> None:
        identity, registry = _runtime_identity(tmp_path)
        server = WindowsRuntimeControlServer(
            identity=identity,
            registry=registry,
            controller=_Controller(),
            owner_sid=current_user_sid(),
        )
        await server.start()
        try:
            with pytest.raises(RuntimeControlError):
                await WindowsRuntimeControlClient(registry, owner_sid="S-1-5-18").call(
                    identity,
                    _request(identity),
                )
        finally:
            await server.close()

    asyncio.run(run())


def test_windows_runtime_server_first_instance_rejects_precreated_pipe(tmp_path: Path) -> None:
    async def run() -> None:
        import win32pipe

        identity, registry = _runtime_identity(tmp_path)
        precreated = win32pipe.CreateNamedPipe(
            identity.control_socket_path,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1,
            4096,
            4096,
            1000,
            None,
        )
        try:
            server = WindowsRuntimeControlServer(
                identity=identity,
                registry=registry,
                controller=_Controller(),
                owner_sid=current_user_sid(),
            )
            with pytest.raises(RuntimeControlError) as exc_info:
                await server.start()
            assert exc_info.value.code == "CONTROL_SOCKET_UNAVAILABLE"
        finally:
            close = getattr(precreated, "Close", None)
            if callable(close):
                close()

    asyncio.run(run())


def test_windows_runtime_client_fails_closed_when_server_identity_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def run() -> None:
        import win32pipe

        identity, registry = _runtime_identity(tmp_path)
        server = WindowsRuntimeControlServer(
            identity=identity,
            registry=registry,
            controller=_Controller(),
            owner_sid=current_user_sid(),
        )
        await server.start()
        try:
            def unavailable(_handle):
                raise OSError("synthetic peer identity failure")

            monkeypatch.setattr(win32pipe, "GetNamedPipeServerProcessId", unavailable)
            with pytest.raises(RuntimeControlError) as exc_info:
                await WindowsRuntimeControlClient(registry, owner_sid=current_user_sid()).call(
                    identity,
                    _request(identity),
                )
            assert exc_info.value.code == "CONTROL_SOCKET_UNAVAILABLE"
        finally:
            await server.close()

    asyncio.run(run())


def test_windows_runtime_identity_rejects_noncanonical_pipe_substitution(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    db.touch()
    info = db.stat()
    process = process_identity(os.getpid())
    instance_id = str(uuid.uuid4())
    with pytest.raises(RuntimeControlError) as exc_info:
        RuntimeIdentity(
            protocol_version=PROTOCOL_VERSION,
            instance_id=instance_id,
            owner_uid=OWNER_UID,
            pid=os.getpid(),
            process_start_ticks=process.creation_time_us,
            boot_id="windows-a1-test-boot",
            database_path=str(db.resolve()),
            device=info.st_dev,
            inode=info.st_ino,
            generation=1,
            control_socket_path=rf"\\.\pipe\factlane-runtime-{uuid.uuid4().hex}",
            lifecycle_state="OPEN",
        )
    assert exc_info.value.code == "PATH_IDENTITY_MISMATCH"


def test_windows_runtime_protocol_rejects_malformed_and_oversized_messages() -> None:
    with pytest.raises(RuntimeControlError) as malformed:
        decode_request(b"not-json\n")
    assert malformed.value.code == "INVALID_CONTROL_MESSAGE"

    oversized = b"{" + b" " * MAX_CONTROL_MESSAGE_BYTES + b"}\n"
    with pytest.raises(RuntimeControlError) as too_large:
        decode_request(oversized)
    assert too_large.value.code == "CONTROL_MESSAGE_TOO_LARGE"


def test_windows_runtime_abrupt_client_disconnect_does_not_release_persistent_endpoint(tmp_path: Path) -> None:
    class _SlowController:
        async def handle(self, request: RuntimeControlRequest) -> dict[str, object]:
            await asyncio.sleep(0.05)
            return make_response(
                request,
                state="OPEN",
                actual_generation=1,
                status_code="OK",
                lease_open=True,
            )

    async def run() -> None:
        import win32file
        import win32pipe

        identity, registry = _runtime_identity(tmp_path)
        server = WindowsRuntimeControlServer(
            identity=identity,
            registry=registry,
            controller=_SlowController(),
            owner_sid=current_user_sid(),
        )
        await server.start()
        try:
            first = _request(identity)

            def send_then_disconnect() -> None:
                win32pipe.WaitNamedPipe(identity.control_socket_path, 2000)
                handle = win32file.CreateFile(
                    identity.control_socket_path,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
                try:
                    win32file.WriteFile(handle, first.to_bytes())
                finally:
                    close = getattr(handle, "Close", None)
                    if callable(close):
                        close()

            await asyncio.to_thread(send_then_disconnect)
            await asyncio.sleep(0.1)

            result = await WindowsRuntimeControlClient(registry, owner_sid=current_user_sid()).call(
                identity,
                _request(identity),
            )
            assert result["status_code"] == "OK"
            assert result["state"] == "OPEN"
        finally:
            await server.close()

    asyncio.run(run())
