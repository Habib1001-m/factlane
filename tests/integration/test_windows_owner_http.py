from __future__ import annotations

import http.client
import json
import socket
import ssl
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows Owner HTTP qualification only")

from factlane.owner_auth import NativeConfirmationRequest, OwnerAuthState
from factlane.owner_http import REQUEST_TIMEOUT_SECONDS, WindowsOwnerHTTPServer


HOSTNAME = "factlane-a3transport.test"
TEST_IDENTITY = Path(__file__).parents[1] / "fixtures" / "windows_owner_http_test_identity.pem"
TEST_IDENTITY_PASSWORD = "factlane-test-only"


@dataclass
class FakeNativeConfirmation:
    approved: bool = True

    def confirm(self, request: NativeConfirmationRequest) -> bool:
        del request
        return self.approved


class LoopbackHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        raw = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _server_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(TEST_IDENTITY), password=TEST_IDENTITY_PASSWORD)
    return context


def _trust_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(TEST_IDENTITY))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    return context


def _server(**kwargs: object) -> WindowsOwnerHTTPServer:
    port = _loopback_port()
    return WindowsOwnerHTTPServer(
        origin=f"https://{HOSTNAME}:{port}",
        tls_context=_server_context(),
        auth_state=OwnerAuthState(owner_uid=1000, native_confirmation=FakeNativeConfirmation()),
        **kwargs,
    )


@contextmanager
def _serving(server: WindowsOwnerHTTPServer):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def _request(
    server: WindowsOwnerHTTPServer,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[http.client.HTTPResponse, bytes]:
    connection = LoopbackHTTPSConnection(
        HOSTNAME,
        server.origin_port,
        timeout=2,
        context=_trust_context(),
    )
    request_headers = {"Host": server.host, **(headers or {})}
    if body is not None:
        request_headers.setdefault("Content-Length", str(len(body)))
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response, payload


def _read_until_close(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        try:
            chunk = connection.recv(8192)
        except (ConnectionResetError, TimeoutError, socket.timeout):
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _response_cookies(response: http.client.HTTPResponse) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, value in response.getheaders():
        if name.lower() != "set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(value)
        for key, morsel in parsed.items():
            values[key] = morsel.value
    return values


def _unverified_client_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def test_windows_server_binds_exact_loopback_origin_port_exclusively_and_probe_verifies_tls() -> None:
    server = _server()
    try:
        assert server.server_address == ("127.0.0.1", server.origin_port)
        assert server.socket.getsockname() == ("127.0.0.1", server.origin_port)
        assert server.allow_reuse_address is False
        assert server.allow_reuse_port is False
        assert server.request_timeout_seconds == REQUEST_TIMEOUT_SECONDS
        assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1

        insecure = _unverified_client_context()
        with pytest.raises(ValueError, match="certificate and hostname verification"):
            server.probe_ready(trust_context=insecure)

        with _serving(server):
            assert server.probe_ready(trust_context=_trust_context(), timeout_seconds=1.0) is True
    finally:
        if server.fileno() != -1:
            server.server_close()


def test_windows_tls_rejects_plaintext_wrong_sni_and_absent_sni_without_http_response() -> None:
    server = _server()
    with _serving(server):
        plaintext = socket.create_connection(("127.0.0.1", server.origin_port), timeout=1)
        plaintext.settimeout(1)
        try:
            plaintext.sendall(
                f"GET /health HTTP/1.1\r\nHost: {server.host}\r\nConnection: close\r\n\r\n".encode("ascii")
            )
            assert b"HTTP/" not in _read_until_close(plaintext)
        finally:
            plaintext.close()

        for server_name in ("wrong.test", None):
            raw = socket.create_connection(("127.0.0.1", server.origin_port), timeout=1)
            raw.settimeout(1)
            tls = _unverified_client_context().wrap_socket(
                raw,
                server_hostname=server_name,
                do_handshake_on_connect=False,
            )
            try:
                with pytest.raises((ssl.SSLError, ConnectionResetError, OSError)):
                    tls.do_handshake()
            finally:
                tls.close()


def test_windows_tls_preserves_exact_host_origin_auth_cookie_and_forwarded_header_policy() -> None:
    server = _server()
    with _serving(server):
        response, payload = _request(server, "GET", "/health")
        assert response.status == 200
        assert payload == b'{"status":"OK"}'

        response, payload = _request(
            server,
            "GET",
            "/health",
            headers={"Host": "wrong.test", "X-Forwarded-Host": server.host, "X-Forwarded-Proto": "https"},
        )
        assert response.status == 403
        assert b'"code":"HOST_DENIED"' in payload

        response, payload = _request(
            server,
            "POST",
            "/auth/preauth",
            body=b"{}",
            headers={"Content-Type": "application/json", "Origin": "https://wrong.test"},
        )
        assert response.status == 403
        assert b'"code":"ORIGIN_DENIED"' in payload

        response, payload = _request(
            server,
            "POST",
            "/auth/preauth",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": server.origin,
                "X-Forwarded-For": "203.0.113.9",
                "X-Forwarded-Host": "evil.test",
                "X-Forwarded-Proto": "http",
            },
        )
        assert response.status == 200
        assert payload == b'{"status":"OK"}'
        cookies = [value for name, value in response.getheaders() if name.lower() == "set-cookie"]
        assert len(cookies) == 1
        assert cookies[0].startswith("__Host-factlane-preauth=")
        assert "HttpOnly" in cookies[0]
        assert "Path=/" in cookies[0]
        assert "SameSite=Strict" in cookies[0]
        assert "Secure" in cookies[0]

        preauth = _response_cookies(response)["__Host-factlane-preauth"]
        pending = server.auth_state.begin_enrollment(
            caller_uid=1000,
            source="local-launcher",
            device_label="Primary browser",
        )
        response, payload = _request(
            server,
            "POST",
            "/auth/connect/claim",
            body=json.dumps({"locator": pending.locator}, separators=(",", ":")).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": server.origin,
                "Cookie": f"__Host-factlane-preauth={preauth}",
            },
        )
        assert response.status == 200, payload
        server.auth_state.approve_pending(locator=pending.locator, caller_uid=1000)
        response, payload = _request(
            server,
            "POST",
            "/auth/connect/poll",
            body=json.dumps({"locator": pending.locator}, separators=(",", ":")).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": server.origin,
                "Cookie": f"__Host-factlane-preauth={preauth}",
            },
        )
        assert response.status == 200, payload
        connect_payload = json.loads(payload.decode("utf-8"))
        assert connect_payload["idle_remaining_seconds"] == pytest.approx(1_800.0, abs=1.0)
        assert connect_payload["absolute_remaining_seconds"] == pytest.approx(28_800.0, abs=1.0)
        assert "expires_at" not in connect_payload
        assert "absolute_expires_at" not in connect_payload
        session = _response_cookies(response)["__Host-factlane-session"]
        csrf = str(connect_payload["csrf"])

        response, payload = _request(
            server,
            "POST",
            "/auth/session/resume",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": server.origin,
                "Cookie": f"__Host-factlane-session={session}",
            },
        )
        assert response.status == 200, payload
        resume_payload = json.loads(payload.decode("utf-8"))
        assert resume_payload["csrf"] != csrf
        assert resume_payload["idle_remaining_seconds"] == pytest.approx(1_800.0, abs=1.0)
        assert resume_payload["absolute_remaining_seconds"] == pytest.approx(28_800.0, abs=1.0)
        assert not [value for name, value in response.getheaders() if name.lower() == "set-cookie"]
        old_csrf = csrf
        csrf = str(resume_payload["csrf"])

        response, payload = _request(
            server,
            "POST",
            "/api/write/create_candidate",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": server.origin,
                "Cookie": f"__Host-factlane-session={session}",
            },
        )
        assert response.status == 403
        assert b'"code":"CSRF_INVALID"' in payload

        response, payload = _request(
            server,
            "POST",
            "/api/write/create_candidate",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": server.origin,
                "Cookie": f"__Host-factlane-session={session}",
                "X-FactLane-CSRF": old_csrf,
            },
        )
        assert response.status == 403
        assert b'"code":"CSRF_INVALID"' in payload

        response, payload = _request(
            server,
            "POST",
            "/api/write/create_candidate",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": server.origin,
                "Cookie": f"__Host-factlane-session={session}",
                "X-FactLane-CSRF": csrf,
            },
        )
        assert response.status == 503
        assert b'"code":"SERVICE_UNAVAILABLE"' in payload


def test_windows_handshake_and_request_timeout_are_bounded_and_overload_closes_without_http() -> None:
    server = _server(max_concurrency=1, request_timeout_seconds=0.2)
    with _serving(server):
        first_raw = socket.create_connection(("127.0.0.1", server.origin_port), timeout=1)
        first = _trust_context().wrap_socket(first_raw, server_hostname=HOSTNAME)
        first.settimeout(1)
        try:
            first.sendall(
                (
                    "POST /auth/preauth HTTP/1.1\r\n"
                    f"Host: {server.host}\r\n"
                    f"Origin: {server.origin}\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: 10\r\n"
                    "Connection: close\r\n\r\n"
                    "{"
                ).encode("ascii")
            )
            time.sleep(0.03)

            overloaded_raw = socket.create_connection(("127.0.0.1", server.origin_port), timeout=1)
            overloaded_raw.settimeout(1)
            overloaded = _unverified_client_context().wrap_socket(
                overloaded_raw,
                server_hostname=HOSTNAME,
                do_handshake_on_connect=False,
            )
            started = time.monotonic()
            try:
                with pytest.raises((ssl.SSLError, ConnectionResetError, OSError)):
                    overloaded.do_handshake()
            finally:
                overloaded.close()
            assert time.monotonic() - started < 1.0

            response = _read_until_close(first)
            assert response.startswith(b"HTTP/1.1 408 Request Timeout")
            assert b'"code":"REQUEST_TIMEOUT"' in response
        finally:
            first.close()

        deadline = time.monotonic() + 1.0
        while not server.probe_ready(trust_context=_trust_context(), timeout_seconds=0.5):
            assert time.monotonic() < deadline
            time.sleep(0.01)


def test_windows_stalled_tls_handshake_times_out_and_releases_worker() -> None:
    server = _server(max_concurrency=1, request_timeout_seconds=0.2)
    with _serving(server):
        stalled = socket.create_connection(("127.0.0.1", server.origin_port), timeout=1)
        stalled.settimeout(1)
        started = time.monotonic()
        try:
            assert _read_until_close(stalled) == b""
        finally:
            stalled.close()
        elapsed = time.monotonic() - started
        assert 0.1 <= elapsed < 1.0

        deadline = time.monotonic() + 1.0
        while not server.probe_ready(trust_context=_trust_context(), timeout_seconds=0.5):
            assert time.monotonic() < deadline
            time.sleep(0.01)


@pytest.mark.parametrize(
    "origin",
    [
        "https://owner.test:8765",
        "https://factlane-a3transport.test",
        "https://FACTLANE-a3transport.test:8765",
        "http://factlane-a3transport.test:8765",
        "https://factlane-a3transport.test:0",
    ],
)
def test_windows_server_rejects_noncanonical_owner_origin(origin: str) -> None:
    with pytest.raises(ValueError):
        WindowsOwnerHTTPServer(
            origin=origin,
            tls_context=_server_context(),
            auth_state=OwnerAuthState(owner_uid=1000, native_confirmation=FakeNativeConfirmation()),
        )
