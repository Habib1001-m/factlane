from __future__ import annotations

import asyncio
import concurrent.futures
import errno
import inspect
import json
import mimetypes
import os
import re
import socket
import socketserver
import stat
import threading
import time
from dataclasses import asdict
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .contract import AdapterError, ScopeContext, validate_scope
from .owner_auth import AuthError, OwnerAuthState
from .owner_destructive import (
    FreshGrantVerifier,
    OwnerDestructiveError,
    PurgePreview,
    PurgeSuccess,
    PurgeTargetIdentity,
)
from .recovery import RecoveryScope


MAX_BODY_BYTES = 64 * 1024
DEFAULT_MAX_CONCURRENCY = 16
DEFAULT_QUEUE_SIZE = 32
REQUEST_TIMEOUT_SECONDS = 10.0
PURGE_EXECUTION_TIMEOUT_SECONDS = 60.0
UPLOAD_READ_PROGRESS_TIMEOUT_SECONDS = 30.0
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024
LOGICAL_IMPORT_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
RESTORE_UPLOAD_MAX_BYTES = 1024 * 1024 * 1024

PREAUTH_COOKIE = "__Host-factlane-preauth"
SESSION_COOKIE = "__Host-factlane-session"
CSRF_HEADER = "X-FactLane-CSRF"

_CSP = (
    "default-src 'self'; "
    "img-src 'self'; "
    "style-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "font-src 'none'; "
    "object-src 'none'; "
    "frame-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)

_ASSET_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/assets/app.css": "assets/app.css",
    "/assets/app.js": "assets/app.js",
    "/assets/factlane-mark.svg": "assets/factlane-mark.svg",
    "/assets/shell.js": "assets/shell.js",
    "/assets/views.js": "assets/views.js",
}

_SCOPE_FIELDS = ("scope", "project_id", "worktree_id", "workflow_id", "agent_id")
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DOWNLOAD_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_HEX_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_LOGICAL_EXPORT_LIFECYCLE_STATES = frozenset(
    {
        "CANDIDATE",
        "VALIDATED_CURRENT",
        "SUPERSEDED",
        "STALE",
        "QUARANTINED",
        "HISTORICAL",
        "REVOKED",
        "REJECTED",
    }
)


class OwnerOverviewLike(Protocol):
    async def overview(self, *, limit: int = 100, cursor: str | None = None, request_id: str | None = None) -> dict[str, Any]: ...

    async def search(self, **kwargs: Any) -> dict[str, Any]: ...


class OwnerControlLike(Protocol):
    async def inventory(self, **kwargs: Any) -> dict[str, Any]: ...

    async def detail(self, **kwargs: Any) -> dict[str, Any]: ...

    async def conflicts(self, **kwargs: Any) -> dict[str, Any]: ...

    async def create_candidate(self, **kwargs: Any) -> dict[str, Any]: ...

    async def batch_review(self, **kwargs: Any) -> dict[str, Any]: ...

    async def revoke(self, **kwargs: Any) -> dict[str, Any]: ...

    async def reject(self, **kwargs: Any) -> dict[str, Any]: ...

    async def reverify(self, **kwargs: Any) -> dict[str, Any]: ...

    async def replace(self, **kwargs: Any) -> dict[str, Any]: ...

    async def resolve(self, **kwargs: Any) -> dict[str, Any]: ...

    async def manual_recheck(self, **kwargs: Any) -> dict[str, Any]: ...

    async def readmit(self, **kwargs: Any) -> dict[str, Any]: ...


class OwnerOperationsLike(Protocol):
    async def housekeeping_preview(self, **kwargs: Any) -> dict[str, Any]: ...

    async def run_housekeeping(self, **kwargs: Any) -> dict[str, Any]: ...

    async def agent_inventory(self, *, request_id: str | None = None) -> dict[str, Any]: ...

    async def settings_snapshot(self, *, request_id: str | None = None) -> dict[str, Any]: ...

    async def settings_preview(self, changes: dict[str, Any], *, request_id: str | None = None) -> dict[str, Any]: ...

    async def settings_apply(self, changes: dict[str, Any], *, request_id: str | None = None) -> dict[str, Any]: ...

    async def support_report_preview(self, **kwargs: Any) -> dict[str, Any]: ...


class OwnerDestructiveLike(Protocol):
    async def preview(
        self,
        *,
        operation_id: str,
        targets: Sequence[PurgeTargetIdentity],
    ) -> PurgePreview: ...

    def recoverable_preview(self, operation_id: str) -> PurgePreview | None: ...

    async def execute(
        self,
        preview: PurgePreview,
        *,
        typed_confirmation: str,
        grant_secret: str,
        drain_deadline: float,
        verify_fresh_grant: FreshGrantVerifier | None = None,
    ) -> PurgeSuccess: ...


class OwnerUploadSinkLike(Protocol):
    upload_id: str

    def write(self, data: bytes) -> int: ...

    def seal(self) -> dict[str, Any]: ...

    def abort(self) -> None: ...


class OwnerDownloadHandleLike(Protocol):
    kind: str
    size_bytes: int
    sha256: str
    filename: str

    def read(self, maximum_bytes: int = UPLOAD_READ_CHUNK_BYTES) -> bytes: ...

    def close(self) -> None: ...


class OwnerDatabaseLike(Protocol):
    def begin_logical_import_upload(
        self, *, declared_size: int, expected_sha256: str | None = None
    ) -> OwnerUploadSinkLike: ...

    def begin_restore_upload(
        self, *, declared_size: int, expected_sha256: str | None = None
    ) -> OwnerUploadSinkLike: ...

    def list_artifacts(self) -> list[dict[str, Any]]: ...

    def open_download(self, artifact_id: str) -> OwnerDownloadHandleLike: ...

    def preview_artifact_delete(self, artifact_id: str) -> dict[str, Any]: ...

    def commit_artifact_delete(self, artifact_id: str, preview_digest: str) -> dict[str, Any]: ...

    def begin_backup_job(self, *, operation_id: str | None = None) -> dict[str, Any]: ...

    def begin_logical_export_job(
        self,
        *,
        selection: Mapping[str, Any],
        operation_id: str | None = None,
    ) -> dict[str, Any]: ...

    def begin_logical_import_preview_job(
        self, artifact_id: str, *, operation_id: str | None = None
    ) -> dict[str, Any]: ...

    def begin_logical_import_commit_job(self, artifact_id: str, *, operation_id: str) -> dict[str, Any]: ...

    def begin_restore_preview_job(
        self,
        artifact_id: str,
        *,
        operation_id: str | None = None,
        trusted_origin_attested: bool = False,
    ) -> dict[str, Any]: ...

    def begin_restore_commit_job(
        self,
        artifact_id: str,
        *,
        operation_id: str,
        state_restoration_acknowledged: bool,
    ) -> dict[str, Any]: ...

    def job_status(self, job_id: str) -> dict[str, Any]: ...

    async def run_job(self, job_id: str) -> dict[str, Any]: ...

    async def run_restore_job(
        self,
        job_id: str,
        *,
        authorize: Callable[[str], Any | Awaitable[Any]],
    ) -> dict[str, Any]: ...


class _ServiceRunnerTimeout(TimeoutError):
    pass


class _AsyncServiceRunner:
    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._closed = False
        self.thread = threading.Thread(
            target=self._run_loop,
            name="factlane-owner-http-async",
            daemon=True,
        )
        self.thread.start()
        if not self._started.wait(timeout=2.0):
            raise RuntimeError("Owner HTTP async service loop failed to start")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def run(self, awaitable: Awaitable[Any], *, timeout_seconds: float | None = None) -> Any:
        if self._closed:
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[attr-defined]
            raise _HTTPFailure(503, "SERVICE_UNAVAILABLE")
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        try:
            return future.result(timeout=self._timeout_seconds if timeout_seconds is None else timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise _ServiceRunnerTimeout from exc

    def submit(self, awaitable: Awaitable[Any]) -> None:
        if self._closed:
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[attr-defined]
            raise _HTTPFailure(503, "SERVICE_UNAVAILABLE")
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)

        def consume_result(done: concurrent.futures.Future[Any]) -> None:
            try:
                done.result()
            except (concurrent.futures.CancelledError, Exception):
                return

        future.add_done_callback(consume_result)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self.thread.join(timeout=2.0)

    @property
    def thread_alive(self) -> bool:
        return self.thread.is_alive()


class _OwnerHTTPRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FactLaneOwnerHTTP/1"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802
        self.server.dispatch(self, "GET")  # type: ignore[attr-defined]

    def do_POST(self) -> None:  # noqa: N802
        self.server.dispatch(self, "POST")  # type: ignore[attr-defined]

    def do_PUT(self) -> None:  # noqa: N802
        self.server.method_not_allowed(self)  # type: ignore[attr-defined]

    def do_PATCH(self) -> None:  # noqa: N802
        self.server.method_not_allowed(self)  # type: ignore[attr-defined]

    def do_DELETE(self) -> None:  # noqa: N802
        self.server.method_not_allowed(self)  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        # Requests intentionally do not enter stdlib access logs. Browser auth
        # credentials, CSRF values and sensitive grants must never be logged.
        return


class OwnerHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Browser-facing Owner HTTP service bound only to a private AF_UNIX socket.

    TLS termination and public/private-network policy live outside this process.
    This server accepts the configured HTTPS origin only, ignores forwarded
    headers, and never opens a TCP listener.
    """

    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False

    def __init__(
        self,
        *,
        socket_path: str | os.PathLike[str],
        origin: str,
        auth_state: OwnerAuthState,
        overview_service: OwnerOverviewLike | None = None,
        control_service: OwnerControlLike | None = None,
        operations_service: OwnerOperationsLike | None = None,
        destructive_service: OwnerDestructiveLike | None = None,
        database_service: OwnerDatabaseLike | None = None,
        webui_root: str | os.PathLike[str] | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.origin, self.host = self._validate_configured_origin(origin)
        self.auth_state = auth_state
        self.overview_service = overview_service
        self.control_service = control_service
        self.operations_service = operations_service
        self.destructive_service = destructive_service
        self.database_service = database_service
        self.webui_root = (
            Path(webui_root).resolve()
            if webui_root is not None
            else Path(__file__).resolve().with_name("webui")
        )
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or not 1 <= max_concurrency <= 64:
            raise ValueError("max_concurrency must be an integer from 1 through 64")
        if isinstance(queue_size, bool) or not isinstance(queue_size, int) or not 1 <= queue_size <= 128:
            raise ValueError("queue_size must be an integer from 1 through 128")
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or request_timeout_seconds <= 0
            or request_timeout_seconds > REQUEST_TIMEOUT_SECONDS
        ):
            raise ValueError("request_timeout_seconds must be greater than zero and at most 10 seconds")
        self.request_queue_size = queue_size
        self.request_timeout_seconds = float(request_timeout_seconds)
        self._worker_slots = threading.BoundedSemaphore(max_concurrency)
        self._socket_identity: tuple[int, int] | None = None
        self._async_runner: _AsyncServiceRunner | None = None
        self._serving_started = False
        self._services_configured = any(
            service is not None
            for service in (overview_service, control_service, operations_service, destructive_service, database_service)
        )
        self._async_cleanups: list[Callable[[], Any]] = []
        self._purge_previews: dict[str, PurgePreview] = {}
        self._purge_preview_lock = threading.Lock()
        self._prepare_socket_path()
        try:
            super().__init__(str(self.socket_path), _OwnerHTTPRequestHandler)
            os.chmod(self.socket_path, 0o600)
            bound = self.socket_path.stat()
            self._socket_identity = (bound.st_dev, bound.st_ino)
            self._async_runner = _AsyncServiceRunner(timeout_seconds=self.request_timeout_seconds)
        except BaseException:
            if self._async_runner is not None:
                self._async_runner.close()
            self._unlink_own_socket()
            raise

    @property
    def service_loop_thread_alive(self) -> bool:
        return self._async_runner is not None and self._async_runner.thread_alive

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self._serving_started = True
        return super().serve_forever(poll_interval=poll_interval)

    def run_service_coroutine(
        self,
        awaitable_or_factory: Awaitable[Any] | Callable[[], Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        if self._serving_started:
            raise RuntimeError("Owner HTTP service composition is closed after serving starts")
        if self._async_runner is None:
            raise RuntimeError("Owner HTTP async service loop is unavailable")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 60.0
        ):
            raise ValueError("timeout must be greater than zero and at most 60 seconds")
        try:
            return self._async_runner.run(
                self._composition_awaitable(awaitable_or_factory),
                timeout_seconds=float(timeout) if timeout is not None else None,
            )
        except _ServiceRunnerTimeout:
            raise TimeoutError("Owner HTTP service coroutine timed out") from None

    def configure_services(
        self,
        *,
        overview_service: OwnerOverviewLike | None,
        control_service: OwnerControlLike | None,
        operations_service: OwnerOperationsLike | None,
        destructive_service: OwnerDestructiveLike | None = None,
        database_service: OwnerDatabaseLike | None = None,
    ) -> None:
        if self._serving_started:
            raise RuntimeError("Owner HTTP services cannot be configured after serving starts")
        if self._services_configured:
            raise RuntimeError("Owner HTTP services are already configured")
        self.overview_service = overview_service
        self.control_service = control_service
        self.operations_service = operations_service
        self.destructive_service = destructive_service
        self.database_service = database_service
        self._services_configured = True

    def register_async_cleanup(self, cleanup_factory: Callable[[], Any]) -> None:
        if self._serving_started:
            raise RuntimeError("Owner HTTP cleanup must be registered before serving starts")
        if not callable(cleanup_factory):
            raise TypeError("cleanup_factory must be callable")
        self._async_cleanups.append(cleanup_factory)

    @staticmethod
    async def _composition_awaitable(awaitable_or_factory: Awaitable[Any] | Callable[[], Any]) -> Any:
        value = awaitable_or_factory() if callable(awaitable_or_factory) else awaitable_or_factory
        if inspect.isawaitable(value):
            return await value
        return value

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout_seconds)
        return request, client_address

    @staticmethod
    def _validate_configured_origin(origin: str) -> tuple[str, str]:
        if not isinstance(origin, str) or not origin or len(origin) > 2048:
            raise ValueError("origin must be a bounded HTTPS origin")
        parsed = urlsplit(origin)
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("origin contains an invalid port") from exc
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("origin must be an exact HTTPS origin without path, query, or fragment")
        return origin, parsed.netloc

    def _prepare_socket_path(self) -> None:
        parent = self.socket_path.parent
        if parent.exists():
            if not parent.is_dir():
                raise NotADirectoryError(parent)
            if stat.S_IMODE(parent.stat().st_mode) != 0o700:
                raise PermissionError("owner HTTP socket directory must already be mode 0700")
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
            raise OSError(errno.EADDRINUSE, "owner HTTP socket is already accepting connections")
        finally:
            probe.close()
        self.socket_path.unlink()

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._worker_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 56\r\n"
                    b"X-Content-Type-Options: nosniff\r\n"
                    b"Cache-Control: no-store\r\n"
                    b"Connection: close\r\n\r\n"
                    b'{"status":"ERROR","code":"SERVER_BUSY","retryable":true}'
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()

    def handle_error(self, request: socket.socket, client_address: Any) -> None:
        # Avoid stdlib traceback/access output from transport failures. Request
        # bodies and authentication material must never be copied to logs.
        return

    def server_close(self) -> None:
        cleanup_failed = False
        try:
            super().server_close()
        finally:
            try:
                if self._async_runner is not None:
                    for cleanup in reversed(self._async_cleanups):
                        try:
                            self._async_runner.run(self._composition_awaitable(cleanup))
                        except Exception:
                            cleanup_failed = True
                    self._async_cleanups.clear()
            finally:
                if self._async_runner is not None:
                    self._async_runner.close()
                self._unlink_own_socket()
        if cleanup_failed:
            raise RuntimeError("Owner HTTP async cleanup failed") from None

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

    def method_not_allowed(self, handler: _OwnerHTTPRequestHandler) -> None:
        self._json(handler, 405, {"status": "ERROR", "code": "METHOD_NOT_ALLOWED"}, extra_headers={"Allow": "GET, POST"})

    def dispatch(self, handler: _OwnerHTTPRequestHandler, method: str) -> None:
        try:
            path = self._request_path(handler)
            self._require_exact_host(handler)
            if method == "GET":
                self._dispatch_get(handler, path)
                return
            if method == "POST":
                self._require_exact_origin(handler)
                if path in {"/api/database/import/upload", "/api/database/restore/upload"}:
                    self._dispatch_raw_database_upload(handler, path)
                    return
                body = self._read_json_body(handler)
                self._dispatch_post(handler, path, body)
                return
            self.method_not_allowed(handler)
        except _HTTPFailure as exc:
            self._json(handler, exc.status, {"status": "ERROR", "code": exc.code, **exc.payload})
        except AuthError as exc:
            self._auth_error(handler, exc)
        except AdapterError as exc:
            self._adapter_error(handler, exc)
        except OwnerDestructiveError as exc:
            self._destructive_error(handler, exc)
        except (TimeoutError, socket.timeout):
            self._json(handler, 408, {"status": "ERROR", "code": "REQUEST_TIMEOUT"})
        except (ValueError, TypeError):
            self._json(handler, 400, {"status": "ERROR", "code": "INVALID_REQUEST"})
        except Exception:
            self._json(handler, 500, {"status": "ERROR", "code": "INTERNAL_ERROR"})

    def _dispatch_get(self, handler: _OwnerHTTPRequestHandler, path: str) -> None:
        if path == "/health":
            self._json(handler, 200, {"status": "OK"})
            return
        download_match = re.fullmatch(
            r"/api/database/artifacts/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/download",
            path,
        )
        if download_match is not None:
            self._authenticated(handler, {})
            service = self._require_database_service()
            handle = service.open_download(download_match.group(1))
            self._stream_download(handler, handle)
            return
        asset = _ASSET_FILES.get(path)
        if asset is None:
            raise _HTTPFailure(404, "NOT_FOUND")
        target = (self.webui_root / asset).resolve()
        try:
            target.relative_to(self.webui_root)
        except ValueError as exc:
            raise _HTTPFailure(404, "NOT_FOUND") from exc
        if not target.is_file():
            raise _HTTPFailure(404, "NOT_FOUND")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            content_type = "text/javascript"
        self._bytes(handler, 200, target.read_bytes(), content_type=content_type)

    def _dispatch_post(self, handler: _OwnerHTTPRequestHandler, path: str, body: dict[str, Any]) -> None:
        if path == "/auth/preauth":
            self._only_fields(body, set())
            preauth = self.auth_state.issue_preauth(origin=self.origin)
            self._json(
                handler,
                200,
                {"status": "OK"},
                extra_headers={"Set-Cookie": self._cookie(PREAUTH_COOKIE, preauth)},
            )
            return

        if path == "/auth/connect/claim":
            self._only_fields(body, {"locator"})
            locator = self._required_string(body, "locator", 512)
            preauth = self._required_cookie(handler, PREAUTH_COOKIE)
            claim = self.auth_state.claim_pending(locator=locator, origin=self.origin, preauth_secret=preauth)
            self._json(handler, 200, {"status": "OK", **asdict(claim)})
            return

        if path == "/auth/enroll/remote":
            self._only_fields(body, {"device_label"})
            device_label = self._required_string(body, "device_label", 256)
            preauth = self._required_cookie(handler, PREAUTH_COOKIE)
            claim = self.auth_state.begin_remote_enrollment(
                source="AF_UNIX_BROWSER",
                device_label=device_label,
                origin=self.origin,
                preauth_secret=preauth,
            )
            self._json(handler, 200, {"status": "OK", **asdict(claim)})
            return

        if path == "/auth/connect/poll":
            self._only_fields(body, {"locator"})
            locator = self._required_string(body, "locator", 512)
            preauth = self._required_cookie(handler, PREAUTH_COOKIE)
            credentials = self.auth_state.poll_pending(
                locator=locator,
                origin=self.origin,
                preauth_secret=preauth,
            )
            if credentials is None:
                self._json(handler, 202, {"status": "PENDING"})
                return
            headers = [
                ("Set-Cookie", self._cookie(SESSION_COOKIE, credentials.session_secret)),
                ("Set-Cookie", self._clear_cookie(PREAUTH_COOKIE)),
            ]
            self._json(
                handler,
                200,
                {
                    "status": "OK",
                    "csrf": credentials.csrf_secret,
                    "expires_at": credentials.expires_at,
                    "absolute_expires_at": credentials.absolute_expires_at,
                    "auth_epoch": credentials.auth_epoch,
                    "primary_local": credentials.primary_local,
                },
                extra_header_items=headers,
            )
            return

        if path == "/auth/logout":
            self._only_fields(body, {"csrf"})
            session_secret, _ = self._authenticated(handler, body)
            self.auth_state.logout(session_secret)
            self._json(
                handler,
                200,
                {"status": "OK"},
                extra_headers={"Set-Cookie": self._clear_cookie(SESSION_COOKIE)},
            )
            return

        if path == "/auth/remote/preview":
            self._only_fields(body, {"locator", "csrf"})
            session_secret, _ = self._authenticated(handler, body)
            locator = self._required_string(body, "locator", 512)
            preview = self.auth_state.remote_enrollment_preview(
                locator=locator,
                session_secret=session_secret,
                origin=self.origin,
            )
            self._json(handler, 200, {"status": "OK", "preview": preview})
            return

        if path == "/auth/sensitive/challenge":
            self._only_fields(body, {"preview", "csrf"})
            session_secret, _ = self._authenticated(handler, body)
            preview = self._required_string(body, "preview", 16_384)
            challenge = self.auth_state.begin_sensitive_challenge(
                session_secret=session_secret,
                origin=self.origin,
                preview=preview,
            )
            self._json(
                handler,
                200,
                {
                    "status": "OK",
                    "challenge_id": challenge.challenge_id,
                    "verification_phrase": challenge.verification_phrase,
                },
            )
            return

        if path == "/auth/sensitive/grant":
            self._only_fields(body, {"challenge_id", "csrf"})
            session_secret, _ = self._authenticated(handler, body)
            challenge_id = self._required_string(body, "challenge_id", 512)
            grant = self.auth_state.mint_sensitive_grant(
                challenge_id=challenge_id,
                session_secret=session_secret,
                origin=self.origin,
            )
            self._json(handler, 200, {"status": "OK", "grant": grant.grant_secret, "expires_at": grant.expires_at})
            return

        if path == "/auth/remote/approve":
            self._only_fields(body, {"locator", "grant", "csrf"})
            session_secret, csrf = self._authenticated(handler, body)
            locator = self._required_string(body, "locator", 512)
            grant = self._required_string(body, "grant", 1024)
            claim = self.auth_state.approve_remote_enrollment(
                locator=locator,
                session_secret=session_secret,
                origin=self.origin,
                csrf_secret=csrf,
                grant_secret=grant,
            )
            self._json(handler, 200, {"status": "OK", **asdict(claim)})
            return

        if path == "/api/read/overview":
            self._only_fields(body, {"limit", "cursor", "request_id", "csrf"})
            self._authenticated(handler, body)
            service = self._require_overview_service()
            result = self._run(
                service.overview(
                    limit=self._optional_int(body, "limit", default=100, minimum=1, maximum=10_000),
                    cursor=self._optional_string(body, "cursor", 16_384),
                    request_id=self._optional_string(body, "request_id", 512),
                )
            )
            self._json(handler, 200, result)
            return

        if path == "/api/read/conflicts":
            self._only_fields(body, {*_SCOPE_FIELDS, "limit", "cursor", "request_id", "csrf"})
            self._authenticated(handler, body)
            service = self._require_control_service()
            scope = self._exact_scope(body)
            result = self._run(
                service.conflicts(
                    **self._scope_kwargs(scope),
                    limit=self._optional_int(body, "limit", default=100, minimum=1, maximum=10_000),
                    cursor=self._optional_string(body, "cursor", 16_384),
                    request_id=self._optional_string(body, "request_id", 512),
                )
            )
            self._json(handler, 200, result)
            return

        if path == "/api/write/create_candidate":
            self._only_fields(
                body,
                {
                    *_SCOPE_FIELDS,
                    "fact",
                    "memory_type",
                    "source_provenance",
                    "freshness_policy",
                    "idempotency_key",
                    "source_timestamp",
                    "confidence",
                    "tags",
                    "subject",
                    "request_id",
                    "csrf",
                },
            )
            self._authenticated(handler, body)
            service = self._require_control_service()
            scope = self._exact_scope(body)
            confidence = body.get("confidence", 0.5)
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise _HTTPFailure(400, "INVALID_ENVELOPE")
            result = self._run(
                service.create_candidate(
                    **self._scope_kwargs(scope),
                    fact=self._required_string(body, "fact", 4096),
                    memory_type=self._required_string(body, "memory_type", 128),
                    source_provenance=self._required_object(body, "source_provenance"),
                    freshness_policy=self._required_object(body, "freshness_policy"),
                    idempotency_key=self._required_string(body, "idempotency_key", 512),
                    source_timestamp=self._optional_string(body, "source_timestamp", 128),
                    confidence=float(confidence),
                    tags=self._optional_string_list(body, "tags", maximum_items=64, maximum_string=256),
                    subject=self._optional_string(body, "subject", 1024),
                    request_id=self._optional_string(body, "request_id", 512),
                )
            )
            self._json(handler, 200, result)
            return

        if path == "/api/write/batch_review":
            self._only_fields(body, {*_SCOPE_FIELDS, "items", "request_id", "csrf"})
            self._authenticated(handler, body)
            service = self._require_control_service()
            scope = self._exact_scope(body)
            result = self._run(
                service.batch_review(
                    **self._scope_kwargs(scope),
                    items=self._required_object_list(body, "items", maximum_items=50),
                    request_id=self._optional_string(body, "request_id", 512),
                )
            )
            self._json(handler, 200, result)
            return

        if path in {"/api/write/revoke", "/api/write/reject"}:
            self._only_fields(
                body,
                {
                    *_SCOPE_FIELDS,
                    "memory_id",
                    "expected_record_id",
                    "expected_revision",
                    "observed_group_token",
                    "idempotency_key",
                    "reason_code",
                    "request_id",
                    "csrf",
                },
            )
            self._authenticated(handler, body)
            service = self._require_control_service()
            kwargs = self._transition_kwargs(body)
            kwargs["reason_code"] = self._required_string(body, "reason_code", 256)
            if path == "/api/write/revoke":
                result = self._run(service.revoke(**kwargs))
            else:
                result = self._run(service.reject(**kwargs))
            self._json(handler, 200, result)
            return

        if path == "/api/write/reverify":
            self._only_fields(
                body,
                {
                    *_SCOPE_FIELDS,
                    "memory_id",
                    "expected_record_id",
                    "expected_revision",
                    "observed_group_token",
                    "idempotency_key",
                    "verification",
                    "request_id",
                    "csrf",
                },
            )
            self._authenticated(handler, body)
            service = self._require_control_service()
            kwargs = self._transition_kwargs(body)
            kwargs["verification"] = self._required_object(body, "verification")
            result = self._run(service.reverify(**kwargs))
            self._json(handler, 200, result)
            return

        if path == "/api/write/replace":
            self._only_fields(
                body,
                {
                    *_SCOPE_FIELDS,
                    "memory_id",
                    "expected_record_id",
                    "expected_revision",
                    "observed_group_token",
                    "idempotency_key",
                    "replacement",
                    "request_id",
                    "csrf",
                },
            )
            self._authenticated(handler, body)
            service = self._require_control_service()
            kwargs = self._transition_kwargs(body)
            kwargs["replacement"] = self._required_object(body, "replacement")
            result = self._run(service.replace(**kwargs))
            self._json(handler, 200, result)
            return

        if path == "/api/write/manual_recheck":
            self._only_fields(
                body,
                {
                    *_SCOPE_FIELDS,
                    "memory_id",
                    "expected_record_id",
                    "expected_revision",
                    "observed_group_token",
                    "idempotency_key",
                    "evidence",
                    "request_id",
                    "csrf",
                },
            )
            self._authenticated(handler, body)
            service = self._require_control_service()
            kwargs = self._transition_kwargs(body)
            kwargs["evidence"] = self._required_object(body, "evidence")
            result = self._run(service.manual_recheck(**kwargs))
            self._json(handler, 200, result)
            return

        if path == "/api/write/resolve":
            self._only_fields(
                body,
                {
                    *_SCOPE_FIELDS,
                    "contradiction_key",
                    "observed_group_token",
                    "winner_memory_id",
                    "winner_record_id",
                    "winner_expected_revision",
                    "idempotency_key",
                    "reason_code",
                    "verification",
                    "request_id",
                    "csrf",
                },
            )
            self._authenticated(handler, body)
            service = self._require_control_service()
            scope = self._exact_scope(body)
            result = self._run(
                service.resolve(
                    **self._scope_kwargs(scope),
                    contradiction_key=self._required_string(body, "contradiction_key", 2048),
                    observed_group_token=self._required_string(body, "observed_group_token", 4096),
                    winner_memory_id=self._required_string(body, "winner_memory_id", 512),
                    winner_record_id=self._required_string(body, "winner_record_id", 512),
                    winner_expected_revision=self._required_int(body, "winner_expected_revision", minimum=1),
                    idempotency_key=self._required_string(body, "idempotency_key", 512),
                    reason_code=self._required_string(body, "reason_code", 256),
                    verification=self._optional_object(body, "verification"),
                    request_id=self._optional_string(body, "request_id", 512),
                )
            )
            self._json(handler, 200, result)
            return

        if path == "/api/write/readmit":
            self._only_fields(
                body,
                {
                    *_SCOPE_FIELDS,
                    "memory_id",
                    "revoked_record_id",
                    "expected_revoked_revision",
                    "observed_group_token",
                    "idempotency_key",
                    "verification",
                    "request_id",
                    "csrf",
                },
            )
            self._authenticated(handler, body)
            service = self._require_control_service()
            scope = self._exact_scope(body)
            result = self._run(
                service.readmit(
                    **self._scope_kwargs(scope),
                    memory_id=self._required_string(body, "memory_id", 512),
                    revoked_record_id=self._required_string(body, "revoked_record_id", 512),
                    expected_revoked_revision=self._required_int(body, "expected_revoked_revision", minimum=1),
                    observed_group_token=self._required_string(body, "observed_group_token", 4096),
                    idempotency_key=self._required_string(body, "idempotency_key", 512),
                    verification=self._required_object(body, "verification"),
                    request_id=self._optional_string(body, "request_id", 512),
                )
            )
            self._json(handler, 200, result)
            return

        if path == "/api/write/purge/preview":
            self._only_fields(body, {"operation_id", "targets", "csrf"})
            self._authenticated(handler, body)
            service = self._require_destructive_service()
            operation_id = self._required_string(body, "operation_id", 64)
            targets = self._purge_targets(body)
            preview = self._run(service.preview(operation_id=operation_id, targets=targets))
            if not isinstance(preview, PurgePreview):
                raise _HTTPFailure(502, "DESTRUCTIVE_RESPONSE_INVALID")
            if preview.operation_id != operation_id or tuple(preview.targets) != tuple(targets):
                raise _HTTPFailure(409, "PURGE_PREVIEW_BINDING_MISMATCH")
            with self._purge_preview_lock:
                existing = self._purge_previews.get(operation_id)
                if existing is not None:
                    if existing.preview_digest != preview.preview_digest or existing.targets != preview.targets:
                        raise _HTTPFailure(409, "PURGE_OPERATION_ALREADY_BOUND")
                    preview = existing
                else:
                    self._purge_previews[operation_id] = preview
            self._json(
                handler,
                200,
                {
                    "status": "OK",
                    "preview": preview.to_mapping(),
                    "authorization_preview": preview.authorization_preview,
                },
            )
            return

        if path in {"/api/write/purge/commit", "/api/write/purge/retry"}:
            self._only_fields(
                body,
                {"operation_id", "preview_digest", "typed_confirmation", "grant_secret", "csrf"},
            )
            session_secret, _ = self._authenticated(handler, body)
            service = self._require_destructive_service()
            operation_id = self._required_string(body, "operation_id", 64)
            preview_digest = self._required_hex_digest(body, "preview_digest")
            typed_confirmation = self._required_string(body, "typed_confirmation", 256)
            grant_secret = self._required_string(body, "grant_secret", 2048)
            with self._purge_preview_lock:
                preview = self._purge_previews.get(operation_id)
            if preview is None:
                recovered = service.recoverable_preview(operation_id)
                if recovered is None:
                    raise _HTTPFailure(409, "PURGE_PREVIEW_REQUIRED")
                if not isinstance(recovered, PurgePreview):
                    raise _HTTPFailure(502, "DESTRUCTIVE_RESPONSE_INVALID")
                if recovered.operation_id != operation_id:
                    raise _HTTPFailure(409, "PURGE_PREVIEW_BINDING_MISMATCH")
                if recovered.preview_digest != preview_digest:
                    raise _HTTPFailure(409, "PURGE_PREVIEW_MISMATCH")
                with self._purge_preview_lock:
                    existing = self._purge_previews.get(operation_id)
                    if existing is not None:
                        if existing.preview_digest != recovered.preview_digest or existing.targets != recovered.targets:
                            raise _HTTPFailure(409, "PURGE_OPERATION_ALREADY_BOUND")
                        preview = existing
                    else:
                        self._purge_previews[operation_id] = recovered
                        preview = recovered
            if preview.preview_digest != preview_digest:
                raise _HTTPFailure(409, "PURGE_PREVIEW_MISMATCH")

            async def verify_fresh_grant(*, grant_secret: str, preview: str) -> None:
                self.auth_state.consume_sensitive_grant(
                    grant_secret=grant_secret,
                    session_secret=session_secret,
                    origin=self.origin,
                    preview=preview,
                )

            result = self._run(
                service.execute(
                    preview,
                    typed_confirmation=typed_confirmation,
                    grant_secret=grant_secret,
                    drain_deadline=time.time() + 30.0,
                    verify_fresh_grant=verify_fresh_grant,
                ),
                timeout_seconds=PURGE_EXECUTION_TIMEOUT_SECONDS,
            )
            if not isinstance(result, PurgeSuccess):
                raise _HTTPFailure(502, "DESTRUCTIVE_RESPONSE_INVALID")
            if result.operation_id != preview.operation_id or result.preview_digest != preview.preview_digest:
                raise _HTTPFailure(409, "PURGE_RESULT_BINDING_MISMATCH")
            self._json(handler, 200, self._purge_success_payload(result))
            return

        if path in {
            "/api/operations/housekeeping_preview",
            "/api/operations/run_housekeeping",
            "/api/operations/support_report_preview",
        }:
            self._only_fields(
                body,
                {*_SCOPE_FIELDS, "max_records", "required_free_bytes", "request_id", "csrf"},
            )
            self._authenticated(handler, body)
            service = self._require_operations_service()
            scope = self._exact_scope(body)
            kwargs = {
                **self._scope_kwargs(scope),
                "max_records": self._required_int(body, "max_records", minimum=1),
                "required_free_bytes": self._required_int(body, "required_free_bytes", minimum=0),
                "request_id": self._optional_string(body, "request_id", 512),
            }
            if path == "/api/operations/housekeeping_preview":
                result = self._run(service.housekeeping_preview(**kwargs))
            elif path == "/api/operations/run_housekeeping":
                result = self._run(service.run_housekeeping(**kwargs))
            else:
                result = self._run(service.support_report_preview(**kwargs))
            self._json(handler, 200, result)
            return

        if path in {"/api/operations/agent_inventory", "/api/operations/settings_snapshot"}:
            self._only_fields(body, {"request_id", "csrf"})
            self._authenticated(handler, body)
            service = self._require_operations_service()
            request_id = self._optional_string(body, "request_id", 512)
            if path == "/api/operations/agent_inventory":
                result = self._run(service.agent_inventory(request_id=request_id))
            else:
                result = self._run(service.settings_snapshot(request_id=request_id))
            self._json(handler, 200, result)
            return

        if path in {"/api/operations/settings_preview", "/api/operations/settings_apply"}:
            self._only_fields(body, {"changes", "request_id", "csrf"})
            self._authenticated(handler, body)
            service = self._require_operations_service()
            changes = self._required_object(body, "changes")
            request_id = self._optional_string(body, "request_id", 512)
            if path == "/api/operations/settings_preview":
                result = self._run(service.settings_preview(changes, request_id=request_id))
            else:
                result = self._run(service.settings_apply(changes, request_id=request_id))
            self._json(handler, 200, result)
            return

        if path == "/api/read/search":
            allowed = {
                *_SCOPE_FIELDS,
                "query",
                "search_mode",
                "lifecycle_state",
                "source_ref",
                "created_after",
                "created_before",
                "limit",
                "cursor",
                "request_id",
                "csrf",
            }
            self._only_fields(body, allowed)
            self._authenticated(handler, body)
            scope = self._exact_scope(body)
            kwargs = self._scope_kwargs(scope)
            kwargs.update(
                {
                    "query": self._required_string(body, "query", 4096),
                    "search_mode": body.get("search_mode", "TEXT"),
                    "lifecycle_state": body.get("lifecycle_state"),
                    "source_ref": body.get("source_ref"),
                    "created_after": body.get("created_after"),
                    "created_before": body.get("created_before"),
                    "limit": body.get("limit", 100),
                    "cursor": body.get("cursor"),
                    "request_id": body.get("request_id"),
                }
            )
            result = self._run(self._require_overview_service().search(**kwargs))
            self._json(handler, 200, result)
            return

        if path == "/api/read/inventory":
            allowed = {
                *_SCOPE_FIELDS,
                "lifecycle_state",
                "contradiction_state",
                "memory_type",
                "limit",
                "cursor",
                "request_id",
                "csrf",
            }
            self._only_fields(body, allowed)
            self._authenticated(handler, body)
            scope = self._exact_scope(body)
            kwargs = self._scope_kwargs(scope)
            kwargs.update(
                {
                    "lifecycle_state": body.get("lifecycle_state"),
                    "contradiction_state": body.get("contradiction_state"),
                    "memory_type": body.get("memory_type"),
                    "limit": body.get("limit", 100),
                    "cursor": body.get("cursor"),
                    "request_id": body.get("request_id"),
                }
            )
            result = self._run(self._require_control_service().inventory(**kwargs))
            self._json(handler, 200, result)
            return

        if path == "/api/read/detail":
            allowed = {*_SCOPE_FIELDS, "memory_id", "limit", "cursor", "event_cursor", "request_id", "csrf"}
            self._only_fields(body, allowed)
            self._authenticated(handler, body)
            scope = self._exact_scope(body)
            kwargs = self._scope_kwargs(scope)
            kwargs.update(
                {
                    "memory_id": self._required_string(body, "memory_id", 512),
                    "limit": body.get("limit", 100),
                    "cursor": body.get("cursor"),
                    "event_cursor": body.get("event_cursor"),
                    "request_id": body.get("request_id"),
                }
            )
            result = self._run(self._require_control_service().detail(**kwargs))
            self._json(handler, 200, result)
            return

        if path == "/api/database/artifacts/list":
            self._only_fields(body, {"csrf"})
            self._authenticated(handler, body)
            artifacts = self._database_public_value(self._require_database_service().list_artifacts())
            if not isinstance(artifacts, list):
                raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")
            self._json(handler, 200, {"status": "OK", "artifacts": artifacts})
            return

        if path == "/api/database/artifacts/delete/preview":
            self._only_fields(body, {"artifact_id", "csrf"})
            self._authenticated(handler, body)
            artifact_id = self._required_artifact_id(body, "artifact_id")
            preview = self._database_public_value(
                self._require_database_service().preview_artifact_delete(artifact_id)
            )
            if not isinstance(preview, dict):
                raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")
            self._json(handler, 200, {"status": "OK", "preview": preview})
            return

        if path == "/api/database/artifacts/delete/commit":
            self._only_fields(body, {"artifact_id", "preview_digest", "csrf"})
            self._authenticated(handler, body)
            artifact_id = self._required_artifact_id(body, "artifact_id")
            preview_digest = self._required_hex_digest(body, "preview_digest")
            result = self._database_public_value(
                self._require_database_service().commit_artifact_delete(artifact_id, preview_digest)
            )
            if not isinstance(result, dict):
                raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")
            self._json(handler, 200, {"status": "OK", "result": result})
            return

        if path == "/api/database/backup/create":
            self._only_fields(body, {"operation_id", "csrf"})
            self._authenticated(handler, body)
            service = self._require_database_service()
            operation_id = self._optional_string(body, "operation_id", 64)
            job = service.begin_backup_job(operation_id=operation_id)
            self._queue_database_job(service, job)
            self._json(handler, 202, {"status": "ACCEPTED", "job": self._database_public_value(job)})
            return

        if path == "/api/database/export/create":
            self._only_fields(
                body,
                {
                    *_SCOPE_FIELDS,
                    "lifecycle_states",
                    "max_records",
                    "operation_id",
                    "csrf",
                },
            )
            self._authenticated(handler, body)
            scope = self._exact_scope(body)
            lifecycle_states = self._required_export_lifecycle_states(body)
            max_records = self._required_int(body, "max_records", minimum=1, maximum=1000)
            operation_id = self._optional_string(body, "operation_id", 64)
            service = self._require_database_service()
            selection = {
                **self._scope_kwargs(scope),
                "lifecycle_states": lifecycle_states,
                "max_records": max_records,
            }
            job = service.begin_logical_export_job(selection=selection, operation_id=operation_id)
            self._queue_database_job(service, job)
            self._json(handler, 202, {"status": "ACCEPTED", "job": self._database_public_value(job)})
            return

        if path in {"/api/database/import/preview", "/api/database/import/commit"}:
            self._only_fields(body, {"artifact_id", "operation_id", "csrf"})
            self._authenticated(handler, body)
            artifact_id = self._required_artifact_id(body, "artifact_id")
            operation_id = self._required_string(body, "operation_id", 64)
            service = self._require_database_service()
            if path == "/api/database/import/preview":
                job = service.begin_logical_import_preview_job(artifact_id, operation_id=operation_id)
            else:
                job = service.begin_logical_import_commit_job(artifact_id, operation_id=operation_id)
            self._queue_database_job(service, job)
            self._json(handler, 202, {"status": "ACCEPTED", "job": self._database_public_value(job)})
            return

        if path == "/api/database/restore/preview":
            self._only_fields(body, {"artifact_id", "operation_id", "trusted_origin_attested", "csrf"})
            self._authenticated(handler, body)
            artifact_id = self._required_artifact_id(body, "artifact_id")
            operation_id = self._required_string(body, "operation_id", 64)
            trusted_origin_attested = self._required_bool(body, "trusted_origin_attested")
            service = self._require_database_service()
            job = service.begin_restore_preview_job(
                artifact_id,
                operation_id=operation_id,
                trusted_origin_attested=trusted_origin_attested,
            )
            self._queue_database_job(service, job)
            self._json(handler, 202, {"status": "ACCEPTED", "job": self._database_public_value(job)})
            return

        if path in {"/api/database/restore/commit", "/api/database/restore/retry"}:
            self._only_fields(
                body,
                {
                    "artifact_id",
                    "operation_id",
                    "state_restoration_acknowledged",
                    "grant_secret",
                    "csrf",
                },
            )
            session_secret, _ = self._authenticated(handler, body)
            artifact_id = self._required_artifact_id(body, "artifact_id")
            operation_id = self._required_string(body, "operation_id", 64)
            state_restoration_acknowledged = self._required_bool(body, "state_restoration_acknowledged")
            grant_secret = self._required_string(body, "grant_secret", 1024)
            service = self._require_database_service()
            job = service.begin_restore_commit_job(
                artifact_id,
                operation_id=operation_id,
                state_restoration_acknowledged=state_restoration_acknowledged,
            )
            origin = self.origin

            def authorize(preview: str) -> None:
                self.auth_state.consume_sensitive_grant(
                    grant_secret=grant_secret,
                    session_secret=session_secret,
                    origin=origin,
                    preview=preview,
                )

            self._queue_restore_database_job(service, job, authorize=authorize)
            self._json(handler, 202, {"status": "ACCEPTED", "job": self._database_public_value(job)})
            return

        if path == "/api/database/jobs/status":
            self._only_fields(body, {"job_id", "csrf"})
            self._authenticated(handler, body, user_activity=False)
            job = self._database_public_value(
                self._require_database_service().job_status(self._required_string(body, "job_id", 64))
            )
            if not isinstance(job, dict):
                raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")
            self._json(handler, 200, {"status": "OK", "job": job})
            return

        raise _HTTPFailure(404, "NOT_FOUND")

    def _run(self, awaitable: Awaitable[Any], *, timeout_seconds: float | None = None) -> Any:
        if self._async_runner is None:
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[attr-defined]
            raise _HTTPFailure(503, "SERVICE_UNAVAILABLE")
        try:
            return self._async_runner.run(awaitable, timeout_seconds=timeout_seconds)
        except _ServiceRunnerTimeout as exc:
            raise _HTTPFailure(504, "SERVICE_TIMEOUT") from exc

    def _require_overview_service(self) -> OwnerOverviewLike:
        if self.overview_service is None:
            raise _HTTPFailure(503, "SERVICE_UNAVAILABLE")
        return self.overview_service

    def _require_control_service(self) -> OwnerControlLike:
        if self.control_service is None:
            raise _HTTPFailure(503, "SERVICE_UNAVAILABLE")
        return self.control_service

    def _require_operations_service(self) -> OwnerOperationsLike:
        if self.operations_service is None:
            raise _HTTPFailure(503, "SERVICE_UNAVAILABLE")
        return self.operations_service

    def _require_destructive_service(self) -> OwnerDestructiveLike:
        if self.destructive_service is None:
            raise _HTTPFailure(503, "SERVICE_UNAVAILABLE")
        return self.destructive_service

    def _require_database_service(self) -> OwnerDatabaseLike:
        if self.database_service is None:
            raise _HTTPFailure(503, "SERVICE_UNAVAILABLE")
        return self.database_service

    def _queue_database_job(self, service: OwnerDatabaseLike, job: dict[str, Any]) -> None:
        if not isinstance(job, dict):
            raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not job_id or len(job_id) > 64:
            raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")
        if self._async_runner is None:
            raise _HTTPFailure(503, "SERVICE_UNAVAILABLE")
        self._async_runner.submit(service.run_job(job_id))

    def _queue_restore_database_job(
        self,
        service: OwnerDatabaseLike,
        job: dict[str, Any],
        *,
        authorize: Callable[[str], Any | Awaitable[Any]],
    ) -> None:
        if not isinstance(job, dict):
            raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not job_id or len(job_id) > 64:
            raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")
        if self._async_runner is None:
            raise _HTTPFailure(503, "SERVICE_UNAVAILABLE")
        self._async_runner.submit(service.run_restore_job(job_id, authorize=authorize))

    @staticmethod
    def _database_public_value(value: Any) -> Any:
        if isinstance(value, list):
            return [OwnerHTTPServer._database_public_value(item) for item in value]
        if isinstance(value, dict):
            projected: dict[str, Any] = {}
            for key, item in value.items():
                normalized = key.casefold()
                if normalized == "path" or normalized == "relative_path" or normalized.endswith("_path"):
                    continue
                projected[key] = OwnerHTTPServer._database_public_value(item)
            return projected
        return value

    @staticmethod
    def _required_artifact_id(body: dict[str, Any], field: str) -> str:
        value = body.get(field)
        if not isinstance(value, str) or not _ARTIFACT_ID_RE.fullmatch(value):
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        return value

    @staticmethod
    def _required_export_lifecycle_states(body: dict[str, Any]) -> list[str]:
        value = body.get("lifecycle_states")
        if not isinstance(value, list) or not 1 <= len(value) <= len(_LOGICAL_EXPORT_LIFECYCLE_STATES):
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        if any(not isinstance(item, str) or item not in _LOGICAL_EXPORT_LIFECYCLE_STATES for item in value):
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        if len(set(value)) != len(value):
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        return list(value)

    def _dispatch_raw_database_upload(self, handler: _OwnerHTTPRequestHandler, path: str) -> None:
        self._authenticated(handler, {})
        if handler.headers.get_all("Transfer-Encoding", failobj=[]):
            raise _HTTPFailure(400, "TRANSFER_ENCODING_DENIED")
        lengths = handler.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1:
            raise _HTTPFailure(400, "INVALID_CONTENT_LENGTH")
        try:
            declared_size = int(lengths[0])
        except ValueError as exc:
            raise _HTTPFailure(400, "INVALID_CONTENT_LENGTH") from exc
        if declared_size <= 0:
            raise _HTTPFailure(400, "INVALID_CONTENT_LENGTH")

        if path == "/api/database/import/upload":
            expected_content_type = "application/json"
            maximum_size = LOGICAL_IMPORT_UPLOAD_MAX_BYTES
        elif path == "/api/database/restore/upload":
            expected_content_type = "application/octet-stream"
            maximum_size = RESTORE_UPLOAD_MAX_BYTES
        else:
            raise _HTTPFailure(404, "NOT_FOUND")
        if declared_size > maximum_size:
            raise _HTTPFailure(413, "BODY_TOO_LARGE")

        content_types = handler.headers.get_all("Content-Type", failobj=[])
        if len(content_types) != 1 or handler.headers.get_content_type() != expected_content_type:
            raise _HTTPFailure(415, "UNSUPPORTED_MEDIA_TYPE")

        checksum_values = handler.headers.get_all("X-FactLane-SHA256", failobj=[])
        if len(checksum_values) > 1:
            raise _HTTPFailure(400, "INVALID_CHECKSUM")
        expected_sha256: str | None = None
        if checksum_values:
            candidate = checksum_values[0]
            if not _HEX_DIGEST_RE.fullmatch(candidate):
                raise _HTTPFailure(400, "INVALID_CHECKSUM")
            expected_sha256 = candidate.lower()

        service = self._require_database_service()
        if path == "/api/database/import/upload":
            sink = service.begin_logical_import_upload(
                declared_size=declared_size,
                expected_sha256=expected_sha256,
            )
        else:
            sink = service.begin_restore_upload(
                declared_size=declared_size,
                expected_sha256=expected_sha256,
            )

        try:
            remaining = declared_size
            while remaining:
                handler.connection.settimeout(UPLOAD_READ_PROGRESS_TIMEOUT_SECONDS)
                try:
                    chunk = handler.rfile.read(min(UPLOAD_READ_CHUNK_BYTES, remaining))
                except (TimeoutError, socket.timeout) as exc:
                    raise _HTTPFailure(408, "UPLOAD_PROGRESS_TIMEOUT") from exc
                if not chunk:
                    raise _HTTPFailure(400, "INCOMPLETE_BODY")
                if len(chunk) > remaining:
                    raise _HTTPFailure(400, "UPLOAD_SIZE_MISMATCH")
                written = sink.write(chunk)
                if written != len(chunk):
                    raise _HTTPFailure(500, "UPLOAD_WRITE_FAILED")
                remaining -= len(chunk)
            sealed = sink.seal()
            artifact_id = sealed.get("artifact_id") if isinstance(sealed, dict) else None
            if not isinstance(artifact_id, str) or not _ARTIFACT_ID_RE.fullmatch(artifact_id):
                raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")
            upload_id = getattr(sink, "upload_id", None)
            if not isinstance(upload_id, str) or not upload_id or len(upload_id) > 128:
                raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")
            artifact = self._database_public_value(sealed)
            if not isinstance(artifact, dict):
                raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")
        except BaseException:
            sink.abort()
            raise

        self._json(
            handler,
            201,
            {
                "status": "CREATED",
                "upload_id": upload_id,
                "artifact_id": artifact_id,
                "artifact": artifact,
            },
        )

    def _stream_download(self, handler: _OwnerHTTPRequestHandler, handle: OwnerDownloadHandleLike) -> None:
        try:
            size = getattr(handle, "size_bytes", None)
            filename = getattr(handle, "filename", None)
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")
            if not isinstance(filename, str) or not _DOWNLOAD_FILENAME_RE.fullmatch(filename):
                raise _HTTPFailure(502, "DATABASE_RESPONSE_INVALID")

            handler.send_response(200)
            handler.send_header("Content-Type", "application/octet-stream")
            handler.send_header("Content-Length", str(size))
            handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            handler.send_header("Content-Security-Policy", _CSP)
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header("X-Frame-Options", "DENY")
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.send_header("Cross-Origin-Opener-Policy", "same-origin")
            handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Connection", "close")
            handler.end_headers()

            remaining = size
            try:
                while remaining:
                    chunk = handle.read(min(UPLOAD_READ_CHUNK_BYTES, remaining))
                    if not isinstance(chunk, bytes) or not chunk or len(chunk) > remaining:
                        handler.close_connection = True
                        return
                    handler.wfile.write(chunk)
                    remaining -= len(chunk)
            except Exception:
                handler.close_connection = True
                return
            handler.close_connection = True
        finally:
            handle.close()

    def _authenticated(
        self,
        handler: _OwnerHTTPRequestHandler,
        body: dict[str, Any],
        *,
        user_activity: bool = True,
    ) -> tuple[str, str]:
        session = self._required_cookie(handler, SESSION_COOKIE)
        header_values = handler.headers.get_all(CSRF_HEADER, failobj=[])
        if len(header_values) > 1:
            raise _HTTPFailure(403, "CSRF_INVALID")
        header_csrf = header_values[0] if header_values else None
        body_csrf = body.get("csrf")
        if body_csrf is not None and (not isinstance(body_csrf, str) or not body_csrf):
            raise _HTTPFailure(403, "CSRF_INVALID")
        if header_csrf is not None and body_csrf is not None and header_csrf != body_csrf:
            raise _HTTPFailure(403, "CSRF_INVALID")
        csrf = header_csrf or body_csrf
        if not isinstance(csrf, str) or not csrf:
            raise _HTTPFailure(403, "CSRF_INVALID")
        self.auth_state.validate_session(
            session_secret=session,
            origin=self.origin,
            csrf_secret=csrf,
            require_csrf=True,
            user_activity=user_activity,
        )
        return session, csrf

    def _transition_kwargs(self, body: dict[str, Any]) -> dict[str, Any]:
        scope = self._exact_scope(body)
        return {
            **self._scope_kwargs(scope),
            "memory_id": self._required_string(body, "memory_id", 512),
            "expected_record_id": self._required_string(body, "expected_record_id", 512),
            "expected_revision": self._required_int(body, "expected_revision", minimum=1),
            "observed_group_token": self._required_string(body, "observed_group_token", 4096),
            "idempotency_key": self._required_string(body, "idempotency_key", 512),
            "request_id": self._optional_string(body, "request_id", 512),
        }

    def _purge_targets(self, body: dict[str, Any]) -> tuple[PurgeTargetIdentity, ...]:
        items = self._required_object_list(body, "targets", maximum_items=50)
        if not items:
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        exact_fields = {*_SCOPE_FIELDS, "memory_id", "record_id", "revision"}
        targets: list[PurgeTargetIdentity] = []
        for item in items:
            if set(item) != exact_fields:
                raise _HTTPFailure(400, "INVALID_ENVELOPE")
            scope = self._exact_scope(item)
            scope_values = self._scope_kwargs(scope)
            targets.append(
                PurgeTargetIdentity(
                    scope=RecoveryScope(
                        scope=str(scope_values["scope"]),
                        project_id=scope_values["project_id"],
                        worktree_id=scope_values["worktree_id"],
                        workflow_id=scope_values["workflow_id"],
                        agent_id=scope_values["agent_id"],
                    ),
                    memory_id=self._required_string(item, "memory_id", 512),
                    record_id=self._required_string(item, "record_id", 512),
                    revision=self._required_int(item, "revision", minimum=0),
                )
            )
        return tuple(targets)

    @staticmethod
    def _purge_success_payload(result: PurgeSuccess) -> dict[str, Any]:
        return {
            "status": result.status,
            "operation_id": result.operation_id,
            "preview_digest": result.preview_digest,
            "target_count": result.target_count,
            "ready_instance_ids": list(result.ready_instance_ids),
            "resume_failures": dict(result.resume_failures),
        }

    @staticmethod
    def _scope_kwargs(scope: ScopeContext) -> dict[str, str | None]:
        return scope.to_dict()

    @staticmethod
    def _exact_scope(body: dict[str, Any]) -> ScopeContext:
        values: dict[str, str | None] = {}
        for field in _SCOPE_FIELDS:
            value = body.get(field)
            if value is not None and not isinstance(value, str):
                raise AdapterError("INVALID_ENVELOPE", f"{field} must be a string or null")
            values[field] = value
        scope = values["scope"]
        if scope is None:
            raise AdapterError("INVALID_ENVELOPE", "scope is required for backend routes")
        return validate_scope(
            scope,
            values["project_id"],
            values["worktree_id"],
            values["workflow_id"],
            values["agent_id"],
        )

    @staticmethod
    def _only_fields(body: dict[str, Any], allowed: set[str]) -> None:
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise _HTTPFailure(400, "INVALID_ENVELOPE", {"message": "unsupported fields: " + ", ".join(unknown)})

    @staticmethod
    def _required_int(body: dict[str, Any], field: str, *, minimum: int, maximum: int = 2**63 - 1) -> int:
        value = body.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        return value

    @staticmethod
    def _required_bool(body: dict[str, Any], field: str) -> bool:
        value = body.get(field)
        if not isinstance(value, bool):
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        return value

    @staticmethod
    def _optional_int(
        body: dict[str, Any],
        field: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        value = body.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        return value

    @staticmethod
    def _required_object(body: dict[str, Any], field: str) -> dict[str, Any]:
        value = body.get(field)
        if not isinstance(value, dict):
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        return value

    @staticmethod
    def _optional_object(body: dict[str, Any], field: str) -> dict[str, Any] | None:
        value = body.get(field)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        return value

    @staticmethod
    def _required_object_list(body: dict[str, Any], field: str, *, maximum_items: int) -> list[dict[str, Any]]:
        value = body.get(field)
        if not isinstance(value, list) or len(value) > maximum_items:
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        if any(not isinstance(item, dict) for item in value):
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        return value

    @staticmethod
    def _optional_string_list(
        body: dict[str, Any],
        field: str,
        *,
        maximum_items: int,
        maximum_string: int,
    ) -> list[str] | None:
        value = body.get(field)
        if value is None:
            return None
        if not isinstance(value, list) or len(value) > maximum_items:
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        if any(not isinstance(item, str) or not item or len(item) > maximum_string for item in value):
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        return value

    @staticmethod
    def _required_string(body: dict[str, Any], field: str, maximum: int) -> str:
        value = body.get(field)
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise _HTTPFailure(400, "INVALID_ENVELOPE", {"message": f"{field} is required and bounded"})
        return value

    @staticmethod
    def _required_hex_digest(body: dict[str, Any], field: str) -> str:
        value = body.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in value)
        ):
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        return value.lower()

    @staticmethod
    def _optional_string(body: dict[str, Any], field: str, maximum: int) -> str | None:
        value = body.get(field)
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        return value

    @staticmethod
    def _request_path(handler: _OwnerHTTPRequestHandler) -> str:
        parsed = urlsplit(handler.path)
        if parsed.query or parsed.fragment:
            raise _HTTPFailure(400, "URL_PARAMETERS_DENIED")
        return parsed.path

    def _require_exact_host(self, handler: _OwnerHTTPRequestHandler) -> None:
        values = handler.headers.get_all("Host", failobj=[])
        if len(values) != 1 or values[0] != self.host:
            raise _HTTPFailure(403, "HOST_DENIED")

    def _require_exact_origin(self, handler: _OwnerHTTPRequestHandler) -> None:
        values = handler.headers.get_all("Origin", failobj=[])
        if len(values) != 1 or values[0] != self.origin:
            raise _HTTPFailure(403, "ORIGIN_DENIED")

    @staticmethod
    def _read_json_body(handler: _OwnerHTTPRequestHandler) -> dict[str, Any]:
        if handler.headers.get_all("Transfer-Encoding", failobj=[]):
            raise _HTTPFailure(400, "TRANSFER_ENCODING_DENIED")
        lengths = handler.headers.get_all("Content-Length", failobj=[])
        if len(lengths) > 1:
            raise _HTTPFailure(400, "INVALID_CONTENT_LENGTH")
        try:
            length = int(lengths[0]) if lengths else 0
        except ValueError as exc:
            raise _HTTPFailure(400, "INVALID_CONTENT_LENGTH") from exc
        if length < 0:
            raise _HTTPFailure(400, "INVALID_CONTENT_LENGTH")
        if length > MAX_BODY_BYTES:
            raise _HTTPFailure(413, "BODY_TOO_LARGE")
        content_type = handler.headers.get_content_type()
        if length and content_type != "application/json":
            raise _HTTPFailure(415, "JSON_REQUIRED")
        raw = handler.rfile.read(length) if length else b"{}"
        if len(raw) != (length if length else 2):
            raise _HTTPFailure(400, "INCOMPLETE_BODY")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _HTTPFailure(400, "INVALID_JSON") from exc
        if not isinstance(parsed, dict):
            raise _HTTPFailure(400, "INVALID_ENVELOPE")
        return parsed

    @staticmethod
    def _cookie(name: str, value: str) -> str:
        cookie = SimpleCookie()
        cookie[name] = value
        morsel = cookie[name]
        morsel["path"] = "/"
        morsel["secure"] = True
        morsel["httponly"] = True
        morsel["samesite"] = "Strict"
        return morsel.OutputString()

    @staticmethod
    def _clear_cookie(name: str) -> str:
        cookie = SimpleCookie()
        cookie[name] = ""
        morsel = cookie[name]
        morsel["path"] = "/"
        morsel["secure"] = True
        morsel["httponly"] = True
        morsel["samesite"] = "Strict"
        morsel["max-age"] = 0
        return morsel.OutputString()

    @staticmethod
    def _required_cookie(handler: _OwnerHTTPRequestHandler, name: str) -> str:
        values = handler.headers.get_all("Cookie", failobj=[])
        found: list[str] = []
        for raw in values:
            parsed = SimpleCookie()
            try:
                parsed.load(raw)
            except CookieError as exc:
                raise _HTTPFailure(401, "AUTH_COOKIE_INVALID") from exc
            if name in parsed:
                found.append(parsed[name].value)
        if len(found) != 1 or not found[0]:
            raise _HTTPFailure(401, "AUTH_COOKIE_INVALID")
        return found[0]

    def _auth_error(self, handler: _OwnerHTTPRequestHandler, exc: AuthError) -> None:
        if exc.code in {"SESSION_INVALID", "SESSION_EXPIRED", "PREAUTH_INVALID", "AUTH_EPOCH_MISMATCH"}:
            status = 401
        elif exc.code in {"SOURCE_RATE_LIMIT", "PENDING_LIMIT", "SESSION_LIMIT"}:
            status = 429
        elif exc.code.endswith("CONFLICT"):
            status = 409
        else:
            status = 403
        self._json(
            handler,
            status,
            {"status": "ERROR", "code": exc.code, "retryable": exc.retryable},
        )

    def _adapter_error(self, handler: _OwnerHTTPRequestHandler, exc: AdapterError) -> None:
        if exc.code == "CROSS_SCOPE_DENIED":
            status = 403
        elif exc.code == "NOT_FOUND":
            status = 404
        elif exc.code == "BACKEND_UNAVAILABLE":
            status = 503
        elif exc.code in {"VERSION_CONFLICT", "GROUP_VERSION_CONFLICT", "IDEMPOTENCY_CONFLICT"}:
            status = 409
        else:
            status = 400
        self._json(handler, status, {"status": "ERROR", "code": exc.code})

    def _destructive_error(self, handler: _OwnerHTTPRequestHandler, exc: OwnerDestructiveError) -> None:
        if exc.recovery_required:
            status = 409
        elif exc.code in {"PURGE_GRANT_REQUIRED"}:
            status = 403
        elif exc.code in {
            "PURGE_CONFIRMATION_MISMATCH",
            "PURGE_PREVIEW_STALE",
            "PURGE_RECOVERY_STATE_MISMATCH",
            "PURGE_RESULT_INVALID",
            "PURGE_COORDINATION_FAILED",
            "PURGE_ACTIVATION_FAILED",
        }:
            status = 409
        else:
            status = 400
        payload: dict[str, Any] = {"status": "ERROR", "code": exc.code}
        if exc.recovery_required:
            payload["recovery_required"] = True
        self._json(handler, status, payload)

    def _json(
        self,
        handler: _OwnerHTTPRequestHandler,
        status: int,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
        extra_header_items: list[tuple[str, str]] | None = None,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._bytes(
            handler,
            status,
            raw,
            content_type="application/json; charset=utf-8",
            extra_headers=extra_headers,
            extra_header_items=extra_header_items,
        )

    def _bytes(
        self,
        handler: _OwnerHTTPRequestHandler,
        status: int,
        payload: bytes,
        *,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
        extra_header_items: list[tuple[str, str]] | None = None,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Content-Security-Policy", _CSP)
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Cross-Origin-Opener-Policy", "same-origin")
        handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        if extra_headers:
            for name, value in extra_headers.items():
                handler.send_header(name, value)
        if extra_header_items:
            for name, value in extra_header_items:
                handler.send_header(name, value)
        handler.end_headers()
        if payload:
            handler.wfile.write(payload)
        handler.close_connection = True


class _HTTPFailure(Exception):
    def __init__(self, status: int, code: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.payload = payload or {}
