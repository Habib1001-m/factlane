from __future__ import annotations

import http.client
import json
import socket
import threading
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlencode

import pytest

from factlane.contract import AdapterError
from factlane.owner_webui import _build_launcher_parser, create_owner_server


GROUP_TOKEN = "a" * 64


class FakeOwnerService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: dict[str, AdapterError] = {}
        self.detail_lifecycle = "VALIDATED_CURRENT"

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, dict(kwargs)))
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    async def inventory(self, **kwargs: Any) -> dict[str, Any]:
        self._record("inventory", kwargs)
        return {
            "status": "OK",
            "items": [
                {
                    "memory_id": "11111111-1111-4111-8111-111111111111",
                    "record_id": "22222222-2222-4222-8222-222222222222",
                    "revision": 1,
                    "fact": "<script>alert(1)</script>",
                    "lifecycle_state": kwargs.get("lifecycle_state") or "VALIDATED_CURRENT",
                    "group_cas": {
                        "observed_group_token": GROUP_TOKEN,
                        "member_count": 1,
                        "members": [],
                    },
                }
            ],
            "next_cursor": None,
        }

    async def detail(self, **kwargs: Any) -> dict[str, Any]:
        self._record("detail", kwargs)
        memory_id = kwargs["memory_id"]
        return {
            "status": "OK",
            "memory_id": memory_id,
            "records": [
                {
                    "memory_id": memory_id,
                    "record_id": "22222222-2222-4222-8222-222222222222",
                    "revision": 1,
                    "parent_record_id": None,
                    "scope": {"scope": "PROJECT", "project_id": "project-a"},
                    "memory_type": "FACT",
                    "fact": "<script>alert(1)</script>",
                    "source_provenance": {"source_class": "OWNER_SUPPLIED", "source_ref": "owner:test"},
                    "source_timestamp": "2026-11-01T00:00:00Z",
                    "created_at": "2026-11-01T00:00:00Z",
                    "last_verified_at": "2026-11-01T00:00:00Z",
                    "verified_by": "OWNER",
                    "authority_role": "OWNER",
                    "contribution_origin": {"contributor_class": "OWNER", "contributor_ref": "owner:test"},
                    "freshness_policy": {"mode": "manual"},
                    "supersedes": [],
                    "contradiction_state": "NONE",
                    "confidence": 1.0,
                    "tags": ["subject:test"],
                    "lifecycle_state": self.detail_lifecycle,
                    "freshness_gate_state": "NONE",
                }
            ],
            "events": [
                {
                    "event_id": "event-1",
                    "action": "STORE",
                    "occurred_at": "2026-11-01T00:00:00Z",
                    "result_lifecycle_state": self.detail_lifecycle,
                    "result_revision": 1,
                }
            ],
            "group_cas": {
                "observed_group_token": GROUP_TOKEN,
                "member_count": 1,
                "members": [
                    {
                        "memory_id": memory_id,
                        "record_id": "22222222-2222-4222-8222-222222222222",
                        "revision": 1,
                        "lifecycle_state": self.detail_lifecycle,
                        "contradiction_state": "NONE",
                        "freshness_gate_state": "NONE",
                        "member_token": "b" * 64,
                    }
                ],
            },
            "next_cursor": None,
        }

    async def conflicts(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conflicts", kwargs)
        return {
            "status": "OK",
            "items": [
                {
                    "contradiction_key": "subject:test",
                    "group_cas": {
                        "observed_group_token": GROUP_TOKEN,
                        "member_count": 2,
                        "members": [
                            {
                                "memory_id": "11111111-1111-4111-8111-111111111111",
                                "record_id": "22222222-2222-4222-8222-222222222222",
                                "revision": 1,
                                "lifecycle_state": "VALIDATED_CURRENT",
                                "contradiction_state": "UNRESOLVED",
                                "freshness_gate_state": "NONE",
                                "member_token": "b" * 64,
                            },
                            {
                                "memory_id": "33333333-3333-4333-8333-333333333333",
                                "record_id": "44444444-4444-4444-8444-444444444444",
                                "revision": 2,
                                "lifecycle_state": "CANDIDATE",
                                "contradiction_state": "UNRESOLVED",
                                "freshness_gate_state": "NONE",
                                "member_token": "c" * 64,
                            },
                        ],
                    },
                }
            ],
            "next_cursor": None,
        }

    async def health(self, **kwargs: Any) -> dict[str, Any]:
        self._record("health", kwargs)
        return {"status": "OK", "backend_status": {"healthy": True}}

    async def retention_status(self, **kwargs: Any) -> dict[str, Any]:
        self._record("retention_status", kwargs)
        return {"status": "OK", "logical_counts": {"VALIDATED_CURRENT": 1}}

    async def revoke(self, **kwargs: Any) -> dict[str, Any]:
        self._record("revoke", kwargs)
        return {"status": "OK", "action": "REVOKE", "observed_group_token": kwargs["observed_group_token"]}

    async def reject(self, **kwargs: Any) -> dict[str, Any]:
        self._record("reject", kwargs)
        return {"status": "OK", "action": "REJECT"}

    async def resolve(self, **kwargs: Any) -> dict[str, Any]:
        self._record("resolve", kwargs)
        return {"status": "OK", "action": "RESOLVE"}

    async def reverify(self, **kwargs: Any) -> dict[str, Any]:
        self._record("reverify", kwargs)
        return {"status": "OK", "action": "REVERIFY"}

    async def replace(self, **kwargs: Any) -> dict[str, Any]:
        self._record("replace", kwargs)
        return {"status": "OK", "action": "REPLACE"}

    async def manual_recheck(self, **kwargs: Any) -> dict[str, Any]:
        self._record("manual_recheck", kwargs)
        return {"status": "OK", "action": "MANUAL_RECHECK"}

    async def readmit(self, **kwargs: Any) -> dict[str, Any]:
        self._record("readmit", kwargs)
        return {"status": "OK", "action": "READMIT"}


@contextmanager
def running_server(service: Any) -> Iterator[Any]:
    server = create_owner_server(
        service,
        scope="PROJECT",
        project_id="project-a",
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(
    server: Any,
    method: str,
    target: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    port = server.server_address[1]
    request_headers = {"Host": f"127.0.0.1:{port}"}
    if headers:
        request_headers.update(headers)
    body: bytes | None = None
    assert json_body is None or form_body is None
    if json_body is not None:
        body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if form_body is not None:
        body = urlencode(form_body).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request(method, target, body=body, headers=request_headers)
        response = conn.getresponse()
        payload = response.read()
        return response.status, {name.lower(): value for name, value in response.getheaders()}, payload
    finally:
        conn.close()


def bootstrap(server: Any) -> tuple[str, str]:
    bootstrap_token = server.bootstrap_token
    assert bootstrap_token is not None
    status, headers, body = request(
        server,
        "POST",
        "/owner/bootstrap",
        headers={"X-FactLane-Bootstrap": bootstrap_token},
    )
    assert status == 200
    result = json.loads(body)
    return headers["set-cookie"].split(";", 1)[0], result["csrf_token"]


def assert_security_headers(headers: dict[str, str]) -> None:
    assert headers["cache-control"] == "no-store"
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert "access-control-allow-origin" not in headers


def test_loopback_bootstrap_is_one_use_and_every_get_requires_authentication() -> None:
    service = FakeOwnerService()
    with pytest.raises(ValueError):
        create_owner_server(service, scope="PROJECT", project_id="project-a", host="0.0.0.0", port=0)
    with pytest.raises(ValueError):
        create_owner_server(service, scope="PROJECT", project_id="project-a", host="localhost", port=0)

    with running_server(service) as server:
        assert server.server_address[0] == "127.0.0.1"
        initial_bootstrap = server.bootstrap_token
        assert isinstance(initial_bootstrap, str) and len(initial_bootstrap) >= 32

        status, _, _ = request(
            server,
            "POST",
            f"/owner/bootstrap?token={initial_bootstrap}",
            headers={"X-FactLane-Bootstrap": initial_bootstrap},
        )
        assert status == 400
        assert server.bootstrap_token == initial_bootstrap

        status, headers, _ = request(server, "GET", "/owner/api/v1/health")
        assert status == 401
        assert_security_headers(headers)

        cookie, csrf = bootstrap(server)
        assert csrf
        assert server.bootstrap_token is None
        status, headers, _ = request(
            server,
            "POST",
            "/owner/bootstrap",
            headers={"X-FactLane-Bootstrap": initial_bootstrap},
        )
        assert status == 401
        assert "set-cookie" not in headers

        status, headers, body = request(
            server,
            "GET",
            "/owner/api/v1/health",
            headers={"Cookie": cookie},
        )
        assert status == 200
        assert json.loads(body)["status"] == "OK"
        assert_security_headers(headers)

        status, _, _ = request(server, "GET", "/does-not-exist")
        assert status == 401
        status, _, _ = request(server, "GET", "/does-not-exist", headers={"Cookie": cookie})
        assert status == 404


def test_idle_browser_preconnect_does_not_starve_other_requests() -> None:
    service = FakeOwnerService()
    with running_server(service) as server:
        port = server.server_address[1]
        idle = socket.create_connection(("127.0.0.1", port), timeout=2)
        try:
            # Browsers may speculatively open a TCP connection before sending an
            # HTTP request. The Owner WebUI must still serve other loopback
            # requests instead of letting that idle connection monopolize the
            # server accept loop.
            status, headers, body = request(server, "GET", "/owner/api/v1/health")
            assert status == 401
            assert json.loads(body)["error_code"] == "UNAUTHENTICATED"
            assert_security_headers(headers)
        finally:
            idle.close()


def test_session_cookie_is_host_only_strict_http_only_and_restart_invalidates_bootstrap() -> None:
    service = FakeOwnerService()
    with running_server(service) as first:
        old_bootstrap = first.bootstrap_token
        assert old_bootstrap is not None
        status, headers, _ = request(
            first,
            "POST",
            "/owner/bootstrap",
            headers={"X-FactLane-Bootstrap": old_bootstrap},
        )
        assert status == 200
        set_cookie = headers["set-cookie"]
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie
        assert "Path=/owner" in set_cookie
        assert "Domain=" not in set_cookie

    with running_server(FakeOwnerService()) as second:
        assert second.bootstrap_token != old_bootstrap
        status, _, _ = request(
            second,
            "POST",
            "/owner/bootstrap",
            headers={"X-FactLane-Bootstrap": old_bootstrap},
        )
        assert status == 401


def test_host_origin_referer_csrf_scope_and_group_token_are_enforced_without_retry() -> None:
    service = FakeOwnerService()
    with running_server(service) as server:
        cookie, csrf = bootstrap(server)
        origin = f"http://127.0.0.1:{server.server_address[1]}"

        status, _, _ = request(
            server,
            "GET",
            "/owner/api/v1/memories/11111111-1111-4111-8111-111111111111",
            headers={"Cookie": cookie, "Host": "attacker.invalid"},
        )
        assert status == 403

        payload = {
            "expected_record_id": "22222222-2222-4222-8222-222222222222",
            "expected_revision": 1,
            "observed_group_token": GROUP_TOKEN,
            "idempotency_key": "webui-revoke-1",
            "reason_code": "OUTDATED",
        }

        status, _, body = request(
            server,
            "POST",
            "/owner/api/v1/memories/11111111-1111-4111-8111-111111111111/revoke",
            headers={"Cookie": cookie, "X-FactLane-CSRF": csrf, "Origin": origin},
            json_body=payload,
        )
        assert status == 400
        assert json.loads(body)["error_code"] == "INVALID_ENVELOPE"
        assert not [call for call in service.calls if call[0] == "revoke"]

        status, _, _ = request(
            server,
            "GET",
            "/owner/api/v1/memories/11111111-1111-4111-8111-111111111111",
            headers={"Cookie": cookie},
        )
        assert status == 200

        baseline = len([call for call in service.calls if call[0] == "revoke"])
        status, _, _ = request(
            server,
            "POST",
            "/owner/api/v1/memories/11111111-1111-4111-8111-111111111111/revoke",
            headers={"Cookie": cookie, "X-FactLane-CSRF": csrf},
            json_body=payload,
        )
        assert status == 403
        assert len([call for call in service.calls if call[0] == "revoke"]) == baseline

        status, _, _ = request(
            server,
            "POST",
            "/owner/api/v1/memories/11111111-1111-4111-8111-111111111111/revoke",
            headers={"Cookie": cookie, "X-FactLane-CSRF": csrf, "Origin": "http://evil.invalid"},
            json_body=payload,
        )
        assert status == 403

        status, _, _ = request(
            server,
            "POST",
            "/owner/api/v1/memories/11111111-1111-4111-8111-111111111111/revoke",
            headers={"Cookie": cookie, "X-FactLane-CSRF": "wrong", "Origin": origin},
            json_body=payload,
        )
        assert status == 403

        status, _, body = request(
            server,
            "POST",
            "/owner/api/v1/memories/11111111-1111-4111-8111-111111111111/revoke",
            headers={"Cookie": cookie, "X-FactLane-CSRF": csrf, "Referer": f"{origin}/owner/current"},
            json_body=payload,
        )
        assert status == 200
        assert json.loads(body)["action"] == "REVOKE"
        revoke_calls = [call for call in service.calls if call[0] == "revoke"]
        assert len(revoke_calls) == baseline + 1
        assert revoke_calls[-1][1]["scope"] == "PROJECT"
        assert revoke_calls[-1][1]["project_id"] == "project-a"
        assert revoke_calls[-1][1]["observed_group_token"] == GROUP_TOKEN

        status, _, _ = request(
            server,
            "POST",
            "/owner/api/v1/memories/11111111-1111-4111-8111-111111111111/revoke",
            headers={"Cookie": cookie, "X-FactLane-CSRF": csrf, "Origin": origin},
            json_body={**payload, "scope": "GLOBAL_USER"},
        )
        assert status == 400

        service.failures["revoke"] = AdapterError(
            "GROUP_VERSION_CONFLICT",
            "The contradiction group changed; inspect it again before mutation.",
        )
        before_conflict = len([call for call in service.calls if call[0] == "revoke"])
        status, _, body = request(
            server,
            "POST",
            "/owner/api/v1/memories/11111111-1111-4111-8111-111111111111/revoke",
            headers={"Cookie": cookie, "X-FactLane-CSRF": csrf, "Origin": origin},
            json_body={**payload, "idempotency_key": "webui-revoke-conflict"},
        )
        assert status == 409
        assert json.loads(body)["error_code"] == "GROUP_VERSION_CONFLICT"
        assert len([call for call in service.calls if call[0] == "revoke"]) == before_conflict + 1


def test_displayed_stale_group_token_is_forwarded_to_service_for_replay() -> None:
    class ChangingDetailOwnerService(FakeOwnerService):
        def __init__(self) -> None:
            super().__init__()
            self.detail_token = GROUP_TOKEN

        async def detail(self, **kwargs: Any) -> dict[str, Any]:
            result = await super().detail(**kwargs)
            result["group_cas"]["observed_group_token"] = self.detail_token
            return result

    service = ChangingDetailOwnerService()
    with running_server(service) as server:
        cookie, csrf = bootstrap(server)
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        status, _, _ = request(
            server,
            "GET",
            "/owner/api/v1/memories/11111111-1111-4111-8111-111111111111",
            headers={"Cookie": cookie},
        )
        assert status == 200
        detail_calls = len([call for call in service.calls if call[0] == "detail"])
        service.detail_token = "b" * 64

        status, _, body = request(
            server,
            "POST",
            "/owner/api/v1/memories/11111111-1111-4111-8111-111111111111/reverify",
            headers={"Cookie": cookie, "X-FactLane-CSRF": csrf, "Origin": origin},
            json_body={
                "expected_record_id": "22222222-2222-4222-8222-222222222222",
                "expected_revision": 1,
                "observed_group_token": GROUP_TOKEN,
                "idempotency_key": "webui-reverify-replay",
                "verification": {"verified_by": "OWNER"},
            },
        )
        assert status == 200
        assert json.loads(body)["action"] == "REVERIFY"
        assert len([call for call in service.calls if call[0] == "detail"]) == detail_calls
        reverify_calls = [call for call in service.calls if call[0] == "reverify"]
        assert len(reverify_calls) == 1
        assert reverify_calls[0][1]["observed_group_token"] == GROUP_TOKEN


def test_read_api_pages_are_scope_bound_safe_and_include_all_owner_views() -> None:
    service = FakeOwnerService()
    with running_server(service) as server:
        cookie, _ = bootstrap(server)

        status, _, body = request(
            server,
            "GET",
            "/owner/api/v1/inventory?lifecycle_state=CANDIDATE&limit=10",
            headers={"Cookie": cookie},
        )
        assert status == 200
        assert json.loads(body)["items"][0]["lifecycle_state"] == "CANDIDATE"
        inventory_call = [call for call in service.calls if call[0] == "inventory"][-1][1]
        assert inventory_call["scope"] == "PROJECT"
        assert inventory_call["project_id"] == "project-a"
        assert inventory_call["limit"] == 10

        status, _, body = request(
            server,
            "GET",
            "/owner/api/v1/status/retention",
            headers={"Cookie": cookie},
        )
        assert status == 200
        assert json.loads(body)["logical_counts"]["VALIDATED_CURRENT"] == 1

        status, _, body = request(
            server,
            "GET",
            "/owner/api/v1/inventory?scope=GLOBAL_USER",
            headers={"Cookie": cookie},
        )
        assert status == 400
        assert json.loads(body)["error_code"] == "INVALID_ENVELOPE"

        pages = {
            "/owner/": "Overview",
            "/owner/current": "Current",
            "/owner/candidates": "Candidates",
            "/owner/conflicts": "Conflicts",
            "/owner/needs-review": "Needs Review",
            "/owner/history": "History",
            "/owner/system-health": "System Health",
        }
        for path, title in pages.items():
            status, headers, body = request(server, "GET", path, headers={"Cookie": cookie})
            text = body.decode("utf-8")
            assert status == 200, path
            assert title in text
            assert "<script>alert(1)</script>" not in text
            assert "localStorage" not in text
            assert_security_headers(headers)


def test_owner_pages_render_visible_operator_workflows_and_record_detail() -> None:
    service = FakeOwnerService()
    memory_id = "11111111-1111-4111-8111-111111111111"
    with running_server(service) as server:
        cookie, _ = bootstrap(server)

        status, headers, body = request(server, "GET", "/owner/current", headers={"Cookie": cookie})
        text = body.decode("utf-8")
        assert status == 200
        assert '<table class="records-table"' in text
        assert f'href="/owner/memories/{memory_id}"' in text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
        assert "<script>alert(1)</script>" not in text
        assert "style-src 'self'" in headers["content-security-policy"]
        assert "script-src 'self'" in headers["content-security-policy"]
        assert '<script src="/owner/assets/owner.js" defer></script>' in text

        status, _, script = request(server, "GET", "/owner/assets/owner.js", headers={"Cookie": cookie})
        assert status == 200
        script_text = script.decode("utf-8")
        assert "fetch(form.action" in script_text
        assert "history.replaceState" not in script_text

        status, _, body = request(server, "GET", f"/owner/memories/{memory_id}", headers={"Cookie": cookie})
        detail = body.decode("utf-8")
        assert status == 200
        for label in (
            "Memory detail",
            "Provenance",
            "Freshness",
            "Revision history",
            "Lifecycle events",
            "Group observation",
        ):
            assert label in detail
        for action in ("REVOKE", "REVERIFY", "REPLACE"):
            assert f'data-action="{action}"' in detail
        assert GROUP_TOKEN in detail
        assert "localStorage" not in detail


def test_conflicts_page_renders_all_observed_members_and_resolve_controls() -> None:
    service = FakeOwnerService()
    with running_server(service) as server:
        cookie, _ = bootstrap(server)
        status, _, body = request(server, "GET", "/owner/conflicts", headers={"Cookie": cookie})
        text = body.decode("utf-8")
        assert status == 200
        assert "subject:test" in text
        assert "11111111-1111-4111-8111-111111111111" in text
        assert "33333333-3333-4333-8333-333333333333" in text
        assert text.count('data-action="RESOLVE"') == 2
        assert GROUP_TOKEN in text


def test_visible_revoke_form_submits_displayed_group_and_returns_operator_result() -> None:
    service = FakeOwnerService()
    memory_id = "11111111-1111-4111-8111-111111111111"
    record_id = "22222222-2222-4222-8222-222222222222"
    with running_server(service) as server:
        cookie, csrf = bootstrap(server)
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        status, _, _ = request(server, "GET", f"/owner/memories/{memory_id}", headers={"Cookie": cookie})
        assert status == 200

        status, _, body = request(
            server,
            "POST",
            f"/owner/ui/memories/{memory_id}/revoke",
            headers={"Cookie": cookie, "Referer": f"{origin}/owner/memories/{memory_id}"},
            form_body={
                "_csrf": csrf,
                "expected_record_id": record_id,
                "expected_revision": "1",
                "observed_group_token": GROUP_TOKEN,
                "idempotency_key": "ui-revoke-1",
                "reason_code": "OUTDATED",
            },
        )
        text = body.decode("utf-8")
        assert status == 200
        assert "REVOKE completed" in text
        assert "Review updated memory" in text
        call = [call for call in service.calls if call[0] == "revoke"][-1][1]
        assert call["scope"] == "PROJECT"
        assert call["project_id"] == "project-a"
        assert call["observed_group_token"] == GROUP_TOKEN
        assert call["expected_revision"] == 1


def test_visible_mutation_conflict_stops_and_requires_explicit_refetch_review() -> None:
    service = FakeOwnerService()
    memory_id = "11111111-1111-4111-8111-111111111111"
    with running_server(service) as server:
        cookie, csrf = bootstrap(server)
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        status, _, _ = request(server, "GET", f"/owner/memories/{memory_id}", headers={"Cookie": cookie})
        assert status == 200
        service.failures["revoke"] = AdapterError(
            "GROUP_VERSION_CONFLICT",
            "The contradiction group changed; inspect it again before mutation.",
        )
        before = len([call for call in service.calls if call[0] == "revoke"])

        status, _, body = request(
            server,
            "POST",
            f"/owner/ui/memories/{memory_id}/revoke",
            headers={"Cookie": cookie, "Origin": origin},
            form_body={
                "_csrf": csrf,
                "expected_record_id": "22222222-2222-4222-8222-222222222222",
                "expected_revision": "1",
                "observed_group_token": GROUP_TOKEN,
                "idempotency_key": "ui-revoke-conflict",
                "reason_code": "OUTDATED",
            },
        )
        text = body.decode("utf-8")
        assert status == 409
        assert "GROUP_VERSION_CONFLICT" in text
        assert "Review current state" in text
        assert f'href="/owner/memories/{memory_id}"' in text
        assert len([call for call in service.calls if call[0] == "revoke"]) == before + 1


@pytest.mark.parametrize(
    ("target", "form_body", "method_name"),
    [
        (
            "/owner/ui/candidates/11111111-1111-4111-8111-111111111111/reject",
            {
                "expected_record_id": "22222222-2222-4222-8222-222222222222",
                "expected_revision": "1",
                "observed_group_token": GROUP_TOKEN,
                "idempotency_key": "ui-reject-1",
                "reason_code": "INSUFFICIENT_EVIDENCE",
            },
            "reject",
        ),
        (
            "/owner/ui/memories/11111111-1111-4111-8111-111111111111/reverify",
            {
                "expected_record_id": "22222222-2222-4222-8222-222222222222",
                "expected_revision": "1",
                "observed_group_token": GROUP_TOKEN,
                "idempotency_key": "ui-reverify-1",
                "verification_json": '{"verified_by":"OWNER"}',
            },
            "reverify",
        ),
        (
            "/owner/ui/memories/11111111-1111-4111-8111-111111111111/replace",
            {
                "expected_record_id": "22222222-2222-4222-8222-222222222222",
                "expected_revision": "1",
                "observed_group_token": GROUP_TOKEN,
                "idempotency_key": "ui-replace-1",
                "replacement_json": '{"fact":"replacement"}',
            },
            "replace",
        ),
        (
            "/owner/ui/memories/11111111-1111-4111-8111-111111111111/recheck",
            {
                "expected_record_id": "22222222-2222-4222-8222-222222222222",
                "expected_revision": "1",
                "observed_group_token": GROUP_TOKEN,
                "idempotency_key": "ui-recheck-1",
                "evidence_json": '{"origin":"OWNER_SUPPLIED"}',
            },
            "manual_recheck",
        ),
        (
            "/owner/ui/revoked/11111111-1111-4111-8111-111111111111/readmit",
            {
                "revoked_record_id": "22222222-2222-4222-8222-222222222222",
                "expected_revoked_revision": "1",
                "observed_group_token": GROUP_TOKEN,
                "idempotency_key": "ui-readmit-1",
                "verification_json": '{"verified_by":"OWNER"}',
            },
            "readmit",
        ),
        (
            "/owner/ui/conflicts/subject%3Atest/resolve",
            {
                "observed_group_token": GROUP_TOKEN,
                "winner_memory_id": "11111111-1111-4111-8111-111111111111",
                "winner_record_id": "22222222-2222-4222-8222-222222222222",
                "winner_expected_revision": "1",
                "idempotency_key": "ui-resolve-1",
                "reason_code": "OWNER_ADJUDICATION",
                "verification_json": "",
            },
            "resolve",
        ),
    ],
)
def test_visible_forms_route_all_governed_actions_through_owner_service(
    target: str,
    form_body: dict[str, str],
    method_name: str,
) -> None:
    service = FakeOwnerService()
    with running_server(service) as server:
        cookie, csrf = bootstrap(server)
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        memory_id = "11111111-1111-4111-8111-111111111111"
        status, _, _ = request(server, "GET", f"/owner/memories/{memory_id}", headers={"Cookie": cookie})
        assert status == 200
        if method_name == "resolve":
            status, _, _ = request(server, "GET", "/owner/conflicts", headers={"Cookie": cookie})
            assert status == 200
        before = len([call for call in service.calls if call[0] == method_name])
        status, _, body = request(
            server,
            "POST",
            target,
            headers={"Cookie": cookie, "Origin": origin},
            form_body={"_csrf": csrf, **form_body},
        )
        assert status == 200, body
        assert len([call for call in service.calls if call[0] == method_name]) == before + 1


def test_all_mutation_routes_delegate_only_to_owner_service_methods() -> None:
    service = FakeOwnerService()
    with running_server(service) as server:
        cookie, csrf = bootstrap(server)
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        headers = {"Cookie": cookie, "X-FactLane-CSRF": csrf, "Origin": origin}
        memory_id = "11111111-1111-4111-8111-111111111111"
        record_id = "22222222-2222-4222-8222-222222222222"

        status, _, _ = request(
            server,
            "GET",
            f"/owner/api/v1/memories/{memory_id}",
            headers={"Cookie": cookie},
        )
        assert status == 200

        cases = [
            (
                f"/owner/api/v1/candidates/{memory_id}/reject",
                {
                    "expected_record_id": record_id,
                    "expected_revision": 1,
                    "observed_group_token": GROUP_TOKEN,
                    "idempotency_key": "reject-1",
                    "reason_code": "INSUFFICIENT_EVIDENCE",
                },
                "reject",
            ),
            (
                "/owner/api/v1/conflicts/subject%3Atest/resolve",
                {
                    "observed_group_token": GROUP_TOKEN,
                    "winner_memory_id": memory_id,
                    "winner_record_id": record_id,
                    "winner_expected_revision": 1,
                    "idempotency_key": "resolve-1",
                    "reason_code": "OWNER_ADJUDICATION",
                },
                "resolve",
            ),
            (
                f"/owner/api/v1/memories/{memory_id}/reverify",
                {
                    "expected_record_id": record_id,
                    "expected_revision": 1,
                    "observed_group_token": GROUP_TOKEN,
                    "idempotency_key": "reverify-1",
                    "verification": {"verified_by": "OWNER"},
                },
                "reverify",
            ),
            (
                f"/owner/api/v1/memories/{memory_id}/replace",
                {
                    "expected_record_id": record_id,
                    "expected_revision": 1,
                    "observed_group_token": GROUP_TOKEN,
                    "idempotency_key": "replace-1",
                    "replacement": {"fact": "replacement"},
                },
                "replace",
            ),
            (
                f"/owner/api/v1/memories/{memory_id}/recheck",
                {
                    "expected_record_id": record_id,
                    "expected_revision": 1,
                    "observed_group_token": GROUP_TOKEN,
                    "idempotency_key": "recheck-1",
                    "evidence": {"origin": "OWNER_SUPPLIED"},
                },
                "manual_recheck",
            ),
            (
                f"/owner/api/v1/revoked/{memory_id}/readmit",
                {
                    "revoked_record_id": record_id,
                    "expected_revoked_revision": 1,
                    "observed_group_token": GROUP_TOKEN,
                    "idempotency_key": "readmit-1",
                    "verification": {"verified_by": "OWNER"},
                },
                "readmit",
            ),
        ]

        for path, payload, method_name in cases:
            before = len([call for call in service.calls if call[0] == method_name])
            status, _, body = request(server, "POST", path, headers=headers, json_body=payload)
            assert status == 200, (path, body)
            assert len([call for call in service.calls if call[0] == method_name]) == before + 1
            kwargs = [call for call in service.calls if call[0] == method_name][-1][1]
            assert kwargs["scope"] == "PROJECT"
            assert kwargs["project_id"] == "project-a"
            assert kwargs["observed_group_token"] == GROUP_TOKEN


def test_missing_owner_service_operation_fails_closed_without_adapter_fallback() -> None:
    class NoReverifyService(FakeOwnerService):
        reverify = None  # type: ignore[assignment]

    service = NoReverifyService()
    with running_server(service) as server:
        cookie, csrf = bootstrap(server)
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        status, _, body = request(
            server,
            "POST",
            "/owner/api/v1/memories/11111111-1111-4111-8111-111111111111/reverify",
            headers={"Cookie": cookie, "X-FactLane-CSRF": csrf, "Origin": origin},
            json_body={
                "expected_record_id": "22222222-2222-4222-8222-222222222222",
                "expected_revision": 1,
                "observed_group_token": GROUP_TOKEN,
                "idempotency_key": "reverify-unavailable",
                "verification": {"verified_by": "OWNER"},
            },
        )
        assert status == 503
        result = json.loads(body)
        assert result["error_code"] == "OWNER_CONTROL_UNAVAILABLE"


def test_owner_webui_launcher_accepts_only_contract_scopes_and_has_no_remote_host_argument() -> None:
    parser = _build_launcher_parser()
    for scope in ("GLOBAL_USER", "PROJECT", "WORKFLOW", "TOOL_ENVIRONMENT", "CROSS_PROJECT_WORKFLOW"):
        parsed = parser.parse_args(["--db", "state.db", "--host-id", "owner-webui", "--scope", scope])
        assert parsed.scope == scope
        assert not hasattr(parsed, "host")
    for unsupported in ("GLOBAL", "WORKTREE", "AGENT"):
        with pytest.raises(SystemExit):
            parser.parse_args(["--db", "state.db", "--host-id", "owner-webui", "--scope", unsupported])
