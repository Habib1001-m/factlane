from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from .adapter import MemoryAdapter, PROFILE_DEFINITIONS, trusted_write_context_for_profile
from .contract import AdapterError, SCOPES, ScopeContext, canonical_json, validate_scope
from .owner_control import OwnerControlService


MAX_REQUEST_BODY_BYTES = 64 * 1024
MAX_REQUEST_TARGET_BYTES = 4096
BOOTSTRAP_TTL_SECONDS = 60
SESSION_TTL_SECONDS = 30 * 60
MAX_SESSION_GROUP_OBSERVATIONS = 1024

_SESSION_COOKIE = "factlane_owner_session"
_BOOTSTRAP_HEADER = "X-FactLane-Bootstrap"
_CSRF_HEADER = "X-FactLane-CSRF"
_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_HTML_CONTENT_TYPE = "text/html; charset=utf-8"
_CSP = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "connect-src 'self'; "
    "script-src 'none'; "
    "style-src 'none'; "
    "object-src 'none'"
)

_FORBIDDEN_REQUEST_FIELDS = frozenset(
    {
        "scope",
        "project_id",
        "worktree_id",
        "workflow_id",
        "agent_id",
        "db",
        "profile",
        "host_id",
        "write_profile",
        "grant_ref",
        "authorization_class",
    }
)

_CONFLICT_ERRORS = frozenset(
    {
        "VERSION_CONFLICT",
        "GROUP_VERSION_CONFLICT",
        "IDEMPOTENCY_CONFLICT",
        "LINEAGE_CHANGED",
        "GROUP_LIMIT_EXCEEDED",
        "GROUP_OBSERVATION_TOO_LARGE",
    }
)
_AUTHORIZATION_ERRORS = frozenset(
    {
        "WRITE_AUTHORIZATION_DENIED",
        "CROSS_SCOPE_DENIED",
        "INVALID_WRITE_CONTEXT",
        "SCOPE_AUTHORITY_DENIED",
    }
)
_UNAVAILABLE_ERRORS = frozenset(
    {
        "OWNER_CONTROL_UNAVAILABLE",
        "BACKEND_BUSY",
        "BACKEND_UNAVAILABLE",
        "MAINTENANCE_IN_PROGRESS",
        "MIGRATION_REQUIRED",
        "PROFILE_MISMATCH",
        "STATUS_UNAVAILABLE",
        "STORAGE_UNAVAILABLE",
        "STORAGE_VERSION_UNSUPPORTED",
    }
)


class _OwnerService(Protocol):
    async def inventory(self, **kwargs: Any) -> dict[str, Any]: ...

    async def detail(self, **kwargs: Any) -> dict[str, Any]: ...

    async def conflicts(self, **kwargs: Any) -> dict[str, Any]: ...

    async def health(self, **kwargs: Any) -> dict[str, Any]: ...

    async def retention_status(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass
class _Session:
    csrf_token: str
    expires_at: float
    observed_group_tokens: OrderedDict[str, None] = field(default_factory=OrderedDict)

    def observe_group_token(self, token: str) -> None:
        self.observed_group_tokens[token] = None
        self.observed_group_tokens.move_to_end(token)
        while len(self.observed_group_tokens) > MAX_SESSION_GROUP_OBSERVATIONS:
            self.observed_group_tokens.popitem(last=False)

    def has_observed_group_token(self, token: str) -> bool:
        return token in self.observed_group_tokens


class _OwnerWebUIState:
    def __init__(
        self,
        service: _OwnerService,
        scope: ScopeContext,
        *,
        bootstrap_ttl_seconds: int,
        session_ttl_seconds: int,
    ) -> None:
        self.service = service
        self.scope = scope
        self.scope_kwargs = scope.to_dict()
        self.session_ttl_seconds = session_ttl_seconds
        self.bootstrap_token: str | None = secrets.token_urlsafe(32)
        self.bootstrap_expires_at = time.monotonic() + bootstrap_ttl_seconds
        self.sessions: dict[str, _Session] = {}
        self.lock = threading.RLock()
        self.origin = ""

    def set_origin(self, port: int) -> None:
        self.origin = f"http://127.0.0.1:{port}"

    def current_bootstrap_token(self) -> str | None:
        with self.lock:
            if self.bootstrap_token is not None and time.monotonic() > self.bootstrap_expires_at:
                self.bootstrap_token = None
            return self.bootstrap_token

    def exchange_bootstrap(self, supplied: str | None) -> tuple[str, _Session] | None:
        with self.lock:
            expected = self.current_bootstrap_token()
            if expected is None or supplied is None or not secrets.compare_digest(expected, supplied):
                return None
            self.bootstrap_token = None
            session_id = secrets.token_urlsafe(32)
            session = _Session(
                csrf_token=secrets.token_urlsafe(32),
                expires_at=time.monotonic() + self.session_ttl_seconds,
            )
            self.sessions[session_id] = session
            return session_id, session

    def get_session(self, session_id: str | None) -> _Session | None:
        if not session_id:
            return None
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return None
            if time.monotonic() > session.expires_at:
                self.sessions.pop(session_id, None)
                return None
            return session

    def observe_result(self, session: _Session, result: Any) -> None:
        tokens: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                token = value.get("observed_group_token")
                if isinstance(token, str) and _is_group_token(token):
                    tokens.append(token)
                for nested in value.values():
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(result)
        if not tokens:
            return
        with self.lock:
            for token in tokens:
                session.observe_group_token(token)


class OwnerWebUIServer(HTTPServer):
    """Single-process loopback HTTP server for one trusted Owner control session."""

    def __init__(self, address: tuple[str, int], state: _OwnerWebUIState) -> None:
        super().__init__(address, OwnerWebUIRequestHandler)
        if self.server_address[0] != "127.0.0.1":
            self.server_close()
            raise ValueError("Owner WebUI must bind exactly 127.0.0.1")
        state.set_origin(int(self.server_address[1]))
        self.owner_state = state

    @property
    def bootstrap_token(self) -> str | None:
        return self.owner_state.current_bootstrap_token()


@dataclass(frozen=True)
class _MutationRoute:
    method_name: str
    route_field: str
    route_value: str
    inspection_memory_id: str
    required_fields: frozenset[str]
    optional_fields: frozenset[str] = frozenset({"request_id"})


def _is_group_token(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)


def _transport_error(code: str, message: str) -> AdapterError:
    return AdapterError(code, message)


def _adapter_status(code: str) -> int:
    if code in _AUTHORIZATION_ERRORS:
        return 403
    if code == "NOT_FOUND":
        return 404
    if code in _CONFLICT_ERRORS:
        return 409
    if code in _UNAVAILABLE_ERRORS:
        return 503
    return 400


def _single_query_values(query: str, allowed: frozenset[str]) -> dict[str, str]:
    try:
        parsed = parse_qs(query, keep_blank_values=True, max_num_fields=12)
    except ValueError as exc:
        raise _transport_error("INVALID_ENVELOPE", "query parameters are invalid or unbounded") from exc
    unexpected = sorted(set(parsed) - allowed)
    if unexpected:
        raise _transport_error("INVALID_ENVELOPE", "query contains unsupported fields")
    result: dict[str, str] = {}
    for key, values in parsed.items():
        if len(values) != 1:
            raise _transport_error("INVALID_ENVELOPE", "query fields must occur at most once")
        result[key] = values[0]
    return result


def _page_template(title: str, payload: Any) -> str:
    safe_title = html.escape(title, quote=True)
    rendered = html.escape(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), quote=True)
    links = (
        ("Overview", "/owner/"),
        ("Current", "/owner/current"),
        ("Candidates", "/owner/candidates"),
        ("Conflicts", "/owner/conflicts"),
        ("Needs Review", "/owner/needs-review"),
        ("History", "/owner/history"),
        ("System Health", "/owner/system-health"),
    )
    nav = " ".join(
        f'<a href="{html.escape(href, quote=True)}">{html.escape(label, quote=True)}</a>' for label, href in links
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>FactLane Owner · {safe_title}</title>"
        "</head><body>"
        f"<nav>{nav}</nav><main><h1>{safe_title}</h1><pre>{rendered}</pre></main></body></html>"
    )


class OwnerWebUIRequestHandler(BaseHTTPRequestHandler):
    server_version = "FactLaneOwnerUI"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> _OwnerWebUIState:
        return self.server.owner_state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str, *, set_cookie: str | None = None) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Connection", "close")
        if set_cookie is not None:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, value: dict[str, Any], *, set_cookie: str | None = None) -> None:
        body = canonical_json(value).encode("utf-8")
        self._send(status, body, _JSON_CONTENT_TYPE, set_cookie=set_cookie)

    def _send_html(self, status: int, value: str) -> None:
        self._send(status, value.encode("utf-8"), _HTML_CONTENT_TYPE)

    def _send_adapter_error(self, error: AdapterError) -> None:
        self._send_json(_adapter_status(error.code), error.envelope(str(uuid.uuid4())))

    def _send_simple_error(self, status: int, code: str, message: str) -> None:
        error = AdapterError(code, message)
        self._send_json(status, error.envelope(str(uuid.uuid4())))

    def _target(self) -> tuple[str, str]:
        if len(self.path.encode("utf-8", errors="surrogatepass")) > MAX_REQUEST_TARGET_BYTES:
            raise _transport_error("INVALID_ENVELOPE", "request target is too large")
        parsed = urlsplit(self.path)
        return parsed.path, parsed.query

    def _host_is_valid(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        return self.headers.get("Host") == expected

    def _same_origin_headers_valid(self, *, require_context: bool) -> bool:
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")
        if origin is not None and origin != self.state.origin:
            return False
        if referer is not None:
            parsed = urlsplit(referer)
            referer_origin = f"{parsed.scheme}://{parsed.netloc}"
            if referer_origin != self.state.origin:
                return False
        if require_context and origin is None and referer is None:
            return False
        return True

    def _session_id(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(raw)
        except Exception:
            return None
        value = cookies.get(_SESSION_COOKIE)
        return value.value if value is not None else None

    def _authenticated_session(self) -> _Session | None:
        return self.state.get_session(self._session_id())

    def _require_common_request_security(self) -> bool:
        if not self._host_is_valid():
            self._send_simple_error(403, "HOST_DENIED", "Owner WebUI Host header is not the bound loopback origin")
            return False
        if not self._same_origin_headers_valid(require_context=False):
            self._send_simple_error(403, "ORIGIN_DENIED", "Owner WebUI origin or referer is not allowed")
            return False
        return True

    def _call_service(self, method_name: str, **kwargs: Any) -> dict[str, Any]:
        method = getattr(self.state.service, method_name, None)
        if method is None or not callable(method):
            raise AdapterError(
                "OWNER_CONTROL_UNAVAILABLE",
                f"OwnerControlService does not expose the governed {method_name} operation",
            )
        result = asyncio.run(method(**kwargs))
        if not isinstance(result, dict):
            raise AdapterError("OWNER_CONTROL_UNAVAILABLE", "OwnerControlService returned an invalid structural result")
        return result

    def _scope_kwargs(self) -> dict[str, str | None]:
        return dict(self.state.scope_kwargs)

    def _read_api(self, path: str, query: str) -> dict[str, Any] | None:
        scope = self._scope_kwargs()
        if path == "/owner/api/v1/inventory":
            values = _single_query_values(
                query,
                frozenset({"lifecycle_state", "contradiction_state", "memory_type", "limit", "cursor"}),
            )
            limit_value = values.pop("limit", None)
            kwargs: dict[str, Any] = dict(values)
            if limit_value is not None:
                try:
                    kwargs["limit"] = int(limit_value)
                except ValueError as exc:
                    raise _transport_error("INVALID_ENVELOPE", "limit must be an integer") from exc
            return self._call_service("inventory", **kwargs, **scope)
        if path == "/owner/api/v1/conflicts":
            values = _single_query_values(query, frozenset({"limit", "cursor"}))
            limit_value = values.pop("limit", None)
            kwargs = dict(values)
            if limit_value is not None:
                try:
                    kwargs["limit"] = int(limit_value)
                except ValueError as exc:
                    raise _transport_error("INVALID_ENVELOPE", "limit must be an integer") from exc
            return self._call_service("conflicts", **kwargs, **scope)
        if path == "/owner/api/v1/health":
            if query:
                raise _transport_error("INVALID_ENVELOPE", "health does not accept query parameters")
            return self._call_service("health", **scope)
        if path == "/owner/api/v1/status/retention":
            if query:
                raise _transport_error("INVALID_ENVELOPE", "retention status does not accept query parameters")
            return self._call_service("retention_status", **scope)
        match = re.fullmatch(r"/owner/api/v1/memories/([^/]+)", path)
        if match is not None:
            values = _single_query_values(query, frozenset({"limit", "cursor"}))
            limit_value = values.pop("limit", None)
            kwargs = dict(values)
            if limit_value is not None:
                try:
                    kwargs["limit"] = int(limit_value)
                except ValueError as exc:
                    raise _transport_error("INVALID_ENVELOPE", "limit must be an integer") from exc
            memory_id = unquote(match.group(1), errors="strict")
            return self._call_service("detail", memory_id=memory_id, **kwargs, **scope)
        return None

    def _page_data(self, path: str) -> tuple[str, Any] | None:
        scope = self._scope_kwargs()
        if path in {"/owner", "/owner/"}:
            return "Overview", self._call_service("inventory", limit=20, **scope)
        if path == "/owner/current":
            return "Current", self._call_service("inventory", lifecycle_state="VALIDATED_CURRENT", limit=20, **scope)
        if path == "/owner/candidates":
            return "Candidates", self._call_service("inventory", lifecycle_state="CANDIDATE", limit=20, **scope)
        if path == "/owner/conflicts":
            return "Conflicts", self._call_service("conflicts", limit=20, **scope)
        if path == "/owner/needs-review":
            return (
                "Needs Review",
                {
                    "unresolved": self._call_service(
                        "inventory", contradiction_state="UNRESOLVED", limit=20, **scope
                    ),
                    "stale": self._call_service("inventory", lifecycle_state="STALE", limit=20, **scope),
                    "conflicts": self._call_service("conflicts", limit=20, **scope),
                },
            )
        if path == "/owner/history":
            return (
                "History",
                {
                    "historical": self._call_service("inventory", lifecycle_state="HISTORICAL", limit=20, **scope),
                    "superseded": self._call_service("inventory", lifecycle_state="SUPERSEDED", limit=20, **scope),
                    "guidance": "Open an authenticated memory detail to inspect bounded per-memory revision and lifecycle-event history.",
                },
            )
        if path == "/owner/system-health":
            return (
                "System Health",
                {
                    "health": self._call_service("health", **scope),
                    "retention": self._call_service("retention_status", **scope),
                },
            )
        return None

    def do_GET(self) -> None:
        try:
            if not self._require_common_request_security():
                return
            session = self._authenticated_session()
            if session is None:
                self._send_simple_error(401, "UNAUTHENTICATED", "Owner WebUI authentication is required")
                return
            path, query = self._target()
            result = self._read_api(path, query)
            if result is not None:
                self.state.observe_result(session, result)
                self._send_json(200, result)
                return
            if query:
                raise _transport_error("INVALID_ENVELOPE", "Owner HTML pages do not accept query parameters")
            page = self._page_data(path)
            if page is None:
                self._send_simple_error(404, "NOT_FOUND", "Owner WebUI route was not found")
                return
            title, payload = page
            self.state.observe_result(session, payload)
            self._send_html(200, _page_template(title, payload))
        except AdapterError as exc:
            self._send_adapter_error(exc)
        except (UnicodeDecodeError, ValueError):
            self._send_simple_error(400, "INVALID_ENVELOPE", "Owner WebUI request is invalid")
        except Exception:
            self._send_simple_error(500, "OWNER_WEBUI_INTERNAL", "Owner WebUI request failed safely")

    def _read_json_body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise _transport_error("INVALID_ENVELOPE", "chunked request bodies are not accepted")
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise _transport_error("INVALID_ENVELOPE", "mutation body must use application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise _transport_error("INVALID_ENVELOPE", "mutation body requires Content-Length")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise _transport_error("INVALID_ENVELOPE", "Content-Length is invalid") from exc
        if length < 2 or length > MAX_REQUEST_BODY_BYTES:
            raise _transport_error("INVALID_ENVELOPE", "mutation body is empty or exceeds the 64 KiB cap")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise _transport_error("INVALID_ENVELOPE", "mutation body was truncated")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _transport_error("INVALID_ENVELOPE", "mutation body must contain one UTF-8 JSON object") from exc
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise _transport_error("INVALID_ENVELOPE", "mutation body must contain one JSON object with string keys")
        return value

    def _match_mutation(self, path: str) -> _MutationRoute | None:
        memory_patterns = (
            (
                r"/owner/api/v1/memories/([^/]+)/revoke",
                "revoke",
                frozenset({"expected_record_id", "expected_revision", "observed_group_token", "idempotency_key", "reason_code"}),
            ),
            (
                r"/owner/api/v1/memories/([^/]+)/reverify",
                "reverify",
                frozenset({"expected_record_id", "expected_revision", "observed_group_token", "idempotency_key", "verification"}),
            ),
            (
                r"/owner/api/v1/memories/([^/]+)/replace",
                "replace",
                frozenset({"expected_record_id", "expected_revision", "observed_group_token", "idempotency_key", "replacement"}),
            ),
            (
                r"/owner/api/v1/memories/([^/]+)/recheck",
                "manual_recheck",
                frozenset({"expected_record_id", "expected_revision", "observed_group_token", "idempotency_key", "evidence"}),
            ),
        )
        for pattern, method_name, required in memory_patterns:
            match = re.fullmatch(pattern, path)
            if match is not None:
                memory_id = unquote(match.group(1), errors="strict")
                return _MutationRoute(method_name, "memory_id", memory_id, memory_id, required)

        match = re.fullmatch(r"/owner/api/v1/candidates/([^/]+)/reject", path)
        if match is not None:
            memory_id = unquote(match.group(1), errors="strict")
            return _MutationRoute(
                "reject",
                "memory_id",
                memory_id,
                memory_id,
                frozenset({"expected_record_id", "expected_revision", "observed_group_token", "idempotency_key", "reason_code"}),
            )

        match = re.fullmatch(r"/owner/api/v1/revoked/([^/]+)/readmit", path)
        if match is not None:
            memory_id = unquote(match.group(1), errors="strict")
            return _MutationRoute(
                "readmit",
                "memory_id",
                memory_id,
                memory_id,
                frozenset({"revoked_record_id", "expected_revoked_revision", "observed_group_token", "idempotency_key", "verification"}),
            )

        match = re.fullmatch(r"/owner/api/v1/conflicts/([^/]+)/resolve", path)
        if match is not None:
            contradiction_key = unquote(match.group(1), errors="strict")
            return _MutationRoute(
                "resolve",
                "contradiction_key",
                contradiction_key,
                "",
                frozenset(
                    {
                        "observed_group_token",
                        "winner_memory_id",
                        "winner_record_id",
                        "winner_expected_revision",
                        "idempotency_key",
                        "reason_code",
                    }
                ),
                frozenset({"verification", "request_id"}),
            )
        return None

    def _validate_mutation_body(self, route: _MutationRoute, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) & _FORBIDDEN_REQUEST_FIELDS:
            raise _transport_error("INVALID_ENVELOPE", "mutation body may not override trusted launcher scope or authority")
        missing = sorted(route.required_fields - set(body))
        unexpected = sorted(set(body) - route.required_fields - route.optional_fields)
        if missing:
            raise _transport_error("INVALID_ENVELOPE", "mutation body is missing required structural fields")
        if unexpected:
            raise _transport_error("INVALID_ENVELOPE", "mutation body contains unsupported structural fields")
        token = body.get("observed_group_token")
        if not isinstance(token, str) or not _is_group_token(token):
            raise _transport_error("INVALID_ENVELOPE", "observed_group_token must be the exact displayed 64-hex token")
        payload = dict(body)
        payload[route.route_field] = route.route_value
        return payload

    def _require_displayed_group(self, session: _Session, payload: dict[str, Any]) -> None:
        submitted = payload["observed_group_token"]
        if not session.has_observed_group_token(submitted):
            raise _transport_error(
                "INVALID_ENVELOPE",
                "observed_group_token was not displayed by an authenticated Owner inspection in this session",
            )

    def _handle_bootstrap(self, query: str) -> None:
        if query:
            self._send_simple_error(400, "INVALID_ENVELOPE", "bootstrap credentials are never accepted in the URL")
            return
        if not self._same_origin_headers_valid(require_context=False):
            self._send_simple_error(403, "ORIGIN_DENIED", "Owner WebUI bootstrap origin or referer is not allowed")
            return
        exchanged = self.state.exchange_bootstrap(self.headers.get(_BOOTSTRAP_HEADER))
        if exchanged is None:
            self._send_simple_error(401, "UNAUTHENTICATED", "Owner WebUI bootstrap credential is invalid or expired")
            return
        session_id, session = exchanged
        cookie = f"{_SESSION_COOKIE}={session_id}; Path=/owner; HttpOnly; SameSite=Strict; Max-Age={self.state.session_ttl_seconds}"
        self._send_json(
            200,
            {"status": "OK", "csrf_token": session.csrf_token, "owner_path": "/owner/"},
            set_cookie=cookie,
        )

    def do_POST(self) -> None:
        try:
            if not self._host_is_valid():
                self._send_simple_error(403, "HOST_DENIED", "Owner WebUI Host header is not the bound loopback origin")
                return
            path, query = self._target()
            if path == "/owner/bootstrap":
                self._handle_bootstrap(query)
                return
            if query:
                raise _transport_error("INVALID_ENVELOPE", "Owner mutation routes do not accept query parameters")
            if not self._same_origin_headers_valid(require_context=True):
                self._send_simple_error(403, "ORIGIN_DENIED", "Owner mutation requires the bound same-origin context")
                return
            session = self._authenticated_session()
            if session is None:
                self._send_simple_error(401, "UNAUTHENTICATED", "Owner WebUI authentication is required")
                return
            csrf = self.headers.get(_CSRF_HEADER)
            if csrf is None or not secrets.compare_digest(session.csrf_token, csrf):
                self._send_simple_error(403, "CSRF_DENIED", "Owner mutation CSRF token is invalid")
                return
            route = self._match_mutation(path)
            if route is None:
                self._send_simple_error(404, "NOT_FOUND", "Owner WebUI mutation route was not found")
                return
            method = getattr(self.state.service, route.method_name, None)
            if method is None or not callable(method):
                raise AdapterError(
                    "OWNER_CONTROL_UNAVAILABLE",
                    f"OwnerControlService does not expose the governed {route.method_name} operation",
                )
            body = self._read_json_body()
            payload = self._validate_mutation_body(route, body)
            self._require_displayed_group(session, payload)
            result = self._call_service(route.method_name, **payload, **self._scope_kwargs())
            self.state.observe_result(session, result)
            self._send_json(200, result)
        except AdapterError as exc:
            self._send_adapter_error(exc)
        except (UnicodeDecodeError, ValueError):
            self._send_simple_error(400, "INVALID_ENVELOPE", "Owner WebUI request is invalid")
        except Exception:
            self._send_simple_error(500, "OWNER_WEBUI_INTERNAL", "Owner WebUI request failed safely")

    def _unsupported_method(self) -> None:
        try:
            if not self._require_common_request_security():
                return
            if self._authenticated_session() is None:
                self._send_simple_error(401, "UNAUTHENTICATED", "Owner WebUI authentication is required")
                return
            self._send_simple_error(405, "METHOD_NOT_ALLOWED", "Owner WebUI does not expose this HTTP method")
        except Exception:
            self._send_simple_error(500, "OWNER_WEBUI_INTERNAL", "Owner WebUI request failed safely")

    do_DELETE = _unsupported_method
    do_HEAD = _unsupported_method
    do_OPTIONS = _unsupported_method
    do_PATCH = _unsupported_method
    do_PUT = _unsupported_method
    do_TRACE = _unsupported_method


def create_owner_server(
    service: _OwnerService,
    *,
    scope: str,
    project_id: str | None = None,
    worktree_id: str | None = None,
    workflow_id: str | None = None,
    agent_id: str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    bootstrap_ttl_seconds: int = BOOTSTRAP_TTL_SECONDS,
    session_ttl_seconds: int = SESSION_TTL_SECONDS,
) -> OwnerWebUIServer:
    """Create an authenticated Owner WebUI server bound to one immutable launcher scope."""

    if host != "127.0.0.1":
        raise ValueError("Owner WebUI host must be exactly 127.0.0.1")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Owner WebUI port must be an integer from 0 through 65535")
    if (
        isinstance(bootstrap_ttl_seconds, bool)
        or not isinstance(bootstrap_ttl_seconds, int)
        or not 1 <= bootstrap_ttl_seconds <= 300
    ):
        raise ValueError("bootstrap_ttl_seconds must be an integer from 1 through 300")
    if (
        isinstance(session_ttl_seconds, bool)
        or not isinstance(session_ttl_seconds, int)
        or not 60 <= session_ttl_seconds <= 86_400
    ):
        raise ValueError("session_ttl_seconds must be an integer from 60 through 86400")
    scope_context = validate_scope(scope, project_id, worktree_id, workflow_id, agent_id)
    state = _OwnerWebUIState(
        service,
        scope_context,
        bootstrap_ttl_seconds=bootstrap_ttl_seconds,
        session_ttl_seconds=session_ttl_seconds,
    )
    return OwnerWebUIServer((host, port), state)


def _build_launcher_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FactLane local Owner WebUI launcher")
    parser.add_argument("--db", required=True, help="FactLane SQLite database path")
    parser.add_argument("--profile", choices=sorted(PROFILE_DEFINITIONS), default="nomic-768")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--tokenizer-path", default=None)
    parser.add_argument("--host-id", required=True, help="stable non-secret trusted launcher identity")
    parser.add_argument("--scope", required=True, choices=sorted(SCOPES))
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--worktree-id", default=None)
    parser.add_argument("--workflow-id", default=None)
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--port", type=int, default=0, help="loopback port; 0 selects an ephemeral port")
    return parser


async def _create_launcher_service(args: argparse.Namespace) -> tuple[MemoryAdapter, OwnerControlService]:
    adapter = await MemoryAdapter.create(
        args.db,
        args.profile,
        ollama_url=args.ollama_url,
        tokenizer_path=args.tokenizer_path,
        runtime_agent_id=args.host_id,
        trusted_write_context=trusted_write_context_for_profile(
            "owner-current",
            contributor_ref=args.host_id,
            grant_ref=f"launcher:owner-current:{args.host_id}",
        ),
    )
    return adapter, OwnerControlService(adapter)


def main(argv: list[str] | None = None) -> int:
    args = _build_launcher_parser().parse_args(argv)
    adapter: MemoryAdapter | None = None
    server: OwnerWebUIServer | None = None
    try:
        adapter, service = asyncio.run(_create_launcher_service(args))
        server = create_owner_server(
            service,
            scope=args.scope,
            project_id=args.project_id,
            worktree_id=args.worktree_id,
            workflow_id=args.workflow_id,
            agent_id=args.agent_id,
            port=args.port,
        )
        bootstrap_token = server.bootstrap_token
        if bootstrap_token is None:
            raise AdapterError("OWNER_CONTROL_UNAVAILABLE", "Owner WebUI bootstrap token was not created")
        print(
            canonical_json(
                {
                    "status": "READY",
                    "origin": server.owner_state.origin,
                    "owner_path": "/owner/",
                    "bootstrap_path": "/owner/bootstrap",
                    "bootstrap_header": _BOOTSTRAP_HEADER,
                    "bootstrap_token": bootstrap_token,
                    "scope": server.owner_state.scope.to_dict(),
                }
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.25)
        return 0
    except KeyboardInterrupt:
        return 0
    except AdapterError as exc:
        print(
            canonical_json({"status": "BLOCKED", "error_code": exc.code, "message": exc.safe_message}),
            flush=True,
        )
        return 1
    finally:
        if server is not None:
            server.server_close()
        if adapter is not None:
            asyncio.run(adapter.close())


if __name__ == "__main__":
    raise SystemExit(main())
