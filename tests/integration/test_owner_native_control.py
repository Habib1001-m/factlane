from __future__ import annotations

import json
import socket
import stat
import struct
import subprocess
import threading
from dataclasses import dataclass

import pytest

from factlane.owner_auth import NativeConfirmationRequest, OwnerAuthState, PendingEnrollment
from factlane.owner_native_control import (
    MAX_FRAME_BYTES,
    OwnerNativeControlServer,
    PeerCredentials,
    ZenityNativeConfirmation,
)


OWNER_UID = 1000
ORIGIN = "https://owner.test"


@dataclass
class FakeNativeConfirmation:
    approved: bool = True

    def __post_init__(self) -> None:
        self.requests: list[NativeConfirmationRequest] = []

    def confirm(self, request: NativeConfirmationRequest) -> bool:
        self.requests.append(request)
        return self.approved


class RecordingServiceControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def status(self):
        self.calls.append(("status", None))
        return {"state": "READY"}

    def start(self, request_id: str):
        self.calls.append(("start", request_id))
        return {"state": "STARTING", "request_id": request_id}

    def stop(self, request_id: str):
        self.calls.append(("stop", request_id))
        return {"state": "STOPPED_UNTIL_REBOOT", "request_id": request_id}

    def restart(self, request_id: str):
        self.calls.append(("restart", request_id))
        return {"state": "STARTING", "request_id": request_id}


class RecordingAuthBoundary:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def begin_enrollment(self, *, caller_uid: int, source: str, device_label: str):
        self.calls.append(("begin", (caller_uid, source, device_label)))
        return PendingEnrollment(locator="locator-1", verification_phrase="one two three four five", expires_at=123.0)

    def approve_pending(self, *, locator: str, caller_uid: int):
        self.calls.append(("approve_pending", (locator, caller_uid)))
        return object()

    def approve_single_pending_sensitive_challenge(self, *, caller_uid: int):
        self.calls.append(("approve_pending_sensitive", caller_uid))
        return object()

    def recover_credentials(self, *, caller_uid: int):
        self.calls.append(("recover", caller_uid))
        return 2


def same_owner_peer(_sock: socket.socket) -> PeerCredentials:
    return PeerCredentials(pid=4321, uid=OWNER_UID, gid=OWNER_UID)


def other_peer(_sock: socket.socket) -> PeerCredentials:
    return PeerCredentials(pid=9876, uid=OWNER_UID + 1, gid=OWNER_UID + 1)


def encode_frame(payload: dict[str, object]) -> bytes:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack("!I", len(raw)) + raw


def recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise AssertionError("socket closed before framed response completed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def decode_frame(sock: socket.socket) -> dict[str, object]:
    length = struct.unpack("!I", recv_exact(sock, 4))[0]
    assert length <= MAX_FRAME_BYTES
    return json.loads(recv_exact(sock, length).decode("utf-8"))


def exchange(socket_path: str, payload: dict[str, object]) -> dict[str, object]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(socket_path)
    try:
        client.sendall(encode_frame(payload))
        return decode_frame(client)
    finally:
        client.close()


@pytest.fixture
def native_server(tmp_path):
    confirmation = FakeNativeConfirmation()
    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=confirmation)
    service = RecordingServiceControl()
    server = OwnerNativeControlServer(
        socket_path=tmp_path / "native" / "owner-control.sock",
        owner_uid=OWNER_UID,
        auth_state=auth,
        service_control=service,
        peer_credentials=same_owner_peer,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, auth, service, confirmation
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_native_socket_is_private_and_local_enrollment_returns_only_nonsecret_browser_data(native_server) -> None:
    server, _, _, _ = native_server
    assert server.socket.family == socket.AF_UNIX
    assert stat.S_IMODE(server.socket_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(server.socket_path.stat().st_mode) == 0o600

    response = exchange(
        str(server.socket_path),
        {"op": "BEGIN_LOCAL_ENROLLMENT", "device_label": "Primary browser"},
    )
    assert response["status"] == "OK"
    assert response["locator"]
    assert response["verification_phrase"]
    assert response["expires_at"]
    serialized = json.dumps(response)
    for forbidden in ("session", "csrf", "preauth", "secret", "token"):
        assert forbidden not in serialized.casefold()


def test_native_auth_operations_stay_inside_owner_uid_boundary_and_invoke_native_confirmation(native_server) -> None:
    server, auth, _, confirmation = native_server
    begin = exchange(
        str(server.socket_path),
        {"op": "BEGIN_LOCAL_ENROLLMENT", "device_label": "Primary browser"},
    )
    locator = str(begin["locator"])
    preauth = auth.issue_preauth(origin=ORIGIN)
    auth.claim_pending(locator=locator, origin=ORIGIN, preauth_secret=preauth)

    approved = exchange(str(server.socket_path), {"op": "APPROVE_PENDING", "locator": locator})
    assert approved == {"status": "OK", "locator": locator}
    assert confirmation.requests[-1].kind == "ENROLLMENT"
    assert confirmation.requests[-1].caller_uid == OWNER_UID

    credentials = auth.poll_pending(locator=locator, origin=ORIGIN, preauth_secret=preauth)
    assert credentials is not None
    challenge = auth.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview="Approve exact destructive preview",
    )
    sensitive = exchange(str(server.socket_path), {"op": "APPROVE_PENDING_SENSITIVE"})
    assert sensitive == {"status": "OK", "state": "SENSITIVE_APPROVED"}
    assert confirmation.requests[-1].kind == "SENSITIVE_ACTION"
    assert confirmation.requests[-1].caller_uid == OWNER_UID
    assert challenge.challenge_id not in json.dumps(sensitive)
    for forbidden in ("challenge", "phrase", "preview", "grant", "session", "csrf"):
        assert forbidden not in json.dumps(sensitive).casefold()

    recovery = exchange(str(server.socket_path), {"op": "RECOVER_CREDENTIALS"})
    assert recovery == {"status": "OK", "auth_epoch": 2}
    assert confirmation.requests[-1].kind == "CREDENTIAL_RECOVERY"
    assert confirmation.requests[-1].caller_uid == OWNER_UID


def test_native_approve_pending_exposes_only_retryable_not_claimed_before_browser_claim(
    native_server,
) -> None:
    server, _, _, confirmation = native_server
    begin = exchange(
        str(server.socket_path),
        {"op": "BEGIN_LOCAL_ENROLLMENT", "device_label": "FactLane local browser"},
    )
    locator = str(begin["locator"])

    response = exchange(
        str(server.socket_path),
        {"op": "APPROVE_PENDING", "locator": locator},
    )

    assert response == {
        "status": "ERROR",
        "code": "PENDING_NOT_CLAIMED",
        "retryable": True,
    }
    assert confirmation.requests == []


def test_service_status_start_stop_restart_are_typed_and_request_id_bound(native_server) -> None:
    server, _, service, _ = native_server
    assert exchange(str(server.socket_path), {"op": "SERVICE_STATUS"}) == {
        "status": "OK",
        "service": {"state": "READY"},
    }
    assert exchange(str(server.socket_path), {"op": "SERVICE_START", "request_id": "start-1"})["status"] == "OK"
    assert exchange(str(server.socket_path), {"op": "SERVICE_STOP", "request_id": "stop-1"})["status"] == "OK"
    assert exchange(str(server.socket_path), {"op": "SERVICE_RESTART", "request_id": "restart-1"})["status"] == "OK"
    assert service.calls == [
        ("status", None),
        ("start", "start-1"),
        ("stop", "stop-1"),
        ("restart", "restart-1"),
    ]


def test_hostile_peer_uid_is_rejected_before_request_dispatch(tmp_path) -> None:
    auth = RecordingAuthBoundary()
    service = RecordingServiceControl()
    server = OwnerNativeControlServer(
        socket_path=tmp_path / "native" / "owner-control.sock",
        owner_uid=OWNER_UID,
        auth_state=auth,
        service_control=service,
        peer_credentials=other_peer,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(str(server.socket_path))
        try:
            response = decode_frame(client)
        finally:
            client.close()
        assert response == {"status": "ERROR", "code": "PEER_UID_DENIED"}
        assert auth.calls == []
        assert service.calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_native_control_processes_only_one_request_at_a_time(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingService(RecordingServiceControl):
        def status(self):
            self.calls.append(("status", None))
            entered.set()
            assert release.wait(timeout=2)
            return {"state": "READY"}

    service = BlockingService()
    server = OwnerNativeControlServer(
        socket_path=tmp_path / "native" / "owner-control.sock",
        owner_uid=OWNER_UID,
        auth_state=RecordingAuthBoundary(),
        service_control=service,
        peer_credentials=same_owner_peer,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    results: list[dict[str, object]] = []

    def invoke_status() -> None:
        results.append(exchange(str(server.socket_path), {"op": "SERVICE_STATUS"}))

    first = threading.Thread(target=invoke_status)
    second = threading.Thread(target=invoke_status)
    try:
        first.start()
        assert entered.wait(timeout=1)
        second.start()
        second.join(timeout=0.1)
        assert second.is_alive()
        assert service.calls == [("status", None)]
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)
        assert not first.is_alive()
        assert not second.is_alive()
        assert service.calls == [("status", None), ("status", None)]
        assert results == [
            {"status": "OK", "service": {"state": "READY"}},
            {"status": "OK", "service": {"state": "READY"}},
        ]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_native_protocol_rejects_unknown_ops_extra_fields_caller_uid_and_oversize_before_dispatch(tmp_path) -> None:
    auth = RecordingAuthBoundary()
    service = RecordingServiceControl()
    server = OwnerNativeControlServer(
        socket_path=tmp_path / "native" / "owner-control.sock",
        owner_uid=OWNER_UID,
        auth_state=auth,
        service_control=service,
        peer_credentials=same_owner_peer,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert exchange(str(server.socket_path), {"op": "QUERY_MEMORY"}) == {
            "status": "ERROR",
            "code": "UNKNOWN_OPERATION",
        }
        assert exchange(str(server.socket_path), {"op": "SERVICE_STATUS", "extra": "x"}) == {
            "status": "ERROR",
            "code": "INVALID_ENVELOPE",
        }
        assert exchange(
            str(server.socket_path),
            {"op": "BEGIN_LOCAL_ENROLLMENT", "device_label": "Browser", "caller_uid": OWNER_UID},
        ) == {"status": "ERROR", "code": "INVALID_ENVELOPE"}
        assert exchange(
            str(server.socket_path),
            {"op": "APPROVE_PENDING_SENSITIVE", "challenge_id": "browser-supplied"},
        ) == {"status": "ERROR", "code": "INVALID_ENVELOPE"}
        assert exchange(
            str(server.socket_path),
            {"op": "APPROVE_SENSITIVE_CHALLENGE", "challenge_id": "legacy-id"},
        ) == {"status": "ERROR", "code": "UNKNOWN_OPERATION"}

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(str(server.socket_path))
        try:
            client.sendall(struct.pack("!I", MAX_FRAME_BYTES + 1))
            assert decode_frame(client) == {"status": "ERROR", "code": "FRAME_TOO_LARGE"}
        finally:
            client.close()
        assert auth.calls == []
        assert service.calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_native_service_result_cannot_return_auth_secret_fields(tmp_path) -> None:
    class UnsafeService(RecordingServiceControl):
        def status(self):
            self.calls.append(("status", None))
            return {"state": "READY", "session_secret": "must-not-leak"}

    service = UnsafeService()
    server = OwnerNativeControlServer(
        socket_path=tmp_path / "native" / "owner-control.sock",
        owner_uid=OWNER_UID,
        auth_state=RecordingAuthBoundary(),
        service_control=service,
        peer_credentials=same_owner_peer,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = exchange(str(server.socket_path), {"op": "SERVICE_STATUS"})
        assert response == {"status": "ERROR", "code": "SERVICE_RESULT_INVALID"}
        assert "must-not-leak" not in json.dumps(response)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_native_pending_sensitive_none_and_ambiguous_are_stable_fail_closed_codes(native_server) -> None:
    server, auth, _, _ = native_server
    none = exchange(str(server.socket_path), {"op": "APPROVE_PENDING_SENSITIVE"})
    assert none == {
        "status": "ERROR",
        "code": "SENSITIVE_CHALLENGE_NONE",
        "retryable": False,
    }

    first = auth.begin_enrollment(caller_uid=OWNER_UID, source="one", device_label="One")
    first_preauth = auth.issue_preauth(origin=ORIGIN)
    auth.claim_pending(locator=first.locator, origin=ORIGIN, preauth_secret=first_preauth)
    auth.approve_pending(locator=first.locator, caller_uid=OWNER_UID)
    first_session = auth.poll_pending(locator=first.locator, origin=ORIGIN, preauth_secret=first_preauth)
    assert first_session is not None
    second = auth.begin_enrollment(caller_uid=OWNER_UID, source="two", device_label="Two")
    second_preauth = auth.issue_preauth(origin=ORIGIN)
    auth.claim_pending(locator=second.locator, origin=ORIGIN, preauth_secret=second_preauth)
    auth.approve_pending(locator=second.locator, caller_uid=OWNER_UID)
    second_session = auth.poll_pending(locator=second.locator, origin=ORIGIN, preauth_secret=second_preauth)
    assert second_session is not None
    auth.begin_sensitive_challenge(session_secret=first_session.session_secret, origin=ORIGIN, preview="One")
    auth.begin_sensitive_challenge(session_secret=second_session.session_secret, origin=ORIGIN, preview="Two")

    ambiguous = exchange(str(server.socket_path), {"op": "APPROVE_PENDING_SENSITIVE"})
    assert ambiguous == {
        "status": "ERROR",
        "code": "SENSITIVE_CHALLENGE_AMBIGUOUS",
        "retryable": False,
    }


def test_sensitive_native_op_extends_server_response_socket_window_without_relaxing_read_timeout(tmp_path) -> None:
    auth = RecordingAuthBoundary()
    server = OwnerNativeControlServer(
        socket_path=tmp_path / "native" / "owner-control.sock",
        owner_uid=OWNER_UID,
        auth_state=auth,
        service_control=RecordingServiceControl(),
        peer_credentials=same_owner_peer,
        request_timeout_seconds=0.25,
    )
    server_side, client_side = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    server_side.settimeout(0.25)
    try:
        client_side.sendall(encode_frame({"op": "APPROVE_PENDING_SENSITIVE"}))
        server.handle_connection(server_side)
        assert decode_frame(client_side) == {"status": "OK", "state": "SENSITIVE_APPROVED"}
        assert server_side.gettimeout() >= 120.0
        assert auth.calls == [("approve_pending_sensitive", OWNER_UID)]
    finally:
        server_side.close()
        client_side.close()
        server.server_close()


def test_zenity_confirmation_uses_argv_no_shell_and_denies_cancel_error_or_missing_helper(monkeypatch) -> None:
    request = NativeConfirmationRequest(
        kind="ENROLLMENT",
        verification_phrase="amber cedar river willow zephyr",
        auth_epoch=1,
        caller_uid=OWNER_UID,
        locator="locator-1",
        source="LOCAL_NATIVE",
        origin=ORIGIN,
        device_label="Primary browser",
    )
    observed: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class Result:
        returncode = 0

    def fake_run(argv, **kwargs):
        observed.append((tuple(argv), kwargs))
        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    confirmation = ZenityNativeConfirmation()
    assert confirmation.confirm(request) is True
    argv, kwargs = observed[-1]
    assert argv[0] == "zenity"
    assert "--question" in argv
    assert any("amber cedar river willow zephyr" in value for value in argv)
    assert any("Primary browser" in value for value in argv)
    assert any(ORIGIN in value for value in argv)
    assert kwargs["shell"] is False
    assert kwargs["check"] is False

    confirmation = ZenityNativeConfirmation(command_runner=lambda _argv: 1)
    assert confirmation.confirm(request) is False
    confirmation = ZenityNativeConfirmation(command_runner=lambda _argv: 5)
    assert confirmation.confirm(request) is False

    def missing(_argv):
        raise FileNotFoundError("zenity")

    confirmation = ZenityNativeConfirmation(command_runner=missing)
    assert confirmation.confirm(request) is False
