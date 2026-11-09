from __future__ import annotations

import contextlib
import hashlib
import http.server
import json
import os
import re
import socket
import threading
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Route, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
WEBUI = Path(
    os.environ.get(
        "FACTLANE_WEBUI_ROOT",
        str(ROOT / "src" / "factlane" / "webui"),
    )
)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    port = _free_port()
    handler = lambda *args, **kwargs: _QuietHandler(*args, directory=str(WEBUI), **kwargs)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    project_scope = {
        "scope": "PROJECT",
        "project_id": "factlane-usability",
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
    }
    global_scope = {
        "scope": "GLOBAL_USER",
        "project_id": None,
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
    }
    project_memory = "11111111-1111-4111-8111-111111111111"
    project_record = "22222222-2222-4222-8222-222222222222"
    global_memory = "33333333-3333-4333-8333-333333333333"
    global_record = "44444444-4444-4444-8444-444444444444"
    conflict_memory = "55555555-5555-4555-8555-555555555555"
    conflict_record = "66666666-6666-4666-8666-666666666666"
    conflict_memory_2 = "88888888-8888-4888-8888-888888888888"
    conflict_record_2 = "99999999-9999-4999-8999-999999999999"
    conflict_memory_3 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    conflict_record_3 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    batch_memory_1 = "12121212-1212-4121-8121-121212121212"
    batch_record_1 = "13131313-1313-4131-8131-131313131313"
    batch_memory_2 = "14141414-1414-4141-8141-141414141414"
    batch_record_2 = "15151515-1515-4151-8151-151515151515"
    detail_event = "77777777-7777-4777-8777-777777777777"
    token = "a" * 64
    conflict_token = "c" * 64
    conflict_token_2 = "f" * 64
    conflict_token_3 = "9" * 64
    source_timestamp = "2026-11-07T20:30:00Z"
    overview_timestamp = "2026-11-07T20:31:00Z"
    verified_timestamp = "2026-11-07T20:32:00Z"
    event_timestamp = "2026-11-07T20:32:30Z"
    artifact_timestamp = "2026-11-07T20:33:00Z"
    agent_timestamp = "2026-11-07T20:31:00Z"
    promoted = False
    batch_promoted_1 = False
    batch_promoted_2 = False
    batch_partial_emitted = False
    conflict_resolved = False
    reverse_scope_discovery = False
    batch_requests: list[dict[str, object]] = []
    create_candidate_requests: list[dict[str, object]] = []
    manual_recheck_requests: list[dict[str, object]] = []
    resolve_requests: list[dict[str, object]] = []
    resolve_attempts = 0
    housekeeping_requests: list[dict[str, object]] = []
    housekeeping_job_requests: list[dict[str, object]] = []
    housekeeping_cancel_requests: list[dict[str, object]] = []
    housekeeping_status_requests: list[dict[str, object]] = []
    support_preview_requests: list[dict[str, object]] = []
    service_stop_requests: list[dict[str, object]] = []
    activity_requests: list[dict[str, object]] = []
    import_upload_requests: list[dict[str, object]] = []
    import_preview_requests: list[dict[str, object]] = []
    fail_next_service_stop = False
    health_ok = True
    detail_token = token
    source_change_hold = False
    remote_locator = "remote-browser-locator"
    remote_request_phrase = "orange river moon cedar"
    remote_native_phrase = "native approve cobalt seven"
    remote_challenge_id = "remote-browser-challenge"
    remote_native_approved = False
    remote_approved = False
    remote_enroll_requests: list[dict[str, object]] = []
    remote_preview_requests: list[dict[str, object]] = []
    remote_approve_requests: list[dict[str, object]] = []

    def candidate(memory_id: str, record_id: str, scope: dict[str, object], fact: str, source: str):
        return {
            "memory_id": memory_id,
            "record_id": record_id,
            "revision": 1,
            "scope": scope,
            "memory_type": "PROJECT_LEARNED_FACT" if scope["scope"] == "PROJECT" else "USER_FACT",
            "fact": fact,
            "source_provenance": {
                "source_class": "TEST",
                "source_ref": source,
                "source_hash": "b" * 64,
                "review_ref": "browser-usability",
                "extraction_method": "disposable-acceptance",
            },
            "source_timestamp": source_timestamp,
            "freshness_policy": {"kind": "manual"},
            "lifecycle_state": "CANDIDATE",
            "freshness_gate_state": "NONE",
            "verified_by": "UNVERIFIED",
        }

    def project_item():
        item = candidate(
            project_memory,
            project_record,
            project_scope,
            "The project uses compact release notes.",
            "fixture:project",
        )
        if promoted:
            item["lifecycle_state"] = "VALIDATED_CURRENT"
            item["verified_by"] = "OWNER"
            item["freshness_policy"] = {
                "kind": "on_change",
                "recheck_ref": "owner-browser-recheck",
                "source_fingerprint": "b" * 64,
            }
            if source_change_hold:
                item["freshness_gate_state"] = "SOURCE_CHANGE_REVIEW_REQUIRED"
        return item

    def batch_item(memory_id: str, record_id: str, fact: str, source: str, promoted_state: bool):
        item = candidate(memory_id, record_id, project_scope, fact, source)
        if promoted_state:
            item["lifecycle_state"] = "VALIDATED_CURRENT"
            item["verified_by"] = "OWNER"
        return item

    global_item = candidate(
        global_memory,
        global_record,
        global_scope,
        "The owner prefers a concise interface.",
        "fixture:global",
    )
    conflict_item = candidate(
        conflict_memory,
        conflict_record,
        project_scope,
        "Release notes must include migration impact.",
        "fixture:conflict-human",
    )
    conflict_item_2 = candidate(
        conflict_memory_2,
        conflict_record_2,
        project_scope,
        "Documentation links must stay relative.",
        "fixture:conflict-two",
    )
    conflict_item_2["lifecycle_state"] = "VALIDATED_CURRENT"
    conflict_item_2["verified_by"] = "OWNER"
    conflict_item_3 = candidate(
        conflict_memory_3,
        conflict_record_3,
        project_scope,
        "Owner diagnostics stay local.",
        "fixture:conflict-three",
    )
    conflict_item_3["lifecycle_state"] = "VALIDATED_CURRENT"
    conflict_item_3["verified_by"] = "OWNER"

    def conflict_group() -> dict[str, object]:
        members = [
            {
                "memory_id": project_memory,
                "record_id": project_record,
                "revision": 1,
                "lifecycle_state": "VALIDATED_CURRENT",
                "contradiction_state": "UNRESOLVED",
                "freshness_gate_state": "NONE",
                "member_token": "d" * 64,
            },
            {
                "memory_id": conflict_memory,
                "record_id": conflict_record,
                "revision": 1,
                "lifecycle_state": "CANDIDATE",
                "contradiction_state": "UNRESOLVED",
                "freshness_gate_state": "NONE",
                "member_token": "e" * 64,
            },
        ]
        return {
            "schema_version": 2,
            "scope": project_scope,
            "contradiction_key": "subject:release-notes",
            "member_count": 2,
            "observed_group_token": conflict_token,
            "members": members,
        }

    def extra_conflict_group(
        *,
        contradiction_key: str,
        token_value: str,
        memory_id: str,
        record_id: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 2,
            "scope": project_scope,
            "contradiction_key": contradiction_key,
            "member_count": 2,
            "observed_group_token": token_value,
            "members": [
                {
                    "memory_id": project_memory,
                    "record_id": project_record,
                    "revision": 1,
                    "lifecycle_state": "VALIDATED_CURRENT",
                    "contradiction_state": "UNRESOLVED",
                    "freshness_gate_state": "NONE",
                    "member_token": "1" * 64,
                },
                {
                    "memory_id": memory_id,
                    "record_id": record_id,
                    "revision": 1,
                    "lifecycle_state": "VALIDATED_CURRENT",
                    "contradiction_state": "UNRESOLVED",
                    "freshness_gate_state": "NONE",
                    "member_token": "2" * 64,
                },
            ],
        }

    def json_response(route: Route, payload: dict[str, object], status: int = 200) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    def api(route: Route) -> None:
        nonlocal promoted, batch_promoted_1, batch_promoted_2, batch_partial_emitted, conflict_resolved, resolve_attempts
        nonlocal source_change_hold, remote_native_approved, remote_approved
        request = route.request
        path = request.url.split(f"127.0.0.1:{port}", 1)[-1].split("?", 1)[0]
        body = request.post_data_json if request.method == "POST" and request.post_data else {}
        if body is None and request.post_data:
            body = json.loads(request.post_data)
        if path == "/health":
            if not health_ok:
                return json_response(route, {"status": "ERROR"}, status=503)
            return json_response(route, {"status": "OK"})
        if path == "/auth/preauth":
            return json_response(route, {"status": "OK"})
        if path == "/auth/enroll/remote":
            remote_enroll_requests.append(body)
            assert body == {"device_label": "Browser remote laptop"}
            return json_response(route, {
                "status": "PENDING",
                "locator": remote_locator,
                "verification_phrase": remote_request_phrase,
            })
        if path == "/auth/connect/poll":
            assert body == {"locator": remote_locator}, body
            if not remote_approved:
                return json_response(route, {"status": "PENDING"}, status=202)
            return json_response(route, {
                "status": "OK",
                "csrf": "browser-remote-csrf",
                "idle_remaining_seconds": 1800,
                "absolute_remaining_seconds": 28800,
                "setup_required": False,
                "primary_local": False,
                "capabilities": {
                    "database_management": True,
                    "full_restore": True,
                    "permanent_purge": True,
                },
            })
        if path == "/auth/remote/preview":
            remote_preview_requests.append(body)
            if body != {"locator": remote_locator}:
                raise AssertionError(f"remote preview body mismatch: {body!r}")
            preview = json.dumps(
                {
                    "device_label": "Browser remote laptop",
                    "locator": remote_locator,
                    "operation": "APPROVE_REMOTE_ENROLLMENT",
                    "origin": f"http://127.0.0.1:{port}",
                    "source": "198.51.100.27",
                    "verification_phrase": remote_request_phrase,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            return json_response(route, {
                "status": "OK",
                "preview": preview,
                "challenge_id": remote_challenge_id,
                "verification_phrase": remote_native_phrase,
            })
        if path == "/auth/remote/approve":
            remote_approve_requests.append(body)
            assert body == {"locator": remote_locator, "challenge_id": remote_challenge_id}
            if not remote_native_approved:
                return json_response(
                    route,
                    {"status": "ERROR", "code": "SENSITIVE_APPROVAL_REQUIRED"},
                    status=403,
                )
            remote_approved = True
            return json_response(route, {"status": "OK", "primary_local": False})
        if path == "/auth/session/resume":
            if request.headers.get("x-test-remote") == "1":
                return json_response(route, {"status": "ERROR", "code": "SESSION_EXPIRED"}, status=401)
            return json_response(route, {
                "status": "OK",
                "csrf": "browser-usability-csrf",
                "idle_remaining_seconds": 1800,
                "absolute_remaining_seconds": 28800,
                "setup_required": False,
                "capabilities": {
                    "database_management": True,
                    "full_restore": True,
                    "permanent_purge": True,
                },
            })
        if path == "/api/read/overview":
            return json_response(route, {
                "status": "OK",
                "totals": {
                    "logical_memories": {"value": 2, "completeness": "EXACT", "reason": None},
                    "revisions": {"value": 2, "completeness": "PARTIAL", "reason": "TEST_BOUNDED"},
                    "candidates": {"value": 1 if promoted else 2, "completeness": "EXACT", "reason": None},
                    "conflicts": {"value": 1, "completeness": "EXACT", "reason": None},
                },
                "lifecycle_breakdown": {
                    "CANDIDATE": {"value": 1 if promoted else 2, "completeness": "EXACT", "reason": None},
                    "VALIDATED_CURRENT": {"value": 1, "completeness": "EXACT", "reason": None},
                },
                "scope_index": {
                    "total": {"value": 2, "completeness": "EXACT", "reason": None},
                    "page_count": 1,
                    "items": [{
                        "scope": project_scope,
                        "logical_memories": 1,
                        "revisions": 2,
                        "candidates": 0 if promoted else 1,
                        "conflicts": 1,
                    }],
                    "next_cursor": "overview-scope-page-2",
                },
                "project_breakdown": [{
                    "project_id": "factlane-usability",
                    "logical_memories": 1,
                    "revisions": 2,
                    "candidates": 0 if promoted else 1,
                    "conflicts": 1,
                }],
                "snapshot_at": overview_timestamp,
            })
        if path == "/api/read/discovery":
            cursor = body.get("cursor")
            limit = body.get("limit")
            if limit == 100 and not body.get("lifecycle_state"):
                first, second = (project_item(), global_item) if reverse_scope_discovery else (global_item, project_item())
                if cursor == "scope-page-2":
                    return json_response(route, {"status": "OK", "items": [second], "next_cursor": None})
                assert cursor is None
                return json_response(route, {"status": "OK", "items": [first], "next_cursor": "scope-page-2"})
            if body.get("lifecycle_state") == "CANDIDATE" and limit == 100:
                if cursor == "review-page-2":
                    items = [project_item()] if project_item()["lifecycle_state"] == "CANDIDATE" else []
                    if not batch_promoted_1:
                        items.append(batch_item(batch_memory_1, batch_record_1, "Batch candidate alpha.", "fixture:batch-alpha", False))
                    if not batch_promoted_2:
                        items.append(batch_item(batch_memory_2, batch_record_2, "Batch candidate beta.", "fixture:batch-beta", False))
                    return json_response(route, {"status": "OK", "items": items, "next_cursor": None})
                assert cursor is None
                return json_response(route, {"status": "OK", "items": [global_item], "next_cursor": "review-page-2"})
            if limit == 50:
                if cursor == "browse-page-2":
                    return json_response(route, {"status": "OK", "items": [project_item()], "next_cursor": None})
                assert cursor is None
                return json_response(route, {"status": "OK", "items": [global_item], "next_cursor": "browse-page-2"})
            return json_response(route, {"status": "ERROR", "code": "UNEXPECTED_DISCOVERY_PAGE", "body": body}, status=500)
        if path == "/api/read/detail":
            assert body["scope"] == "PROJECT"
            assert body["project_id"] == "factlane-usability"
            detail_items = {
                project_memory: project_item(),
                conflict_memory: conflict_item,
                conflict_memory_2: conflict_item_2,
                conflict_memory_3: conflict_item_3,
                batch_memory_1: batch_item(batch_memory_1, batch_record_1, "Batch candidate alpha.", "fixture:batch-alpha", batch_promoted_1),
                batch_memory_2: batch_item(batch_memory_2, batch_record_2, "Batch candidate beta.", "fixture:batch-beta", batch_promoted_2),
            }
            assert body["memory_id"] in detail_items
            latest = dict(detail_items[body["memory_id"]])
            latest["last_verified_at"] = verified_timestamp
            latest["authority_role"] = "PROJECT_CURRENT" if latest["lifecycle_state"] == "VALIDATED_CURRENT" else "UNRESOLVED"
            history_more = body["memory_id"] == project_memory and body.get("limit") == 100
            historical = dict(latest)
            historical.update({
                "record_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "revision": 0,
                "fact": "Earlier retained release-note wording.",
                "last_verified_at": "2026-11-06T20:32:00Z",
            })
            older_event = {
                "event_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "occurred_at": "2026-11-06T20:32:30Z",
                "action": "REPLACE",
                "prior_lifecycle_state": "VALIDATED_CURRENT",
                "result_lifecycle_state": "SUPERSEDED",
            }
            if body.get("cursor") == "history-page-2":
                records, events, next_cursor, events_next_cursor = [historical], [], None, None
            elif body.get("event_cursor") == "events-page-2":
                records, events, next_cursor, events_next_cursor = [], [older_event], None, None
            else:
                records = [latest]
                events = [{
                    "event_id": detail_event,
                    "occurred_at": event_timestamp,
                    "action": "REVERIFY",
                    "prior_lifecycle_state": "CANDIDATE",
                    "result_lifecycle_state": latest["lifecycle_state"],
                }]
                next_cursor = "history-page-2" if history_more else None
                events_next_cursor = "events-page-2" if history_more else None
            return json_response(route, {
                "status": "OK",
                "scope": project_scope,
                "latest": latest,
                "records": records,
                "events": events,
                "group_cas": {"observed_group_token": detail_token, "member_count": 1},
                "next_cursor": next_cursor,
                "events_next_cursor": events_next_cursor,
            })
        if path == "/api/read/conflicts":
            if body["scope"] == "GLOBAL_USER":
                return json_response(route, {"status": "OK", "items": [], "next_cursor": None})
            assert body["scope"] == "PROJECT"
            assert body["project_id"] == "factlane-usability"
            if body.get("cursor") == "conflict-page-2":
                return json_response(route, {
                    "status": "OK",
                    "items": [{
                        "contradiction_key": "subject:diagnostics",
                        "group_cas": extra_conflict_group(
                            contradiction_key="subject:diagnostics",
                            token_value=conflict_token_3,
                            memory_id=conflict_memory_3,
                            record_id=conflict_record_3,
                        ),
                        "candidate_verification_seeds": {},
                    }],
                    "next_cursor": None,
                })
            assert body.get("cursor") is None
            items = [] if conflict_resolved else [{
                "contradiction_key": "subject:release-notes",
                "group_cas": conflict_group(),
                "candidate_verification_seeds": {conflict_record: {
                    "source_provenance": conflict_item["source_provenance"],
                    "freshness_policy": conflict_item["freshness_policy"],
                    "source_timestamp": conflict_item["source_timestamp"],
                    "verified_by": "OWNER",
                }},
            }]
            items.append({
                "contradiction_key": "subject:documentation",
                "group_cas": extra_conflict_group(
                    contradiction_key="subject:documentation",
                    token_value=conflict_token_2,
                    memory_id=conflict_memory_2,
                    record_id=conflict_record_2,
                ),
                "candidate_verification_seeds": {},
            })
            return json_response(route, {
                "status": "OK",
                "items": items,
                "next_cursor": "conflict-page-2",
            })
        if path == "/api/operations/agent_inventory":
            return json_response(route, {
                "status": "OK",
                "items": [{
                    "agent_id": "CODEX_WINDOWS",
                    "host_id": "owner-test-host",
                    "configured_profile": "nomic-768",
                    "configured_permissions": ["read", "candidate_write"],
                    "runtime_id": "runtime-1",
                    "effective_permissions": ["read", "candidate_write"],
                    "observation_state": "OBSERVED",
                    "last_seen": agent_timestamp,
                    "observation_source": "runtime registry",
                }],
            })
        if path == "/api/operations/settings_snapshot":
            return json_response(route, {
                "status": "OK",
                "allowlist": ["retention_days"],
                "configured": {"retention_days": 30},
                "effective": {"retention_days": 30},
            })
        if path == "/api/service/status":
            assert body == {}
            return json_response(route, {
                "status": "OK",
                "service": {
                    "status": "READY",
                    "saved_endpoint": "https://owner.test:8765",
                    "intent_revision": 4,
                    "stop_intent_active": False,
                },
            })
        if path == "/api/service/stop":
            service_stop_requests.append(body)
            assert set(body) == {"request_id"}
            assert body["request_id"]
            if fail_next_service_stop:
                route.abort()
                return None
            return json_response(route, {
                "status": "STOP_REQUESTED",
                "request_id": body["request_id"],
                "stop_verified": False,
                "service": {
                    "status": "STOPPED_UNTIL_REBOOT",
                    "saved_endpoint": "https://owner.test:8765",
                    "intent_revision": 5,
                    "stop_intent_active": True,
                },
            })
        if path == "/api/operations/housekeeping_preview":
            housekeeping_requests.append(body)
            assert body["scope"] == "PROJECT"
            assert body["project_id"] == "factlane-usability"
            return json_response(route, {
                "status": "PREVIEW",
                "preview_digest": "a" * 64,
                "eligibility": {"selected_upper_bound": 2},
                "blockers": [],
                "capacity": {"observation": "KNOWN", "filesystem_free_bytes": 4096},
                "reclaim_estimate": {"observation": "UNKNOWN", "bytes": None},
            })
        if path == "/api/operations/housekeeping/jobs/begin":
            housekeeping_job_requests.append(body)
            assert body["preview_digest"] == "a" * 64
            return json_response(route, {"status": "ACCEPTED", "job": {
                "job_id": "00000000-0000-4000-8000-000000000321",
                "kind": "HOUSEKEEPING",
                "phase": "RUNNING",
                "progress": {
                    "selected_records": 2,
                    "compacted_records": 0,
                    "cancellation_boundary": "BETWEEN_RECORDS",
                },
                "cancel_requested": False,
            }}, status=202)
        if path == "/api/operations/housekeeping/jobs/cancel":
            housekeeping_cancel_requests.append(body)
            return json_response(route, {"status": "OK", "job": {
                "job_id": body["job_id"],
                "kind": "HOUSEKEEPING",
                "phase": "RUNNING",
                "progress": {
                    "selected_records": 2,
                    "compacted_records": 0,
                    "cancellation_boundary": "BETWEEN_RECORDS",
                },
                "cancel_requested": True,
            }})
        if path == "/api/operations/housekeeping/jobs/status":
            housekeeping_status_requests.append(body)
            return json_response(route, {"status": "OK", "job": {
                "job_id": body["job_id"],
                "kind": "HOUSEKEEPING",
                "phase": "CANCELLED",
                "progress": {
                    "selected_records": 2,
                    "compacted_records": 1,
                    "cancellation_boundary": "BETWEEN_RECORDS",
                },
                "cancel_requested": True,
                "result": {
                    "outcome": "HOUSEKEEPING_CANCELLED",
                    "compacted_records": 1,
                    "filesystem_free_bytes_before": 16777216,
                    "filesystem_free_bytes_after": 16781312,
                    "physical_file_shrink_claimed": False,
                },
            }})
        if path == "/api/operations/support_report_preview":
            support_preview_requests.append(body)
            assert body["scope"] == "PROJECT"
            assert body["project_id"] == "factlane-usability"
            return json_response(route, {
                "status": "OK",
                "schema_version": 1,
                "generated_at": "2026-11-08T15:10:00Z",
                "scope": project_scope,
                "summary": {"memories": 2, "conflicts": 2, "candidates": 0},
                "delivery": {
                    "requires_explicit_download": True,
                    "automatic_upload": False,
                    "uploaded": False,
                    "external_telemetry": False,
                },
            })
        if path == "/api/database/artifacts/list":
            return json_response(route, {"status": "OK", "artifacts": [{
                "artifact_id": "artifact-backup-1",
                "kind": "BACKUP",
                "registered_at": artifact_timestamp,
                "size_bytes": 1234,
                "sha256": "d" * 64,
            }]})
        if path == "/api/database/import/upload":
            import_upload_requests.append(body)
            return json_response(route, {
                "status": "OK",
                "artifact_id": "artifact-import-1",
            })
        if path == "/api/database/import/preview":
            import_preview_requests.append(body)
            return json_response(route, {
                "status": "ACCEPTED",
                "job": {
                    "job_id": "00000000-0000-4000-8000-000000000456",
                    "operation_id": body["operation_id"],
                    "kind": "LOGICAL_IMPORT_PREVIEW",
                    "phase": "SUCCEEDED",
                    "result": {
                        "format": "factlane-logical-v1",
                        "file_digest": "e" * 64,
                        "record_count": 2,
                        "candidate_admission_count": 1,
                        "external_history_count": 1,
                        "missing_lineage": [{
                            "source_record_id": "source-record-2",
                            "missing_parent_source_record_id": "missing-parent",
                        }],
                        "exact_scopes": [project_scope],
                        "source_approval_policy": "EXTERNAL_CLAIM_ONLY",
                        "destination_duplicates": ["source-record-1"],
                        "destination_conflicts": ["source-record-2"],
                        "destination_results": [
                            {
                                "source_record_id": "source-record-1",
                                "scope": project_scope,
                                "classification": "EXACT_DUPLICATE",
                            },
                            {
                                "source_record_id": "source-record-2",
                                "scope": project_scope,
                                "classification": "CONFLICT",
                            },
                        ],
                    },
                },
            }, status=202)
        if path == "/api/database/status":
            return json_response(route, {
                "status": "OK",
                "database_identity": "db-browser-acceptance",
                "storage_contract_version": 3,
                "expected_storage_contract_version": 3,
                "embedding_profile_id": "embeddinggemma-300m-768",
                "factlane_version": "0.1.7",
                "compatibility": "COMPATIBLE",
                "integrity_check": "ok",
                "storage": {
                    "database_file_bytes": 4096,
                    "wal_file_bytes": 512,
                    "shm_file_bytes": 128,
                    "backup_count": 1,
                    "backup_total_bytes": 1234,
                    "artifact_total_bytes": 1234,
                    "filesystem_free_bytes": 8192,
                    "warning": None,
                },
            })
        if path == "/api/database/jobs/list":
            return json_response(route, {
                "status": "OK",
                "items": [{
                    "job_id": "00000000-0000-4000-8000-000000000123",
                    "operation_id": "db-job-browser-1",
                    "kind": "BACKUP_CREATE",
                    "phase": "SUCCEEDED",
                    "updated_at": artifact_timestamp,
                    "artifact_id": "artifact-backup-1",
                    "error": None,
                    "result": {"status": "OK"},
                }],
                "next_cursor": None,
            })
        if path == "/api/operations/activity":
            activity_requests.append(body)
            return json_response(route, {
                "status": "OK",
                "partial": False,
                "items": [{
                    "domain": "LIFECYCLE",
                    "operation_id": "activity-browser-op-1",
                    "correlation_reference": "activity-browser-1",
                    "operation": "REJECT",
                    "actor": "OWNER",
                    "scope": project_scope,
                    "target": {"memory_id": project_memory, "record_id": project_record},
                    "result": {"outcome": "REJECTED"},
                    "time": artifact_timestamp,
                }],
            })
        if path == "/api/write/batch_review":
            batch_requests.append(body)
            assert body["scope"] == "PROJECT"
            assert body["project_id"] == "factlane-usability"
            if len(body["items"]) > 1 or body["items"][0]["memory_id"] in {batch_memory_1, batch_memory_2}:
                item_ids = [item["memory_id"] for item in body["items"]]
                assert all(item["expected_revision"] == 1 for item in body["items"])
                assert all(item["observed_group_token"] == token for item in body["items"])
                assert all(item["verification"]["source_timestamp"].endswith("Z") for item in body["items"])
                if not batch_partial_emitted:
                    assert item_ids == [batch_memory_1, batch_memory_2]
                    batch_promoted_1 = True
                    batch_partial_emitted = True
                    return json_response(route, {
                        "status": "PARTIAL",
                        "counts": {"applied": 1, "conflicted": 0, "failed": 0, "unattempted": 1},
                        "outcomes": [
                            {"memory_id": batch_memory_1, "status": "APPLIED"},
                            {"memory_id": batch_memory_2, "status": "UNATTEMPTED", "error_code": "EMBEDDING_UNAVAILABLE"},
                        ],
                    })
                assert item_ids == [batch_memory_2]
                batch_promoted_2 = True
                return json_response(route, {
                    "status": "OK",
                    "counts": {"applied": 1, "conflicted": 0, "failed": 0, "unattempted": 0},
                    "outcomes": [{"memory_id": batch_memory_2, "status": "APPLIED"}],
                })
            item = body["items"][0]
            assert item["memory_id"] == project_memory
            assert item["expected_record_id"] == project_record
            assert item["expected_revision"] == 1
            assert item["observed_group_token"] == token
            assert item["verification"]["source_timestamp"].endswith("Z")
            assert str(item["verification"]["source_provenance"]["review_ref"]).startswith("owner-panel-review:review-")
            assert "review-required" not in str(item["verification"]["source_provenance"]["review_ref"])
            promoted = True
            return json_response(route, {"status": "OK", "counts": {"applied": 1, "conflicted": 0, "failed": 0}, "outcomes": [{"status": "APPLIED"}]})
        if path == "/api/write/create_candidate":
            create_candidate_requests.append(body)
            assert body["scope"] == "PROJECT"
            assert body["project_id"] == "factlane-usability"
            assert body["memory_type"] == "PROJECT_LEARNED_FACT"
            assert body["source_provenance"]["source_class"] == "OWNER_REQUEST"
            assert body["source_provenance"]["extraction_method"] == "direct-owner-statement"
            expected_hash = hashlib.sha256(str(body["fact"]).encode("utf-8")).hexdigest()
            assert body["source_provenance"]["source_hash"] == expected_hash
            assert str(body["source_provenance"]["source_ref"]).startswith("owner-panel-entry:")
            assert str(body["source_provenance"]["review_ref"]).startswith("owner-panel-review-required:")
            return json_response(route, {"status": "OK", "lifecycle_state": "CANDIDATE"})
        if path == "/api/write/manual_recheck":
            manual_recheck_requests.append(body)
            assert body["scope"] == "PROJECT"
            assert body["project_id"] == "factlane-usability"
            assert body["memory_id"] == project_memory
            assert body["expected_record_id"] == project_record
            assert body["expected_revision"] == 1
            assert body["observed_group_token"] == token
            evidence = body["evidence"]
            assert evidence["origin"] == "OWNER_SUPPLIED"
            assert evidence["claim"] == "OBSERVED_HASH"
            assert evidence["recheck_ref"] == "owner-browser-recheck"
            assert evidence["observed_source_hash"] == hashlib.sha256(
                b"Observed current release-note source."
            ).hexdigest()
            assert evidence["evidence_ref"] is None
            assert evidence["method"] == "OWNER_HASHED_SOURCE"
            assert str(evidence["observed_at"]).endswith("Z")
            source_change_hold = False
            return json_response(route, {"status": "OK"})
        if path == "/api/write/resolve":
            resolve_requests.append(body)
            resolve_attempts += 1
            assert body["scope"] == "PROJECT"
            assert body["project_id"] == "factlane-usability"
            assert body["contradiction_key"] == "subject:release-notes"
            assert body["observed_group_token"] == conflict_token
            assert body["winner_memory_id"] == conflict_memory
            assert body["winner_record_id"] == conflict_record
            assert body["winner_expected_revision"] == 1
            verification = body["verification"]
            provenance = verification["source_provenance"]
            for key in ("source_class", "source_ref", "source_hash", "extraction_method"):
                assert provenance[key] == conflict_item["source_provenance"][key]
            assert provenance["review_ref"] == f"owner-panel-review:{body['idempotency_key']}"
            assert verification["freshness_policy"] == conflict_item["freshness_policy"]
            assert datetime.fromisoformat(str(verification["source_timestamp"]).replace("Z", "+00:00")) == datetime.fromisoformat(
                str(conflict_item["source_timestamp"]).replace("Z", "+00:00")
            )
            assert verification["verified_by"] == "OWNER"
            if resolve_attempts == 1:
                return json_response(route, {"status": "ERROR", "code": "TEST_LOST_RESPONSE", "retryable": True}, status=503)
            assert body["idempotency_key"] == resolve_requests[0]["idempotency_key"]
            conflict_resolved = True
            return json_response(route, {"status": "OK"})
        return json_response(route, {"status": "ERROR", "code": "UNEXPECTED_TEST_ROUTE", "path": path}, status=500)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(locale="en-US", timezone_id="Africa/Cairo", accept_downloads=True)
            page = context.new_page()
            page.route("**/health", api)
            page.route("**/auth/**", api)
            page.route("**/api/**", api)
            page.goto(f"http://127.0.0.1:{port}/#overview")
            page.get_by_role("heading", name="Overview").wait_for()
            page.get_by_text("Project factlane-usability", exact=True).wait_for()
            page.get_by_text("Σ factlane-usability", exact=True).wait_for()
            page.get_by_text("Partial: more memory areas exist.", exact=True).wait_for()
            page.get_by_text("PARTIAL · TEST_BOUNDED", exact=True).wait_for()
            overview_text = page.locator("main").inner_text()
            assert overview_timestamp not in overview_text
            assert re.search(r"Nov\s+7.*2026", overview_text)
            health_ok = False
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            page.get_by_text("Disconnected", exact=True).wait_for()
            page.get_by_text("Displayed data is stale until FactLane reconnects.", exact=True).wait_for()
            health_ok = True
            page.locator("[data-stale-banner]").get_by_role("button", name="Retry", exact=True).click()
            page.locator("[data-connection-state]", has_text="Connected").wait_for()

            page.get_by_role("link", name="Memories").click()
            page.get_by_text("The owner prefers a concise interface.").wait_for()
            assert page.get_by_text(global_memory, exact=True).count() == 0
            page.get_by_role("button", name="Next").click()
            page.get_by_text("The project uses compact release notes.").wait_for()
            assert page.get_by_text(project_memory, exact=True).count() == 0
            assert page.locator('input[name="projectId"]').count() == 0
            assert page.get_by_text("All memories").count() >= 1

            page.get_by_role("button", name="Add candidate").click()
            candidate_form = page.locator("form.candidate-form")
            candidate_form.locator('input[name="fact"]').fill("Owner-entered candidate needs no technical provenance.")
            candidate_form.locator('select[name="scopeKey"]').select_option(label="Project factlane-usability")
            assert candidate_form.locator('input[name="sourceHash"], input[name="reviewRef"]').count() == 0
            assert candidate_form.locator('select[name="evidenceMode"]').input_value() == "OWNER"
            candidate_form.get_by_role("button", name="Create Candidate").click()
            page.get_by_text("Candidate created", exact=True).wait_for()
            assert len(create_candidate_requests) == 1

            page.get_by_role("link", name="Review").click()
            page.get_by_role("heading", name="Review queue").wait_for()
            global_review_card = page.locator(".review-queue-item").filter(has_text="concise interface")
            global_review_card.get_by_label("Select for batch").check()
            page.get_by_role("button", name="Next").click()
            page.get_by_text("Batch candidate alpha.", exact=True).wait_for()
            assert page.locator('.review-queue-item input[type="checkbox"]').evaluate_all(
                "nodes => nodes.length >= 3 && nodes.every(node => node.disabled)"
            )
            page.get_by_role("button", name="Previous").click()
            page.get_by_text("The owner prefers a concise interface.", exact=True).wait_for()
            global_review_card = page.locator(".review-queue-item").filter(has_text="concise interface")
            global_review_card.get_by_label("Select for batch").uncheck()
            page.get_by_role("button", name="Next").click()
            page.locator(".review-queue-item").filter(has_text="compact release notes").get_by_role("button", name="Review").click()
            page.get_by_text("Evidence summary").wait_for()
            review_text = page.locator("main").inner_text()
            assert source_timestamp not in review_text
            assert re.search(r"Nov\s+7.*2026", review_text)
            assert "Approve: keeps this content unchanged → next validated revision." in review_text
            timestamp = page.locator('input[name="sourceTimestampLocal"]')
            assert timestamp.input_value()
            assert page.get_by_text("Africa/Cairo", exact=False).count() >= 1
            assert page.locator('input[name="expectedRecordId"], input[name="observedGroupToken"], input[name="revision"]').count() == 0
            assert page.get_by_text(project_record, exact=True).count() == 0
            assert page.get_by_text(token, exact=True).count() == 0

            detail_token = "0" * 64
            page.get_by_role("button", name="Approve with verification").click()
            page.get_by_text("Conflict group changed; review current state before committing.", exact=True).wait_for()
            assert len(batch_requests) == 0
            detail_token = token
            page.get_by_role("link", name="Review").click()
            page.get_by_role("button", name="Next").click()
            page.locator(".review-queue-item").filter(has_text="compact release notes").get_by_role("button", name="Review").click()
            page.get_by_role("button", name="Approve with verification").click()
            page.get_by_text("Candidate approved", exact=True).wait_for()
            assert len(batch_requests) == 1

            page.get_by_role("button", name="Next").click()
            alpha = page.locator(".review-queue-item").filter(has_text="Batch candidate alpha.")
            beta = page.locator(".review-queue-item").filter(has_text="Batch candidate beta.")
            alpha.get_by_label("Select for batch").check()
            beta.get_by_label("Select for batch").check()
            approve_selected = page.get_by_role("button", name="Approve selected", exact=True)
            assert approve_selected.is_disabled()
            page.get_by_role("button", name="Preview selected", exact=True).click()
            batch_preview_table = page.locator("table").filter(has_text="Batch candidate alpha.")
            batch_preview_table.wait_for()
            batch_preview_text = batch_preview_table.inner_text()
            assert batch_memory_1[:8] in batch_preview_text
            assert batch_memory_2[:8] in batch_preview_text
            assert "Project factlane-usability" in batch_preview_text
            assert approve_selected.is_disabled()
            page.get_by_label("I verified every selected Candidate against the displayed evidence.").check()
            assert approve_selected.is_enabled()
            approve_selected.click()
            page.get_by_text("Batch review complete", exact=True).wait_for()
            page.get_by_text("UNATTEMPTED", exact=True).wait_for()
            assert len(batch_requests) == 2
            partial_beta_key = next(
                item["idempotency_key"] for item in batch_requests[-1]["items"] if item["memory_id"] == batch_memory_2
            )
            page.get_by_role("button", name="Preview selected", exact=True).click()
            page.get_by_label("I verified every selected Candidate against the displayed evidence.").check()
            page.get_by_role("button", name="Approve selected", exact=True).click()
            page.get_by_text("Batch review complete", exact=True).wait_for()
            assert len(batch_requests) == 3
            assert batch_requests[-1]["items"][0]["memory_id"] == batch_memory_2
            assert batch_requests[-1]["items"][0]["idempotency_key"] == partial_beta_key

            page.get_by_role("link", name="Memories").click()
            page.get_by_role("button", name="Next").click()
            card = page.locator(".memory-card").filter(has_text="compact release notes")
            card.get_by_text("VALIDATED_CURRENT", exact=True).wait_for()
            assert page.locator('input[name="projectId"]').count() == 0
            card.get_by_role("button", name="Memory detail").click()
            page.get_by_role("heading", name="Memory detail").wait_for()
            detail_text = page.locator("main").inner_text()
            for raw_value in (
                project_memory,
                project_record,
                detail_event,
                source_timestamp,
                verified_timestamp,
                event_timestamp,
            ):
                assert raw_value not in detail_text
            assert "Revision 1" in detail_text
            assert "REVERIFY" in detail_text
            assert re.search(r"Nov\s+7.*2026", detail_text)
            page.get_by_role("button", name="Load more revisions").click()
            prior_text = page.locator(".history-item > p", has_text="Earlier retained release-note wording.")
            prior_text.first.wait_for()
            assert prior_text.count() >= 1
            assert page.get_by_text("Revision 0", exact=True).count() == 1
            page.get_by_text("Compare with previous revision", exact=True).wait_for()
            page.get_by_role("button", name="Load more events").click()
            page.get_by_text("REPLACE", exact=True).wait_for()
            assert page.get_by_role("button", name="Load more revisions").count() == 0
            assert page.get_by_role("button", name="Load more events").count() == 0

            source_change_hold = True
            page.get_by_role("link", name="Memories").click()
            page.get_by_role("button", name="Next").click()
            held_card = page.locator(".memory-card").filter(has_text="compact release notes")
            held_card.get_by_role("button", name="Memory detail").click()
            page.get_by_role("heading", name="Memory detail").wait_for()
            page.get_by_text(
                "Ordinary reverify is unavailable; use Manual recheck.",
                exact=True,
            ).wait_for()
            assert page.get_by_role("button", name="Reverify", exact=True).count() == 0
            assert page.get_by_role("button", name="Manual recheck", exact=True).count() == 1

            readmit_preview = page.evaluate(
                """async () => {
                  const views = await import("./assets/views.js");
                  const rendered = views.renderDetailState({
                    memory: {
                      lifecycle_state: "REVOKED",
                      scope: "PROJECT",
                      fact: "Revoked browser fixture",
                      freshness_policy: {kind: "manual"}
                    },
                    lifecycleDraft: {action: "READMIT"},
                    records: [],
                    events: [],
                    groupCas: {member_count: 1}
                  });
                  return rendered.textContent;
                }"""
            )
            assert "VALIDATED_CURRENT / CANDIDATE / existing equivalent" in readmit_preview

            page.get_by_role("button", name="Manual recheck").click()
            page.locator('select[name="claim"]').select_option("OBSERVED_HASH")
            page.locator('input[name="evidenceText"]').fill("Observed current release-note source.")
            assert page.locator('input[name="recheckRef"], input[name="observedSourceHash"], input[name="evidenceRef"]').count() == 0
            page.get_by_role("button", name="Submit action").click()
            page.get_by_text("MANUAL_RECHECK complete", exact=True).wait_for()
            assert len(manual_recheck_requests) == 1

            page.get_by_role("link", name="Conflicts").click()
            page.get_by_role("heading", name="Conflicts").wait_for()
            page.locator('select[name="scopeKey"]').select_option(label="Project factlane-usability")
            page.get_by_text("The project uses compact release notes.", exact=True).wait_for()
            page.get_by_text("Release notes must include migration impact.", exact=True).wait_for()
            group_select = page.locator('select[name="conflictGroup"]')
            assert group_select.locator("option").count() == 2
            group_select.select_option("subject:documentation")
            page.get_by_text("Documentation links must stay relative.", exact=True).wait_for()
            page.get_by_role("button", name="Next").click()
            page.get_by_text("Owner diagnostics stay local.", exact=True).wait_for()
            page.get_by_role("button", name="Previous").click()
            page.get_by_text("Release notes must include migration impact.", exact=True).wait_for()
            reverse_scope_discovery = True
            page.get_by_role("link", name="Review").click()
            page.get_by_role("heading", name="Review queue").wait_for()
            page.get_by_role("link", name="Conflicts").click()
            page.get_by_text("Release notes must include migration impact.", exact=True).wait_for()
            for raw_value in (project_memory, project_record, conflict_memory, conflict_record, conflict_token, "subject:release-notes"):
                assert page.get_by_text(raw_value, exact=True).count() == 0
            winner = page.locator('select[name="winner"]')
            assert "Release notes must include migration impact." in winner.locator("option").nth(2).inner_text()
            assert not winner.locator("option").nth(2).is_disabled()
            winner.select_option(conflict_record)
            source_time = page.locator('input[name="sourceTimestampLocal"]')
            source_time.wait_for(state="visible")
            assert source_time.input_value()
            assert page.get_by_role("button", name="Confirm resolution").is_disabled()
            page.get_by_text("Advanced evidence details", exact=True).click()
            assert page.locator('input[name="sourceRef"]').input_value() == "fixture:conflict-human"
            assert page.locator('input[name="sourceHash"]').input_value() == "b" * 64
            page.locator('input[name="verifiedNow"]').check()
            assert page.get_by_role("button", name="Confirm resolution").is_enabled()
            page.evaluate("""
                () => {
                  const nativeFetch = window.fetch.bind(window);
                  let delayed = false;
                  window.fetch = async (...args) => {
                    const body = String(args[1]?.body || "");
                    if (!delayed && body.includes("conflicts-refresh")) {
                      delayed = true;
                      await new Promise((resolve) => setTimeout(resolve, 250));
                    }
                    return nativeFetch(...args);
                  };
                }
            """)
            page.get_by_role("button", name="Confirm resolution").click()
            page.locator('select[name="scopeKey"]').select_option(label="Personal memory")
            page.wait_for_timeout(350)
            assert len(resolve_requests) == 0
            page.locator('select[name="scopeKey"]').select_option(label="Project factlane-usability")
            page.get_by_text("Release notes must include migration impact.", exact=True).wait_for()
            winner = page.locator('select[name="winner"]')
            winner.select_option(conflict_record)
            page.locator('input[name="sourceTimestampLocal"]').wait_for(state="visible")
            assert len(resolve_requests) == 0
            page.locator('input[name="verifiedNow"]').check()
            page.get_by_role("button", name="Confirm resolution").click()
            page.get_by_role("button", name="Retry").wait_for()
            assert len(resolve_requests) == 1
            page.get_by_role("button", name="Retry").click()
            page.get_by_text("Conflict resolved", exact=True).wait_for()
            assert len(resolve_requests) == 2
            assert resolve_requests[0]["idempotency_key"] == resolve_requests[1]["idempotency_key"]

            page.get_by_role("link", name="Database").click()
            page.get_by_role("heading", name="Database and backups").wait_for()
            database_text = page.locator("main").inner_text()
            assert "Review database identity, health, storage" not in database_text
            assert artifact_timestamp not in database_text
            assert "artifact-backup-1" in database_text
            assert "d" * 64 in database_text
            assert re.search(r"Nov\s+7.*2026", database_text)
            housekeeping_form = page.locator("form.panel").filter(has_text="Preview housekeeping")
            housekeeping_form.locator('select[name="scopeKey"]').select_option(label="Project factlane-usability")
            page.get_by_role("link", name="Memories").click()
            page.get_by_text("The owner prefers a concise interface.").wait_for()
            page.get_by_role("link", name="Database").click()
            housekeeping_form = page.locator("form.panel").filter(has_text="Preview housekeeping")
            housekeeping_form.get_by_role("button", name="Preview housekeeping").click()
            page.get_by_text("Housekeeping preview", exact=True).wait_for()
            page.get_by_text("READY", exact=True).wait_for()
            page.get_by_text("4096", exact=True).wait_for()
            page.get_by_text("Estimate", exact=True).wait_for()
            page.get_by_text("UNKNOWN", exact=True).wait_for()
            assert len(housekeeping_requests) == 1
            housekeeping_form.get_by_role("button", name="Run housekeeping").click()
            page.get_by_text("RUNNING · 0/2 · BETWEEN_RECORDS", exact=True).wait_for()
            housekeeping_form.get_by_role("button", name="Cancel request", exact=True).click()
            page.get_by_text(
                "CANCELLED · HOUSEKEEPING_CANCELLED · 1/2 · BETWEEN_RECORDS",
                exact=True,
            ).wait_for()
            page.get_by_text("Free bytes Before", exact=True).wait_for()
            page.get_by_text("16777216", exact=True).wait_for()
            page.get_by_text("Free bytes After", exact=True).wait_for()
            page.get_by_text("16781312", exact=True).wait_for()
            assert "SQLite" not in housekeeping_form.inner_text()
            assert len(housekeeping_job_requests) == 1
            assert len(housekeeping_cancel_requests) == 1
            assert len(housekeeping_status_requests) >= 1
            import_form = page.locator("form.panel").filter(has_text="Import preview")
            import_form.locator('input[type="file"]').set_input_files({
                "name": "factlane-import.json",
                "mimeType": "application/json",
                "buffer": b'{"format":"factlane-logical-v1","records":[]}',
            })
            import_form.get_by_role("button", name="Import preview").click()
            page.get_by_text("EXTERNAL_CLAIM_ONLY", exact=True).wait_for()
            page.get_by_text("EXACT_DUPLICATE", exact=True).wait_for()
            page.get_by_text("CONFLICT", exact=True).wait_for()
            page.get_by_text("source-record-1", exact=True).wait_for()
            page.get_by_text("source-record-2", exact=True).wait_for()
            assert len(import_upload_requests) == 1
            assert len(import_preview_requests) == 1
            page.get_by_role("button", name="Restore preview").click()
            page.get_by_text("Restore replaces active database state.", exact=False).wait_for()
            assert page.get_by_text("native confirmation", exact=False).count() >= 1

            page.get_by_role("link", name="Agents").click()
            page.get_by_role("heading", name="Agents and connections").wait_for()
            page.get_by_text("Configured and observed runtime state are separate.").wait_for()
            agents_text = page.locator("main").inner_text()
            assert agent_timestamp not in agents_text
            assert re.search(r"Nov\s+7.*2026", agents_text)

            page.get_by_role("link", name="Activity").click()
            page.get_by_role("heading", name="Activity", level=1).wait_for()
            page.get_by_text("activity-browser-1", exact=True).wait_for()
            page.get_by_text("outcome: REJECTED", exact=True).wait_for()
            assert artifact_timestamp not in page.locator("main").inner_text()
            activity_form = page.locator("form.panel").filter(has_text="Search")
            activity_form.locator('select[name="domain"]').select_option("LIFECYCLE")
            activity_form.locator('input[name="operation"]').fill("REJECT")
            activity_form.get_by_role("button", name="Search").click()
            assert len(activity_requests) >= 2
            assert activity_requests[-1]["domain"] == "LIFECYCLE"
            assert activity_requests[-1]["operation"] == "REJECT"
            page.locator('a[data-nav-key="settings"]').click()
            page.get_by_role("heading", name="Session and settings").wait_for()
            page.get_by_role("heading", name="Diagnostics").wait_for()
            page.get_by_text("Choose one exact memory area", exact=False).wait_for()
            page.get_by_text("retention_days", exact=True).wait_for()
            page.get_by_role("heading", name="Remote access").wait_for()

            remote_page = context.new_page()
            remote_page.set_extra_http_headers({"X-Test-Remote": "1"})
            remote_page.route("**/health", api)
            remote_page.route("**/auth/**", api)
            remote_page.route("**/api/**", api)
            remote_page.goto(f"http://127.0.0.1:{port}/#connect")
            remote_label = remote_page.locator('input[name="deviceLabel"]')
            remote_label.wait_for()
            remote_label.fill("Browser remote laptop")
            remote_page.get_by_role("button", name="Request remote access", exact=True).click()
            for word in remote_request_phrase.split():
                remote_page.get_by_text(word, exact=True).wait_for()
            assert len(remote_enroll_requests) == 1

            remote_panel = page.locator("section.panel").filter(has_text="Remote access")
            remote_panel.locator('input[name="remoteLocator"]').fill(remote_locator)
            remote_panel.get_by_role("button", name="Review request", exact=True).click()
            remote_panel.get_by_text("Browser remote laptop", exact=True).wait_for()
            remote_panel.get_by_text(remote_locator, exact=True).wait_for()
            remote_panel.get_by_text(remote_request_phrase, exact=True).wait_for()
            remote_panel.get_by_text(remote_native_phrase, exact=True).wait_for()
            assert len(remote_preview_requests) == 1

            approve_remote = remote_panel.get_by_role("button", name="Approve device", exact=True)
            assert approve_remote.is_enabled()
            approve_remote.click()
            page.get_by_text("SENSITIVE_APPROVAL_REQUIRED", exact=True).wait_for()
            assert len(remote_approve_requests) == 1
            remote_native_approved = True
            approve_remote.click()
            page.get_by_text("Remote device approved", exact=True).wait_for()
            assert len(remote_approve_requests) == 2
            assert remote_approve_requests[0] == remote_approve_requests[1]
            remote_page.get_by_role("heading", name="Overview").wait_for(timeout=6000)
            remote_page.close()

            settings_text = page.locator("main").inner_text()
            assert "Expires at" in settings_text
            assert "Ends at" in settings_text
            page.get_by_role("heading", name="Service").wait_for()
            page.get_by_text("READY", exact=True).wait_for()
            page.get_by_text("Starts normally", exact=True).wait_for()
            stop_button = page.get_by_role("button", name="Stop", exact=True)
            assert stop_button.is_disabled()
            page.get_by_label("I understand Stop ends this Owner Panel until I start it again or reboot.").check()
            assert stop_button.is_enabled()
            stop_button.click()
            page.get_by_text("Stop requested", exact=True).wait_for()
            page.get_by_text("STOPPED_UNTIL_REBOOT", exact=True).wait_for()
            page.get_by_text("Stopped until reboot", exact=True).wait_for()
            page.get_by_text("This page cannot verify shutdown", exact=False).wait_for()
            assert len(service_stop_requests) == 1
            first_stop_id = service_stop_requests[0]["request_id"]
            fail_next_service_stop = True
            page.get_by_label("I understand Stop ends this Owner Panel until I start it again or reboot.").check()
            page.get_by_role("button", name="Stop", exact=True).click()
            page.get_by_text("Stop outcome unverified", exact=True).wait_for()
            page.get_by_text("Use the FactLane launcher to check status or start it again.", exact=True).wait_for()
            assert len(service_stop_requests) == 2
            lost_stop_id = service_stop_requests[-1]["request_id"]
            assert lost_stop_id != first_stop_id
            fail_next_service_stop = False
            page.locator("[data-status-banner]").get_by_role("button", name="Retry", exact=True).click()
            page.get_by_text("Stop requested", exact=True).wait_for()
            assert len(service_stop_requests) == 3
            assert service_stop_requests[-1]["request_id"] == lost_stop_id
            assert re.search(r"\b1\d{9}\.\d+\b", settings_text) is None
            assert "2026" in settings_text
            assert page.get_by_text("Unavailable through current HTTP routes", exact=False).count() == 0
            assert page.get_by_text("Activity listing is unavailable", exact=False).count() == 0
            page.locator('select[name="scopeKey"]').select_option(label="Project factlane-usability")
            page.get_by_role("button", name="Preview diagnostics").click()
            page.get_by_text("Redacted support preview ready for review or download.", exact=True).wait_for()
            page.get_by_text("Local download only; no automatic upload or external telemetry.", exact=True).wait_for()
            with page.expect_download() as download_info:
                page.get_by_role("button", name="Download diagnostics").click()
            download = download_info.value
            assert download.suggested_filename == "factlane-support-preview.json"
            downloaded = json.loads(Path(download.path()).read_text(encoding="utf-8"))
            assert downloaded["delivery"] == {
                "requires_explicit_download": True,
                "automatic_upload": False,
                "uploaded": False,
                "external_telemetry": False,
            }
            assert len(support_preview_requests) == 1
            page.evaluate("window.location.hash = 'activity'")
            page.wait_for_url(re.compile(r"#activity$"))
            page.get_by_role("heading", name="Activity", level=1).wait_for()
            page.get_by_role("link", name="Settings").click()
            page.wait_for_url(re.compile(r"#settings$"))
            page.get_by_role("heading", name="Session and settings").wait_for()
            page.get_by_role("button", name="العربية").click()
            page.get_by_role("heading", name="الجلسة والإعدادات").wait_for()
            arabic_settings = page.locator("main").inner_text()
            assert "تنتهي عند" in arabic_settings
            assert "تنتهي نهائيًا عند" in arabic_settings
            assert re.search(r"\b1\d{9}\.\d+\b", arabic_settings) is None
            assert page.locator("html").get_attribute("dir") == "rtl"
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
