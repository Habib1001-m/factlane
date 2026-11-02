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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .adapter import MemoryAdapter, PROFILE_DEFINITIONS, trusted_write_context_for_profile
from .contract import AdapterError, SCOPES, ScopeContext, canonical_json, validate_scope
from .owner_control import OwnerControlService
from .owner_contract import OWNER_REASON_CODES


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
_CSS_CONTENT_TYPE = "text/css; charset=utf-8"
_JS_CONTENT_TYPE = "text/javascript; charset=utf-8"
_CSP = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "connect-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "object-src 'none'"
)

_OWNER_CSS = """
:root{color-scheme:dark;--bg:#0b1020;--panel:#121a2e;--panel2:#18223a;--line:#2a3858;--text:#eef3ff;--muted:#a7b4cf;--accent:#78a6ff;--good:#7be0a3;--warn:#ffd27a;--bad:#ff8d9b;font:15px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text)}a{color:var(--accent)}code,pre,textarea{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.shell{max-width:1320px;margin:0 auto;padding:0 20px 48px}.topbar{position:sticky;top:0;z-index:3;background:rgba(11,16,32,.96);border-bottom:1px solid var(--line);padding:16px 0}.brand{display:flex;align-items:center;justify-content:space-between;gap:18px}.brand strong{font-size:18px}.eyebrow,.muted{color:var(--muted);font-size:13px}.nav{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}.nav a{padding:7px 10px;border:1px solid transparent;border-radius:8px;text-decoration:none}.nav a[aria-current=page],.nav a:hover{background:var(--panel);border-color:var(--line)}main{padding-top:28px}h1{margin:0 0 6px;font-size:30px}h2{margin:0 0 14px;font-size:20px}h3{margin:0 0 10px;font-size:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}.metric{font-size:28px;font-weight:700}.badge{display:inline-block;padding:3px 8px;border:1px solid var(--line);border-radius:999px;background:var(--panel2);font-size:12px;white-space:nowrap}.badge.good{color:var(--good)}.badge.warn{color:var(--warn)}.badge.bad{color:var(--bad)}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:10px}table{width:100%;border-collapse:collapse;background:var(--panel)}th,td{text-align:left;vertical-align:top;padding:10px 12px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.fact{max-width:560px;white-space:pre-wrap;overflow-wrap:anywhere}.token{display:block;overflow-wrap:anywhere;color:var(--muted);font-size:12px}.kv{display:grid;grid-template-columns:minmax(150px,220px) 1fr;gap:8px 18px;margin:0}.kv dt{color:var(--muted)}.kv dd{margin:0;overflow-wrap:anywhere}.actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}.action{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:14px}.action label{display:block;margin:8px 0 4px;color:var(--muted);font-size:13px}.action input,.action select,.action textarea{width:100%;background:#0d1426;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:8px}.action textarea{min-height:150px;resize:vertical}.button{display:inline-block;margin-top:10px;padding:8px 12px;border:1px solid #4776ce;border-radius:8px;background:#21447f;color:white;font-weight:600;cursor:pointer}.button.danger{border-color:#a64a59;background:#6d2531}.notice{border-left:3px solid var(--warn);padding:10px 12px;background:#211d17;margin-bottom:16px}.notice.bad{border-color:var(--bad);background:#27171d}.result{border-left:3px solid var(--good)}.pagination{margin:14px 0}.section-stack>*+*{margin-top:16px}@media(max-width:720px){.shell{padding-left:12px;padding-right:12px}.kv{grid-template-columns:1fr}.brand{align-items:flex-start;flex-direction:column}h1{font-size:25px}}
""".strip()

_OWNER_JS = """
(() => {
  document.addEventListener('submit', async (event) => {
    const form = event.target instanceof HTMLFormElement ? event.target : null;
    if (!form || !form.matches('form[data-action]')) return;
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    if (button) button.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Accept': 'text/html'},
        body: new URLSearchParams(new FormData(form)),
      });
      const contentType = response.headers.get('content-type') || '';
      const body = await response.text();
      if (!contentType.includes('text/html')) throw new Error('Owner UI returned a non-HTML response');
      const parsed = new DOMParser().parseFromString(body, 'text/html');
      document.title = parsed.title;
      document.body.replaceWith(parsed.body);
    } catch (_error) {
      if (button) button.disabled = false;
      const notice = document.createElement('div');
      notice.className = 'notice bad';
      notice.textContent = 'The Owner action could not be completed in the browser. Review the current state before retrying.';
      form.prepend(notice);
    }
  });
})();
""".strip()

_NAV_LINKS = (
    ("Overview", "/owner/"),
    ("Current", "/owner/current"),
    ("Candidates", "/owner/candidates"),
    ("Conflicts", "/owner/conflicts"),
    ("Needs Review", "/owner/needs-review"),
    ("History", "/owner/history"),
    ("System Health", "/owner/system-health"),
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
        self.service_call_lock = threading.RLock()
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


class OwnerWebUIServer(ThreadingHTTPServer):
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


def _escape(value: Any) -> str:
    if value is None:
        return "—"
    return html.escape(str(value), quote=True)


def _json_text(value: Any) -> str:
    return html.escape(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), quote=False)


def _badge(value: Any) -> str:
    text = str(value if value is not None else "UNKNOWN")
    upper = text.upper()
    css = ""
    if upper in {"OK", "FRESH", "VALIDATED_CURRENT", "NONE", "CLOSED"}:
        css = " good"
    elif upper in {"STALE", "UNRESOLVED", "RECHECK_REQUIRED", "MANUAL_REVIEW", "CANDIDATE"}:
        css = " warn"
    elif upper in {"QUARANTINED", "BLOCKED", "ERROR", "REVOKED", "REJECTED"}:
        css = " bad"
    return f'<span class="badge{css}">{_escape(text)}</span>'


def _nav(active_path: str) -> str:
    items: list[str] = []
    for label, href in _NAV_LINKS:
        current = (
            (href == "/owner/" and active_path in {"/owner", "/owner/"})
            or (href != "/owner/" and active_path.startswith(href))
        )
        current_attr = ' aria-current="page"' if current else ""
        items.append(f'<a href="{_escape(href)}"{current_attr}>{_escape(label)}</a>')
    return "".join(items)


def _layout(title: str, body: str, *, active_path: str) -> str:
    safe_title = _escape(title)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>FactLane Owner · {safe_title}</title>"
        '<link rel="stylesheet" href="/owner/assets/owner.css">'
        '<script src="/owner/assets/owner.js" defer></script>'
        '</head><body><div class="shell">'
        '<header class="topbar"><div class="brand">'
        '<div><strong>FactLane Owner</strong><div class="eyebrow">Local governed control plane</div></div>'
        '<span class="badge good">Loopback only</span></div>'
        f'<nav class="nav" aria-label="Owner views">{_nav(active_path)}</nav></header>'
        f'<main><h1>{safe_title}</h1>{body}</main></div></body></html>'
    )


def _pagination(payload: Any, path: str) -> str:
    if not isinstance(payload, dict):
        return ""
    cursor = payload.get("next_cursor")
    if not isinstance(cursor, str) or not cursor:
        return ""
    return (
        '<div class="pagination">'
        f'<a class="button" href="{_escape(path)}?cursor={quote(cursor, safe="")}">Next page</a>'
        "</div>"
    )


def _record_rows(payload: Any) -> str:
    items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(items, list) or not items:
        return '<div class="card muted">No records in this bounded view.</div>'
    rows: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        memory_id = str(item.get("memory_id", ""))
        fact = item.get("fact", "")
        rows.append(
            "<tr>"
            f'<td><div class="fact">{_escape(fact)}</div><div class="muted">{_escape(memory_id)}</div></td>'
            f'<td>{_badge(item.get("memory_type"))}</td>'
            f'<td>{_badge(item.get("lifecycle_state"))}</td>'
            f'<td>{_badge(item.get("freshness_gate_state"))}</td>'
            f'<td>{_badge(item.get("contradiction_state"))}</td>'
            f'<td>{_escape(item.get("revision"))}</td>'
            f'<td><a href="/owner/memories/{quote(memory_id, safe="")}">Inspect</a></td>'
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table class="records-table"><thead><tr>'
        '<th>Fact / memory</th><th>Type</th><th>Lifecycle</th><th>Freshness</th>'
        '<th>Contradiction</th><th>Rev</th><th></th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _reason_select(action: str) -> str:
    reasons = sorted(OWNER_REASON_CODES[action])
    return '<select name="reason_code" required>' + "".join(
        f'<option value="{_escape(reason)}">{_escape(reason.replace("_", " ").title())}</option>'
        for reason in reasons
    ) + "</select>"


def _hidden(name: str, value: Any) -> str:
    return f'<input type="hidden" name="{_escape(name)}" value="{_escape(value)}">'


def _new_idempotency(action: str) -> str:
    return f"owner-ui-{action.lower()}-{uuid.uuid4()}"


def _verification_seed(record: dict[str, Any], *, include_fact: bool = False) -> dict[str, Any]:
    seed: dict[str, Any] = {
        "source_provenance": record.get("source_provenance", {}),
        "freshness_policy": record.get("freshness_policy", {}),
        "source_timestamp": record.get("source_timestamp"),
        "verified_by": "OWNER",
        "confidence": record.get("confidence"),
        "tags": record.get("tags", []),
    }
    if include_fact:
        seed["fact"] = record.get("fact", "")
        seed["memory_type"] = record.get("memory_type")
    return seed


def _action_shell(action: str, description: str, fields: str, *, submit_label: str, danger: bool = False) -> str:
    button_class = "button danger" if danger else "button"
    return (
        f'<form class="action" method="post" data-action="{_escape(action)}">'
        f'<h3>{_escape(action.replace("_", " ").title())}</h3><div class="muted">{_escape(description)}</div>'
        f'{fields}<button class="{button_class}" type="submit">{_escape(submit_label)}</button></form>'
    )


def _record_actions(record: dict[str, Any], group: dict[str, Any], csrf_token: str) -> str:
    token = group.get("observed_group_token") if isinstance(group, dict) else None
    if not isinstance(token, str) or not _is_group_token(token):
        return '<div class="notice">Mutation controls are unavailable until a complete mutation-safe group observation is displayed.</div>'
    memory_id = str(record.get("memory_id", ""))
    record_id = str(record.get("record_id", ""))
    revision = record.get("revision")
    lifecycle = record.get("lifecycle_state")
    common = (
        _hidden("_csrf", csrf_token)
        + _hidden("expected_record_id", record_id)
        + _hidden("expected_revision", revision)
        + _hidden("observed_group_token", token)
    )
    actions: list[str] = []
    if lifecycle == "VALIDATED_CURRENT":
        actions.append(
            _action_shell(
                "REVOKE",
                "Remove this record from Current while preserving governed history.",
                common
                + _hidden("idempotency_key", _new_idempotency("revoke"))
                + '<label>Reason</label>'
                + _reason_select("REVOKE"),
                submit_label="Revoke current record",
                danger=True,
            ).replace('method="post"', f'method="post" action="/owner/ui/memories/{quote(memory_id, safe="")}/revoke"', 1)
        )
        reverify = _json_text(_verification_seed(record))
        actions.append(
            _action_shell(
                "REVERIFY",
                "Create the next verified revision without changing semantic identity.",
                common
                + _hidden("idempotency_key", _new_idempotency("reverify"))
                + f'<label>Verification payload</label><textarea name="verification_json" required>{reverify}</textarea>',
                submit_label="Reverify",
            ).replace('method="post"', f'method="post" action="/owner/ui/memories/{quote(memory_id, safe="")}/reverify"', 1)
        )
        replacement = _json_text(_verification_seed(record, include_fact=True))
        actions.append(
            _action_shell(
                "REPLACE",
                "Create a governed replacement; this is not generic edit-in-place.",
                common
                + _hidden("idempotency_key", _new_idempotency("replace"))
                + f'<label>Replacement payload</label><textarea name="replacement_json" required>{replacement}</textarea>',
                submit_label="Replace",
            ).replace('method="post"', f'method="post" action="/owner/ui/memories/{quote(memory_id, safe="")}/replace"', 1)
        )
        freshness = record.get("freshness_policy")
        if isinstance(freshness, dict) and freshness.get("mode") == "on_change":
            evidence = _json_text(
                {
                    "origin": "OWNER_SUPPLIED",
                    "claim": "OBSERVED_HASH",
                    "recheck_ref": "",
                    "observed_at": "",
                    "observed_source_hash": "",
                    "evidence_ref": "",
                    "method": "OWNER_REVIEWED_SOURCE",
                }
            )
            actions.append(
                _action_shell(
                    "MANUAL_RECHECK",
                    "Supply explicit observed source evidence for an on-change freshness review.",
                    common
                    + _hidden("idempotency_key", _new_idempotency("recheck"))
                    + f'<label>Evidence payload</label><textarea name="evidence_json" required>{evidence}</textarea>',
                    submit_label="Submit recheck evidence",
                ).replace('method="post"', f'method="post" action="/owner/ui/memories/{quote(memory_id, safe="")}/recheck"', 1)
            )
    elif lifecycle == "CANDIDATE":
        actions.append(
            _action_shell(
                "REJECT",
                "Reject this Candidate while preserving its history and audit trail.",
                common
                + _hidden("idempotency_key", _new_idempotency("reject"))
                + '<label>Reason</label>'
                + _reason_select("REJECT"),
                submit_label="Reject candidate",
                danger=True,
            ).replace('method="post"', f'method="post" action="/owner/ui/candidates/{quote(memory_id, safe="")}/reject"', 1)
        )
        reverify = _json_text(_verification_seed(record))
        actions.append(
            _action_shell(
                "REVERIFY",
                "Promote only if current proof and complete-group checks remain admissible.",
                common
                + _hidden("idempotency_key", _new_idempotency("candidate-reverify"))
                + f'<label>Verification payload</label><textarea name="verification_json" required>{reverify}</textarea>',
                submit_label="Reverify / promote eligible",
            ).replace('method="post"', f'method="post" action="/owner/ui/memories/{quote(memory_id, safe="")}/reverify"', 1)
        )
    elif lifecycle == "REVOKED":
        verification = _json_text(_verification_seed(record))
        fields = (
            _hidden("_csrf", csrf_token)
            + _hidden("revoked_record_id", record_id)
            + _hidden("expected_revoked_revision", revision)
            + _hidden("observed_group_token", token)
            + _hidden("idempotency_key", _new_idempotency("readmit"))
            + f'<label>Fresh Owner verification</label><textarea name="verification_json" required>{verification}</textarea>'
        )
        actions.append(
            _action_shell(
                "READMIT",
                "Re-admit the exact revoked lineage tip with fresh Owner proof.",
                fields,
                submit_label="Readmit",
            ).replace('method="post"', f'method="post" action="/owner/ui/revoked/{quote(memory_id, safe="")}/readmit"', 1)
        )
    if not actions:
        return '<div class="card muted">This lifecycle state has no ordinary Owner mutation controls.</div>'
    return '<div class="actions">' + "".join(actions) + "</div>"


def _group_observation(group: Any, csrf_token: str, *, contradiction_key: str | None = None) -> str:
    if not isinstance(group, dict):
        return '<div class="notice">No complete group observation is available.</div>'
    token = group.get("observed_group_token")
    members = group.get("members", [])
    if not isinstance(members, list):
        members = []
    member_rows: list[str] = []
    resolve_forms: list[str] = []
    for member in members:
        if not isinstance(member, dict):
            continue
        member_rows.append(
            "<tr>"
            f'<td><a href="/owner/memories/{quote(str(member.get("memory_id", "")), safe="")}">{_escape(member.get("memory_id"))}</a></td>'
            f'<td>{_escape(member.get("record_id"))}</td><td>{_escape(member.get("revision"))}</td>'
            f'<td>{_badge(member.get("lifecycle_state"))}</td><td>{_badge(member.get("contradiction_state"))}</td>'
            f'<td>{_badge(member.get("freshness_gate_state"))}</td></tr>'
        )
        if contradiction_key is not None and isinstance(token, str) and _is_group_token(token):
            verification = ""
            if member.get("lifecycle_state") == "CANDIDATE":
                verification = (
                    '<label>Candidate verification payload</label>'
                    '<textarea name="verification_json" required>{}</textarea>'
                )
            fields = (
                _hidden("_csrf", csrf_token)
                + _hidden("observed_group_token", token)
                + _hidden("winner_memory_id", member.get("memory_id"))
                + _hidden("winner_record_id", member.get("record_id"))
                + _hidden("winner_expected_revision", member.get("revision"))
                + _hidden("idempotency_key", _new_idempotency("resolve"))
                + '<label>Reason</label>'
                + _reason_select("RESOLVE")
                + verification
            )
            form = _action_shell(
                "RESOLVE",
                f"Select revision {member.get('revision')} of {member.get('memory_id')} as the winner for the observed group.",
                fields,
                submit_label="Resolve with this winner",
            ).replace(
                'method="post"',
                f'method="post" action="/owner/ui/conflicts/{quote(contradiction_key, safe="")}/resolve"',
                1,
            )
            resolve_forms.append(form)
    token_html = _escape(token) if isinstance(token, str) else "Unavailable"
    table = (
        '<div class="table-wrap"><table><thead><tr><th>Memory</th><th>Record</th><th>Rev</th>'
        '<th>Lifecycle</th><th>Contradiction</th><th>Freshness</th></tr></thead><tbody>'
        + "".join(member_rows)
        + "</tbody></table></div>"
    )
    resolve = '<div class="actions">' + "".join(resolve_forms) + "</div>" if resolve_forms else ""
    return (
        f'<div class="card"><h3>Group observation</h3><div class="muted">Members: {_escape(group.get("member_count"))}</div>'
        f'<code class="token">{token_html}</code>{table}</div>{resolve}'
    )


def _detail_body(payload: dict[str, Any], csrf_token: str) -> str:
    records = payload.get("records", [])
    events = payload.get("events", [])
    if not isinstance(records, list) or not records:
        return '<div class="notice bad">No visible revisions were returned for this memory.</div>'
    valid_records = [record for record in records if isinstance(record, dict)]
    latest = max(valid_records, key=lambda record: int(record.get("revision", 0)))
    provenance = latest.get("source_provenance", {})
    freshness = latest.get("freshness_policy", {})
    scope = latest.get("scope", {})
    detail = (
        '<div class="grid">'
        f'<section class="card"><h2>Fact</h2><div class="fact">{_escape(latest.get("fact"))}</div></section>'
        '<section class="card"><h2>State</h2><dl class="kv">'
        f'<dt>Lifecycle</dt><dd>{_badge(latest.get("lifecycle_state"))}</dd>'
        f'<dt>Freshness gate</dt><dd>{_badge(latest.get("freshness_gate_state"))}</dd>'
        f'<dt>Contradiction</dt><dd>{_badge(latest.get("contradiction_state"))}</dd>'
        f'<dt>Revision</dt><dd>{_escape(latest.get("revision"))}</dd>'
        f'<dt>Record</dt><dd>{_escape(latest.get("record_id"))}</dd>'
        '</dl></section></div>'
        '<div class="grid">'
        f'<section class="card"><h2>Provenance</h2><pre>{_json_text(provenance)}</pre>'
        f'<div class="muted">Source timestamp: {_escape(latest.get("source_timestamp"))} · Verified by {_escape(latest.get("verified_by"))}</div></section>'
        f'<section class="card"><h2>Freshness</h2><pre>{_json_text(freshness)}</pre>'
        f'<div class="muted">Last verified: {_escape(latest.get("last_verified_at"))}</div></section>'
        '</div>'
        f'<section class="card"><h2>Scope and origin</h2><pre>{_json_text({"scope": scope, "contribution_origin": latest.get("contribution_origin"), "memory_type": latest.get("memory_type"), "tags": latest.get("tags")})}</pre></section>'
    )
    revision_rows = "".join(
        "<tr>"
        f'<td>{_escape(record.get("revision"))}</td><td>{_escape(record.get("record_id"))}</td>'
        f'<td>{_badge(record.get("lifecycle_state"))}</td><td>{_escape(record.get("created_at"))}</td>'
        f'<td>{_escape(record.get("last_verified_at"))}</td></tr>'
        for record in valid_records
    )
    detail += (
        '<section class="card"><h2>Revision history</h2><div class="table-wrap"><table><thead><tr>'
        '<th>Rev</th><th>Record</th><th>Lifecycle</th><th>Created</th><th>Verified</th>'
        f'</tr></thead><tbody>{revision_rows}</tbody></table></div></section>'
    )
    event_rows = ""
    if isinstance(events, list):
        event_rows = "".join(
            "<tr>"
            f'<td>{_escape(event.get("occurred_at"))}</td><td>{_escape(event.get("action"))}</td>'
            f'<td>{_escape(event.get("reason_code"))}</td><td>{_escape(event.get("result_lifecycle_state"))}</td>'
            f'<td>{_escape(event.get("result_revision"))}</td><td>{_escape(event.get("event_id"))}</td></tr>'
            for event in events if isinstance(event, dict)
        )
    detail += (
        '<section class="card"><h2>Lifecycle events</h2><div class="table-wrap"><table><thead><tr>'
        '<th>Occurred</th><th>Action</th><th>Reason</th><th>Result state</th><th>Rev</th><th>Event</th>'
        f'</tr></thead><tbody>{event_rows}</tbody></table></div></section>'
    )
    detail += _group_observation(payload.get("group_cas"), csrf_token)
    detail += '<section class="card"><h2>Governed actions</h2>' + _record_actions(latest, payload.get("group_cas", {}), csrf_token) + "</section>"
    return detail


def _conflicts_body(payload: dict[str, Any], csrf_token: str) -> str:
    items = payload.get("items", [])
    if not isinstance(items, list) or not items:
        return '<div class="card muted">No open contradiction groups in this bounded view.</div>'
    sections: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("contradiction_key", ""))
        sections.append(
            f'<section class="card"><h2>{_escape(key)}</h2><div class="muted">Choose only after reviewing the displayed exact group.</div></section>'
            + _group_observation(item.get("group_cas"), csrf_token, contradiction_key=key)
        )
    return '<div class="section-stack">' + "".join(sections) + "</div>"


def _overview_body(payload: dict[str, Any]) -> str:
    inventory = payload.get("inventory", payload)
    retention = payload.get("retention", {})
    conflicts = payload.get("conflicts", {})
    logical = retention.get("logical_counts", {}) if isinstance(retention, dict) else {}
    conflict_items = conflicts.get("items", []) if isinstance(conflicts, dict) else []
    visible = inventory.get("items", []) if isinstance(inventory, dict) else []
    metrics = [
        ("Visible records", len(visible) if isinstance(visible, list) else 0),
        ("Current", logical.get("VALIDATED_CURRENT", "—") if isinstance(logical, dict) else "—"),
        ("Candidates", logical.get("CANDIDATE", "—") if isinstance(logical, dict) else "—"),
        ("Open conflict groups", len(conflict_items) if isinstance(conflict_items, list) else 0),
    ]
    cards = "".join(
        f'<section class="card"><div class="muted">{_escape(label)}</div><div class="metric">{_escape(value)}</div></section>'
        for label, value in metrics
    )
    return '<div class="grid">' + cards + '</div><section class="card"><h2>Recent scoped records</h2>' + _record_rows(inventory) + "</section>"


def _object_card(title: str, value: Any) -> str:
    return f'<section class="card"><h2>{_escape(title)}</h2><pre>{_json_text(value)}</pre></section>'


def _page_template(title: str, payload: Any, csrf_token: str, active_path: str) -> str:
    if not isinstance(payload, dict):
        body = _object_card("Data", payload)
    elif title == "Overview":
        body = _overview_body(payload)
    elif title in {"Current", "Candidates"}:
        body = _record_rows(payload) + _pagination(payload, active_path)
    elif title == "Conflicts":
        body = _conflicts_body(payload, csrf_token) + _pagination(payload, active_path)
    elif title == "Memory detail":
        body = _detail_body(payload, csrf_token) + _pagination(payload, active_path)
    elif title == "Needs Review":
        body = (
            '<section class="card"><h2>Unresolved</h2>' + _record_rows(payload.get("unresolved", {})) + "</section>"
            '<section class="card"><h2>Stale</h2>' + _record_rows(payload.get("stale", {})) + "</section>"
            '<section class="card"><h2>Conflict groups</h2>' + _conflicts_body(payload.get("conflicts", {}), csrf_token) + "</section>"
        )
    elif title == "History":
        body = (
            '<section class="card"><h2>Historical</h2>' + _record_rows(payload.get("historical", {})) + "</section>"
            '<section class="card"><h2>Superseded</h2>' + _record_rows(payload.get("superseded", {})) + "</section>"
        )
    elif title == "System Health":
        body = _object_card("Runtime health", payload.get("health", {})) + _object_card("Retention and capacity", payload.get("retention", {}))
    else:
        body = _object_card("Data", payload)
    return _layout(title, body, active_path=active_path)


def _operation_result_page(action: str, result: dict[str, Any], *, memory_id: str | None = None) -> str:
    link = (
        f'<a class="button" href="/owner/memories/{quote(memory_id, safe="")}">Review updated memory</a>'
        if memory_id
        else '<a class="button" href="/owner/conflicts">Review conflicts</a>'
    )
    body = (
        f'<section class="card result"><h2>{_escape(action)} completed</h2>'
        '<div class="muted">The governed service accepted the submitted exact observed state.</div>'
        f'<pre>{_json_text(result)}</pre>{link}</section>'
    )
    return _layout(f"{action} result", body, active_path="/owner/")


def _operation_error_page(error: AdapterError, *, review_path: str) -> str:
    refetch = error.code in _CONFLICT_ERRORS
    message = (
        "The observed state changed. Refetch and review the current state before deciding again."
        if refetch
        else error.safe_message
    )
    body = (
        '<section class="card notice bad">'
        f'<h2>{_escape(error.code)}</h2><p>{_escape(message)}</p>'
        f'<a class="button" href="{_escape(review_path)}">Review current state</a></section>'
    )
    return _layout("Action blocked", body, active_path=review_path)


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

    def _send_css(self, status: int, value: str) -> None:
        self._send(status, value.encode("utf-8"), _CSS_CONTENT_TYPE)

    def _send_js(self, status: int, value: str) -> None:
        self._send(status, value.encode("utf-8"), _JS_CONTENT_TYPE)

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
        # Threaded request acceptance prevents an idle browser preconnect from
        # monopolizing the HTTP accept loop, while governed Owner operations
        # remain serialized exactly as they were under the prior HTTPServer.
        with self.state.service_call_lock:
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

    def _page_data(self, path: str, query: str) -> tuple[str, Any] | None:
        scope = self._scope_kwargs()
        if path in {"/owner", "/owner/"}:
            if query:
                raise _transport_error("INVALID_ENVELOPE", "Overview does not accept query parameters")
            return (
                "Overview",
                {
                    "inventory": self._call_service("inventory", limit=20, **scope),
                    "retention": self._call_service("retention_status", **scope),
                    "conflicts": self._call_service("conflicts", limit=20, **scope),
                },
            )
        if path == "/owner/current":
            values = _single_query_values(query, frozenset({"cursor"}))
            return "Current", self._call_service(
                "inventory", lifecycle_state="VALIDATED_CURRENT", limit=20, cursor=values.get("cursor"), **scope
            )
        if path == "/owner/candidates":
            values = _single_query_values(query, frozenset({"cursor"}))
            return "Candidates", self._call_service(
                "inventory", lifecycle_state="CANDIDATE", limit=20, cursor=values.get("cursor"), **scope
            )
        if path == "/owner/conflicts":
            values = _single_query_values(query, frozenset({"cursor"}))
            return "Conflicts", self._call_service("conflicts", limit=20, cursor=values.get("cursor"), **scope)
        if path == "/owner/needs-review":
            if query:
                raise _transport_error("INVALID_ENVELOPE", "Needs Review does not accept query parameters")
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
            if query:
                raise _transport_error("INVALID_ENVELOPE", "History does not accept query parameters")
            return (
                "History",
                {
                    "historical": self._call_service("inventory", lifecycle_state="HISTORICAL", limit=20, **scope),
                    "superseded": self._call_service("inventory", lifecycle_state="SUPERSEDED", limit=20, **scope),
                    "guidance": "Open an authenticated memory detail to inspect bounded per-memory revision and lifecycle-event history.",
                },
            )
        if path == "/owner/system-health":
            if query:
                raise _transport_error("INVALID_ENVELOPE", "System Health does not accept query parameters")
            return (
                "System Health",
                {
                    "health": self._call_service("health", **scope),
                    "retention": self._call_service("retention_status", **scope),
                },
            )
        match = re.fullmatch(r"/owner/memories/([^/]+)", path)
        if match is not None:
            values = _single_query_values(query, frozenset({"cursor"}))
            memory_id = unquote(match.group(1), errors="strict")
            return (
                "Memory detail",
                self._call_service(
                    "detail", memory_id=memory_id, limit=100, cursor=values.get("cursor"), **scope
                ),
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
            if path == "/owner/assets/owner.css":
                if query:
                    raise _transport_error("INVALID_ENVELOPE", "Owner stylesheet does not accept query parameters")
                self._send_css(200, _OWNER_CSS)
                return
            if path == "/owner/assets/owner.js":
                if query:
                    raise _transport_error("INVALID_ENVELOPE", "Owner script does not accept query parameters")
                self._send_js(200, _OWNER_JS)
                return
            result = self._read_api(path, query)
            if result is not None:
                self.state.observe_result(session, result)
                self._send_json(200, result)
                return
            page = self._page_data(path, query)
            if page is None:
                self._send_simple_error(404, "NOT_FOUND", "Owner WebUI route was not found")
                return
            title, payload = page
            self.state.observe_result(session, payload)
            self._send_html(200, _page_template(title, payload, session.csrf_token, path))
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

    def _read_form_body(self) -> dict[str, str]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise _transport_error("INVALID_ENVELOPE", "chunked request bodies are not accepted")
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            raise _transport_error("INVALID_ENVELOPE", "Owner UI form must use application/x-www-form-urlencoded")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise _transport_error("INVALID_ENVELOPE", "Owner UI form requires Content-Length")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise _transport_error("INVALID_ENVELOPE", "Content-Length is invalid") from exc
        if length < 1 or length > MAX_REQUEST_BODY_BYTES:
            raise _transport_error("INVALID_ENVELOPE", "Owner UI form is empty or exceeds the 64 KiB cap")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise _transport_error("INVALID_ENVELOPE", "Owner UI form body was truncated")
        try:
            parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=32)
        except (UnicodeDecodeError, ValueError) as exc:
            raise _transport_error("INVALID_ENVELOPE", "Owner UI form body is invalid") from exc
        result: dict[str, str] = {}
        for key, values in parsed.items():
            if not isinstance(key, str) or len(values) != 1:
                raise _transport_error("INVALID_ENVELOPE", "Owner UI form fields must occur exactly once")
            result[key] = values[0]
        return result

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

    def _match_ui_mutation(self, path: str) -> _MutationRoute | None:
        memory_patterns = (
            (
                r"/owner/ui/memories/([^/]+)/revoke",
                "revoke",
                frozenset({"expected_record_id", "expected_revision", "observed_group_token", "idempotency_key", "reason_code"}),
            ),
            (
                r"/owner/ui/memories/([^/]+)/reverify",
                "reverify",
                frozenset({"expected_record_id", "expected_revision", "observed_group_token", "idempotency_key", "verification"}),
            ),
            (
                r"/owner/ui/memories/([^/]+)/replace",
                "replace",
                frozenset({"expected_record_id", "expected_revision", "observed_group_token", "idempotency_key", "replacement"}),
            ),
            (
                r"/owner/ui/memories/([^/]+)/recheck",
                "manual_recheck",
                frozenset({"expected_record_id", "expected_revision", "observed_group_token", "idempotency_key", "evidence"}),
            ),
        )
        for pattern, method_name, required in memory_patterns:
            match = re.fullmatch(pattern, path)
            if match is not None:
                memory_id = unquote(match.group(1), errors="strict")
                return _MutationRoute(method_name, "memory_id", memory_id, memory_id, required)

        match = re.fullmatch(r"/owner/ui/candidates/([^/]+)/reject", path)
        if match is not None:
            memory_id = unquote(match.group(1), errors="strict")
            return _MutationRoute(
                "reject",
                "memory_id",
                memory_id,
                memory_id,
                frozenset({"expected_record_id", "expected_revision", "observed_group_token", "idempotency_key", "reason_code"}),
            )

        match = re.fullmatch(r"/owner/ui/revoked/([^/]+)/readmit", path)
        if match is not None:
            memory_id = unquote(match.group(1), errors="strict")
            return _MutationRoute(
                "readmit",
                "memory_id",
                memory_id,
                memory_id,
                frozenset({"revoked_record_id", "expected_revoked_revision", "observed_group_token", "idempotency_key", "verification"}),
            )

        match = re.fullmatch(r"/owner/ui/conflicts/([^/]+)/resolve", path)
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

    @staticmethod
    def _parse_form_json(raw: str, field: str, *, required: bool) -> dict[str, Any] | None:
        if raw == "" and not required:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _transport_error("INVALID_ENVELOPE", f"{field} must contain one JSON object") from exc
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise _transport_error("INVALID_ENVELOPE", f"{field} must contain one JSON object")
        return value

    def _ui_form_payload(self, route: _MutationRoute, form: dict[str, str]) -> dict[str, Any]:
        json_fields = {
            "verification": "verification_json",
            "replacement": "replacement_json",
            "evidence": "evidence_json",
        }
        allowed = {"_csrf"}
        for field in route.required_fields | route.optional_fields:
            allowed.add(json_fields.get(field, field))
        unexpected = sorted(set(form) - allowed)
        if unexpected:
            raise _transport_error("INVALID_ENVELOPE", "Owner UI form contains unsupported fields")

        body: dict[str, Any] = {}
        for field in route.required_fields:
            if field in json_fields:
                raw = form.get(json_fields[field])
                if raw is None:
                    raise _transport_error("INVALID_ENVELOPE", "Owner UI form is missing a required payload")
                parsed = self._parse_form_json(raw, json_fields[field], required=True)
                assert parsed is not None
                body[field] = parsed
                continue
            raw = form.get(field)
            if raw is None or raw == "":
                raise _transport_error("INVALID_ENVELOPE", "Owner UI form is missing required structural fields")
            if field in {"expected_revision", "expected_revoked_revision", "winner_expected_revision"}:
                try:
                    body[field] = int(raw)
                except ValueError as exc:
                    raise _transport_error("INVALID_ENVELOPE", f"{field} must be an integer") from exc
            else:
                body[field] = raw

        if "verification" in route.optional_fields and "verification_json" in form:
            parsed = self._parse_form_json(form["verification_json"], "verification_json", required=False)
            if parsed is not None:
                body["verification"] = parsed
        if "request_id" in route.optional_fields and form.get("request_id"):
            body["request_id"] = form["request_id"]
        return self._validate_mutation_body(route, body)

    @staticmethod
    def _ui_review_path(route: _MutationRoute) -> str:
        if route.method_name == "resolve":
            return "/owner/conflicts"
        return f"/owner/memories/{quote(route.inspection_memory_id, safe='')}"

    def _handle_ui_mutation(self, path: str, session: _Session) -> bool:
        route = self._match_ui_mutation(path)
        if route is None:
            return False
        review_path = self._ui_review_path(route)
        try:
            method = getattr(self.state.service, route.method_name, None)
            if method is None or not callable(method):
                raise AdapterError(
                    "OWNER_CONTROL_UNAVAILABLE",
                    f"OwnerControlService does not expose the governed {route.method_name} operation",
                )
            form = self._read_form_body()
            csrf = form.get("_csrf")
            if csrf is None or not secrets.compare_digest(session.csrf_token, csrf):
                raise AdapterError("CSRF_DENIED", "Owner mutation CSRF token is invalid")
            payload = self._ui_form_payload(route, form)
            self._require_displayed_group(session, payload)
            result = self._call_service(route.method_name, **payload, **self._scope_kwargs())
            self.state.observe_result(session, result)
            action = result.get("action") if isinstance(result.get("action"), str) else route.method_name.upper()
            memory_id = route.route_value if route.route_field == "memory_id" else None
            self._send_html(200, _operation_result_page(action, result, memory_id=memory_id))
        except AdapterError as exc:
            self._send_html(_adapter_status(exc.code), _operation_error_page(exc, review_path=review_path))
        return True

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
            if path.startswith("/owner/ui/"):
                if self._handle_ui_mutation(path, session):
                    return
                self._send_html(
                    404,
                    _layout(
                        "Not found",
                        '<section class="card notice bad">Owner UI mutation route was not found.</section>',
                        active_path="/owner/",
                    ),
                )
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
