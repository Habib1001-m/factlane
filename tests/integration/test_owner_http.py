from __future__ import annotations

import asyncio
import hashlib
import http.client
import inspect
import json
import socket
import stat
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from http.cookies import SimpleCookie

import pytest

if sys.platform != "linux":
    pytest.skip(
        "managed Owner WebUI HTTP transport qualification is Linux-only",
        allow_module_level=True,
    )

import factlane.owner_http as owner_http_module
from factlane.owner_auth import NativeConfirmationRequest, OwnerAuthState
from factlane.contract import AdapterError
from factlane.owner_destructive import OwnerDestructiveError, PurgePreview, PurgeSuccess, PurgeTargetIdentity
from factlane.owner_http import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_QUEUE_SIZE,
    MAX_BODY_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    OwnerHTTPServer,
)
from factlane.recovery import RecoveryScope


OWNER_UID = 1000
ORIGIN = "https://owner.test"
HOST = "owner.test"


@dataclass
class FakeNativeConfirmation:
    approved: bool = True

    def __post_init__(self) -> None:
        self.requests: list[NativeConfirmationRequest] = []

    def confirm(self, request: NativeConfirmationRequest) -> bool:
        self.requests.append(request)
        return self.approved


class RecordingOverview:
    def __init__(self) -> None:
        self.overview_calls: list[dict[str, object]] = []
        self.search_calls: list[dict[str, object]] = []

    async def overview(self, **kwargs):
        self.overview_calls.append(kwargs)
        return {"status": "OK", "totals": {}}

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return {"status": "OK", "scope": {"scope": kwargs["scope"]}, "items": []}


class RecordingControl:
    def __init__(self) -> None:
        self.inventory_calls: list[dict[str, object]] = []
        self.detail_calls: list[dict[str, object]] = []
        self.conflicts_calls: list[dict[str, object]] = []
        self.calls: dict[str, list[dict[str, object]]] = {
            name: []
            for name in (
                "create_candidate",
                "batch_review",
                "revoke",
                "reject",
                "reverify",
                "replace",
                "resolve",
                "manual_recheck",
                "readmit",
            )
        }

    async def inventory(self, **kwargs):
        self.inventory_calls.append(kwargs)
        return {"status": "OK", "scope": {"scope": kwargs["scope"]}, "items": []}

    async def detail(self, **kwargs):
        self.detail_calls.append(kwargs)
        return {"status": "OK", "scope": {"scope": kwargs["scope"]}, "memory_id": kwargs["memory_id"]}

    async def conflicts(self, **kwargs):
        self.conflicts_calls.append(kwargs)
        return {"status": "OK", "scope": {"scope": kwargs["scope"]}, "items": []}

    async def create_candidate(self, **kwargs):
        self.calls["create_candidate"].append(kwargs)
        return {"status": "OK", "operation": "create_candidate"}

    async def batch_review(self, **kwargs):
        self.calls["batch_review"].append(kwargs)
        return {"status": "OK", "operation": "batch_review"}

    async def revoke(self, **kwargs):
        self.calls["revoke"].append(kwargs)
        return {"status": "OK", "operation": "revoke"}

    async def reject(self, **kwargs):
        self.calls["reject"].append(kwargs)
        return {"status": "OK", "operation": "reject"}

    async def reverify(self, **kwargs):
        self.calls["reverify"].append(kwargs)
        return {"status": "OK", "operation": "reverify"}

    async def replace(self, **kwargs):
        self.calls["replace"].append(kwargs)
        return {"status": "OK", "operation": "replace"}

    async def resolve(self, **kwargs):
        self.calls["resolve"].append(kwargs)
        return {"status": "OK", "operation": "resolve"}

    async def manual_recheck(self, **kwargs):
        self.calls["manual_recheck"].append(kwargs)
        return {"status": "OK", "operation": "manual_recheck"}

    async def readmit(self, **kwargs):
        self.calls["readmit"].append(kwargs)
        return {"status": "OK", "operation": "readmit"}


class RecordingOperations:
    def __init__(self) -> None:
        self.calls: dict[str, list[dict[str, object]]] = {
            name: []
            for name in (
                "housekeeping_preview",
                "run_housekeeping",
                "agent_inventory",
                "settings_snapshot",
                "settings_preview",
                "settings_apply",
                "support_report_preview",
            )
        }

    async def housekeeping_preview(self, **kwargs):
        self.calls["housekeeping_preview"].append(kwargs)
        return {"status": "PREVIEW"}

    async def run_housekeeping(self, **kwargs):
        self.calls["run_housekeeping"].append(kwargs)
        return {"status": "OK"}

    async def agent_inventory(self, **kwargs):
        self.calls["agent_inventory"].append(kwargs)
        return {"status": "OK", "items": []}

    async def settings_snapshot(self, **kwargs):
        self.calls["settings_snapshot"].append(kwargs)
        return {"status": "OK"}

    async def settings_preview(self, changes, **kwargs):
        self.calls["settings_preview"].append({"changes": changes, **kwargs})
        return {"status": "PREVIEW", "changes": changes}

    async def settings_apply(self, changes, **kwargs):
        self.calls["settings_apply"].append({"changes": changes, **kwargs})
        return {"status": "APPLIED", "changes": changes}

    async def support_report_preview(self, **kwargs):
        self.calls["support_report_preview"].append(kwargs)
        return {"status": "PREVIEW", "delivery": {"uploaded": False}}


class RecordingDestructive:
    def __init__(self) -> None:
        self.preview_calls: list[dict[str, object]] = []
        self.execute_calls: list[dict[str, object]] = []
        self.fail_recovery_once = False

    async def preview(self, *, operation_id, targets):
        self.preview_calls.append({"operation_id": operation_id, "targets": tuple(targets)})
        public_targets = tuple(targets)
        preview_digest = "a" * 64
        return PurgePreview(
            operation_id=operation_id,
            targets=public_targets,
            backup_retention_warning="Backups may retain purged content.",
            preview_digest=preview_digest,
            typed_confirmation=f"PURGE {preview_digest}",
            authorization_preview=(
                f'{{"kind":"PURGE","operation_id":"{operation_id}","preview_digest":"{preview_digest}"}}'
            ),
            _target_state_digest="b" * 64,
        )

    async def execute(
        self,
        preview,
        *,
        typed_confirmation,
        grant_secret,
        drain_deadline,
        verify_fresh_grant=None,
    ):
        if not callable(verify_fresh_grant):
            raise AssertionError("request-bound purge verifier is required")
        await verify_fresh_grant(
            grant_secret=grant_secret,
            preview=preview.authorization_preview,
        )
        self.execute_calls.append(
            {
                "preview": preview,
                "typed_confirmation": typed_confirmation,
                "grant_secret": grant_secret,
                "drain_deadline": drain_deadline,
            }
        )
        if self.fail_recovery_once:
            self.fail_recovery_once = False
            raise OwnerDestructiveError(
                "PURGE_RECOVERY_REQUIRED",
                "synthetic recovery detail",
                recovery_required=True,
            )
        return PurgeSuccess(
            operation_id=preview.operation_id,
            preview_digest=preview.preview_digest,
            status="S1_LOCAL_FACTLANE_PURGE_VERIFIED",
            receipt_path="/private/receipt",
            state_path="/private/state",
            target_count=len(preview.targets),
            ready_instance_ids=("runtime-1",),
            resume_failures={},
        )


class DurableRecordingDestructive(RecordingDestructive):
    def __init__(self, journal_path) -> None:
        super().__init__()
        self.journal_path = journal_path
        self.recoverable_preview_calls: list[str] = []

    def recoverable_preview(self, operation_id: str):
        self.recoverable_preview_calls.append(operation_id)
        if not self.journal_path.exists():
            return None
        payload = json.loads(self.journal_path.read_text(encoding="utf-8"))
        if payload.get("operation_id") != operation_id:
            raise OwnerDestructiveError("PURGE_RECOVERY_STATE_MISMATCH", "synthetic durable journal mismatch")
        targets = tuple(
            PurgeTargetIdentity(
                scope=RecoveryScope(
                    item["scope"],
                    project_id=item["project_id"],
                    worktree_id=item["worktree_id"],
                    workflow_id=item["workflow_id"],
                    agent_id=item["agent_id"],
                ),
                memory_id=item["memory_id"],
                record_id=item["record_id"],
                revision=item["revision"],
            )
            for item in payload["targets"]
        )
        return PurgePreview(
            operation_id=payload["operation_id"],
            targets=targets,
            backup_retention_warning=payload["backup_retention_warning"],
            preview_digest=payload["preview_digest"],
            typed_confirmation=payload["typed_confirmation"],
            authorization_preview=payload["authorization_preview"],
            _target_state_digest=payload["target_state_digest"],
        )

    async def execute(self, preview, **kwargs):
        try:
            return await super().execute(preview, **kwargs)
        except OwnerDestructiveError as exc:
            if exc.code != "PURGE_RECOVERY_REQUIRED":
                raise
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_text(
                json.dumps(
                    {
                        "operation_id": preview.operation_id,
                        "targets": list(preview.targets),
                        "backup_retention_warning": preview.backup_retention_warning,
                        "preview_digest": preview.preview_digest,
                        "typed_confirmation": preview.typed_confirmation,
                        "authorization_preview": preview.authorization_preview,
                        "target_state_digest": preview._target_state_digest,
                        "logical_deletions": 1,
                    },
                    default=lambda value: value.to_mapping(),
                ),
                encoding="utf-8",
            )
            raise


class RecordingUploadSink:
    def __init__(self, database, *, kind, declared_size, expected_sha256):
        self.database = database
        self.kind = kind
        self.declared_size = declared_size
        self.expected_sha256 = expected_sha256
        self.upload_id = str(uuid.uuid4())
        self.data = bytearray()
        self.aborted = False
        self.sealed = False

    def write(self, data):
        if self.aborted:
            raise AssertionError("aborted upload sink must not receive writes")
        raw = bytes(data)
        if len(self.data) + len(raw) > self.declared_size:
            raise AdapterError("UPLOAD_SIZE_MISMATCH", "too many bytes")
        self.data.extend(raw)
        return len(raw)

    def seal(self):
        if len(self.data) != self.declared_size:
            raise AdapterError("UPLOAD_INCOMPLETE", "incomplete upload")
        digest = hashlib.sha256(self.data).hexdigest()
        if self.expected_sha256 is not None and digest != self.expected_sha256.lower():
            raise AdapterError("UPLOAD_CHECKSUM_MISMATCH", "checksum mismatch")
        self.sealed = True
        artifact_id = str(uuid.uuid4())
        artifact = {
            "artifact_id": artifact_id,
            "kind": self.kind,
            "size_bytes": len(self.data),
            "sha256": digest,
            "relative_path": f"private/{artifact_id}",
        }
        self.database.artifacts[artifact_id] = artifact
        return dict(artifact)

    def abort(self):
        self.aborted = True


class RecordingDownloadHandle:
    def __init__(self, payload: bytes, *, artifact_id: str, filename: str | None = None):
        self.payload = payload
        self.position = 0
        self.kind = "LOGICAL_IMPORT"
        self.size_bytes = len(payload)
        self.sha256 = hashlib.sha256(payload).hexdigest()
        self.filename = filename or f"factlane-logical-import-{artifact_id}.json"
        self.closed = False

    def read(self, maximum_bytes=1024 * 1024):
        if self.closed:
            raise AssertionError("closed download handle must not be read")
        chunk = self.payload[self.position : self.position + maximum_bytes]
        self.position += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class RecordingDatabase:
    def __init__(self):
        self.calls: dict[str, list[dict[str, object]]] = {}
        self.sinks: list[RecordingUploadSink] = []
        self.artifacts: dict[str, dict[str, object]] = {}
        self.jobs: dict[str, dict[str, object]] = {}
        self.download_payload = b"verified-download-body"
        self.download_open_calls: list[str] = []
        self.download_handles: list[RecordingDownloadHandle] = []
        self.restore_authorization_preview = "restore authorization preview"
        self.invoke_restore_authorize = False
        self.restore_authorize_callbacks: list[object] = []
        self.restore_authorize_errors: list[BaseException] = []

    def _record(self, name, **kwargs):
        self.calls.setdefault(name, []).append(dict(kwargs))

    def _job(self, kind, *, operation_id=None, artifact_id=None):
        job_id = str(uuid.uuid4())
        job = {
            "job_id": job_id,
            "kind": kind,
            "operation_id": operation_id or str(uuid.uuid4()),
            "phase": "QUEUED",
        }
        if artifact_id is not None:
            job["artifact_id"] = artifact_id
        self.jobs[job_id] = job
        return dict(job)

    def list_artifacts(self):
        self._record("list_artifacts")
        return [self.artifact_metadata(artifact_id) for artifact_id in sorted(self.artifacts)]

    def artifact_metadata(self, artifact_id):
        artifact = self.artifacts[artifact_id]
        return {key: value for key, value in artifact.items() if not key.endswith("path")}

    def preview_artifact_delete(self, artifact_id):
        self._record("preview_artifact_delete", artifact_id=artifact_id)
        return {"artifact_id": artifact_id, "sha256": "a" * 64, "preview_digest": "b" * 64}

    def commit_artifact_delete(self, artifact_id, preview_digest):
        self._record("commit_artifact_delete", artifact_id=artifact_id, preview_digest=preview_digest)
        if preview_digest != "b" * 64:
            raise AdapterError("ARTIFACT_DELETE_PREVIEW_MISMATCH", "stale preview")
        return {"artifact_id": artifact_id, "deleted": True}

    def begin_backup_job(self, *, operation_id=None):
        self._record("begin_backup_job", operation_id=operation_id)
        return self._job("BACKUP_CREATE", operation_id=operation_id)

    def begin_logical_export_job(self, *, selection, operation_id=None):
        self._record(
            "begin_logical_export_job",
            selection=dict(selection),
            operation_id=operation_id,
        )
        return self._job("LOGICAL_EXPORT", operation_id=operation_id)

    def begin_logical_import_preview_job(self, artifact_id, *, operation_id=None):
        self._record("begin_logical_import_preview_job", artifact_id=artifact_id, operation_id=operation_id)
        return self._job("LOGICAL_IMPORT_PREVIEW", operation_id=operation_id, artifact_id=artifact_id)

    def begin_logical_import_commit_job(self, artifact_id, *, operation_id):
        self._record("begin_logical_import_commit_job", artifact_id=artifact_id, operation_id=operation_id)
        return self._job("LOGICAL_IMPORT_COMMIT", operation_id=operation_id, artifact_id=artifact_id)

    def begin_restore_preview_job(self, artifact_id, *, operation_id=None, trusted_origin_attested=False):
        self._record(
            "begin_restore_preview_job",
            artifact_id=artifact_id,
            operation_id=operation_id,
            trusted_origin_attested=trusted_origin_attested,
        )
        return self._job("RESTORE_PREVIEW", operation_id=operation_id, artifact_id=artifact_id)

    def begin_restore_commit_job(self, artifact_id, *, operation_id, state_restoration_acknowledged):
        self._record(
            "begin_restore_commit_job",
            artifact_id=artifact_id,
            operation_id=operation_id,
            state_restoration_acknowledged=state_restoration_acknowledged,
        )
        if state_restoration_acknowledged is not True:
            raise AdapterError("RESTORE_ACK_REQUIRED", "restore acknowledgement required")
        return self._job("RESTORE_COMMIT", operation_id=operation_id, artifact_id=artifact_id)

    def begin_logical_import_upload(self, *, declared_size, expected_sha256=None):
        self._record("begin_logical_import_upload", declared_size=declared_size, expected_sha256=expected_sha256)
        sink = RecordingUploadSink(
            self,
            kind="LOGICAL_IMPORT",
            declared_size=declared_size,
            expected_sha256=expected_sha256,
        )
        self.sinks.append(sink)
        return sink

    def begin_restore_upload(self, *, declared_size, expected_sha256=None):
        self._record("begin_restore_upload", declared_size=declared_size, expected_sha256=expected_sha256)
        sink = RecordingUploadSink(
            self,
            kind="RESTORE_UPLOAD",
            declared_size=declared_size,
            expected_sha256=expected_sha256,
        )
        self.sinks.append(sink)
        return sink

    def job_status(self, job_id):
        self._record("job_status", job_id=job_id)
        return dict(self.jobs[job_id])

    async def run_job(self, job_id):
        self._record("run_job", job_id=job_id)
        await asyncio.sleep(0)
        self.jobs[job_id]["phase"] = "SUCCEEDED"
        return dict(self.jobs[job_id])

    async def run_restore_job(self, job_id, *, authorize):
        self._record("run_restore_job", job_id=job_id)
        self.restore_authorize_callbacks.append(authorize)
        await asyncio.sleep(0)
        if self.invoke_restore_authorize:
            try:
                outcome = authorize(self.restore_authorization_preview)
                if inspect.isawaitable(outcome):
                    await outcome
            except BaseException as exc:
                self.restore_authorize_errors.append(exc)
                self.jobs[job_id]["phase"] = "FAILED"
                return dict(self.jobs[job_id])
        self.jobs[job_id]["phase"] = "SUCCEEDED"
        return dict(self.jobs[job_id])

    def open_download(self, artifact_id):
        self.download_open_calls.append(artifact_id)
        handle = RecordingDownloadHandle(self.download_payload, artifact_id=artifact_id)
        self.download_handles.append(handle)
        return handle


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__(HOST, timeout=2)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


def response_cookies(response: http.client.HTTPResponse) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, value in response.getheaders():
        if name.lower() != "set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(value)
        for key, morsel in parsed.items():
            values[key] = morsel.value
    return values


def set_cookie_headers(response: http.client.HTTPResponse) -> list[str]:
    return [value for name, value in response.getheaders() if name.lower() == "set-cookie"]


def request(
    server: OwnerHTTPServer,
    method: str,
    path: str,
    *,
    body: dict[str, object] | bytes | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> tuple[http.client.HTTPResponse, bytes]:
    request_headers = {"Host": HOST, **(headers or {})}
    raw_body: bytes | None
    if isinstance(body, dict):
        raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    else:
        raw_body = body
    if raw_body is not None:
        request_headers["Content-Length"] = str(len(raw_body))
    if cookies:
        request_headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
    conn = UnixHTTPConnection(str(server.socket_path))
    conn.request(method, path, body=raw_body, headers=request_headers)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response, payload


def json_payload(payload: bytes) -> dict[str, object]:
    return json.loads(payload.decode("utf-8"))


def raw_request(socket_path: str, payload: bytes) -> bytes:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(socket_path)
    try:
        try:
            client.sendall(payload)
        except BrokenPipeError:
            pass
        chunks: list[bytes] = []
        while True:
            try:
                chunk = client.recv(8192)
            except ConnectionResetError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        client.close()


@pytest.fixture
def running_server(tmp_path):
    confirmation = FakeNativeConfirmation()
    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=confirmation)
    overview = RecordingOverview()
    control = RecordingControl()
    operations = RecordingOperations()
    server = OwnerHTTPServer(
        socket_path=tmp_path / "private" / "owner-http.sock",
        origin=ORIGIN,
        auth_state=auth,
        overview_service=overview,
        control_service=control,
        operations_service=operations,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, auth, overview, control, operations, confirmation
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def running_destructive_server(tmp_path):
    confirmation = FakeNativeConfirmation()
    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=confirmation)
    destructive = RecordingDestructive()
    server = OwnerHTTPServer(
        socket_path=tmp_path / "private" / "owner-http.sock",
        origin=ORIGIN,
        auth_state=auth,
        destructive_service=destructive,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, auth, destructive
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def running_database_server(tmp_path):
    confirmation = FakeNativeConfirmation()
    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=confirmation)
    database = RecordingDatabase()
    server = OwnerHTTPServer(
        socket_path=tmp_path / "private" / "owner-http.sock",
        origin=ORIGIN,
        auth_state=auth,
        database_service=database,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, auth, database
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def purge_target_body() -> dict[str, object]:
    return {
        "scope": "PROJECT",
        "project_id": "project-a",
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "memory_id": "memory-1",
        "record_id": "record-1",
        "revision": 1,
    }


def issue_preauth(server: OwnerHTTPServer) -> str:
    response, payload = request(
        server,
        "POST",
        "/auth/preauth",
        body={},
        headers={"Origin": ORIGIN},
    )
    assert response.status == 200, payload
    cookies = response_cookies(response)
    return cookies["__Host-factlane-preauth"]


def establish_primary_session(server: OwnerHTTPServer, auth: OwnerAuthState):
    pending = auth.begin_enrollment(caller_uid=OWNER_UID, source="local-launcher", device_label="Primary browser")
    preauth = issue_preauth(server)
    response, payload = request(
        server,
        "POST",
        "/auth/connect/claim",
        body={"locator": pending.locator},
        headers={"Origin": ORIGIN},
        cookies={"__Host-factlane-preauth": preauth},
    )
    assert response.status == 200, payload
    auth.approve_pending(locator=pending.locator, caller_uid=OWNER_UID)
    response, payload = request(
        server,
        "POST",
        "/auth/connect/poll",
        body={"locator": pending.locator},
        headers={"Origin": ORIGIN},
        cookies={"__Host-factlane-preauth": preauth},
    )
    assert response.status == 200, payload
    result = json_payload(payload)
    cookies = response_cookies(response)
    return cookies["__Host-factlane-session"], str(result["csrf"])


def mint_sensitive_grant(
    server: OwnerHTTPServer,
    auth: OwnerAuthState,
    *,
    session: str,
    csrf: str,
    preview: str,
) -> str:
    headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
    cookies = {"__Host-factlane-session": session}
    response, payload = request(
        server,
        "POST",
        "/auth/sensitive/challenge",
        body={"preview": preview},
        headers=headers,
        cookies=cookies,
    )
    assert response.status == 200, payload
    challenge_id = str(json_payload(payload)["challenge_id"])
    auth.approve_sensitive_challenge(challenge_id=challenge_id, caller_uid=OWNER_UID)
    response, payload = request(
        server,
        "POST",
        "/auth/sensitive/grant",
        body={"challenge_id": challenge_id},
        headers=headers,
        cookies=cookies,
    )
    assert response.status == 200, payload
    return str(json_payload(payload)["grant"])


def test_server_is_af_unix_only_with_private_socket_permissions_and_safe_cleanup(tmp_path) -> None:
    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=FakeNativeConfirmation())
    socket_path = tmp_path / "private" / "owner-http.sock"
    sibling = socket_path.parent / "keep-me"
    server = OwnerHTTPServer(
        socket_path=socket_path,
        origin=ORIGIN,
        auth_state=auth,
        overview_service=RecordingOverview(),
        control_service=RecordingControl(),
    )
    sibling.write_text("keep", encoding="utf-8")
    try:
        assert server.socket.family == socket.AF_UNIX
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        assert not hasattr(server, "server_port")
    finally:
        server.server_close()
    assert not socket_path.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


def test_resource_defaults_are_16_workers_32_queue_and_10_second_request_timeout(tmp_path) -> None:
    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=FakeNativeConfirmation())
    server = OwnerHTTPServer(
        socket_path=tmp_path / "private" / "owner-http.sock",
        origin=ORIGIN,
        auth_state=auth,
        overview_service=RecordingOverview(),
        control_service=RecordingControl(),
    )
    try:
        assert DEFAULT_MAX_CONCURRENCY == 16
        assert DEFAULT_QUEUE_SIZE == 32
        assert REQUEST_TIMEOUT_SECONDS == 10.0
        assert server.request_queue_size == 32
        assert server.request_timeout_seconds == 10.0
    finally:
        server.server_close()


def test_incomplete_body_times_out_stably_and_releases_the_only_worker(tmp_path, capsys) -> None:
    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=FakeNativeConfirmation())
    server = OwnerHTTPServer(
        socket_path=tmp_path / "private" / "owner-http.sock",
        origin=ORIGIN,
        auth_state=auth,
        overview_service=RecordingOverview(),
        control_service=RecordingControl(),
        max_concurrency=1,
        request_timeout_seconds=0.1,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(server.socket_path))
    try:
        client.sendall(
            (
                "POST /auth/preauth HTTP/1.1\r\n"
                f"Host: {HOST}\r\n"
                f"Origin: {ORIGIN}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 10\r\n"
                "Connection: close\r\n\r\n"
                "{"
            ).encode("ascii")
        )
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
        response = b"".join(chunks)
        assert response.startswith(b"HTTP/1.1 408 Request Timeout")
        assert b'"code":"REQUEST_TIMEOUT"' in response

        deadline = time.monotonic() + 1.0
        while True:
            followup = raw_request(
                str(server.socket_path),
                f"GET /health HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n".encode("ascii"),
            )
            if followup.startswith(b"HTTP/1.1 200 OK"):
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_concurrent_service_calls_share_one_server_async_loop_and_close_stops_it(tmp_path) -> None:
    class ContendedControl(RecordingControl):
        def __init__(self) -> None:
            super().__init__()
            self.lock = asyncio.Lock()
            self.loop_ids: list[int] = []
            self.entered = 0

        async def create_candidate(self, **kwargs):
            self.loop_ids.append(id(asyncio.get_running_loop()))
            async with self.lock:
                self.entered += 1
                await asyncio.sleep(0.05)
                return {"status": "OK", "scope": {"scope": kwargs["scope"]}}

    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=FakeNativeConfirmation())
    control = ContendedControl()
    server = OwnerHTTPServer(
        socket_path=tmp_path / "private" / "owner-http.sock",
        origin=ORIGIN,
        auth_state=auth,
        overview_service=RecordingOverview(),
        control_service=control,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        session, csrf = establish_primary_session(server, auth)
        headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
        cookie = {"__Host-factlane-session": session}
        statuses: list[int] = []

        def invoke() -> None:
            response, _ = request(
                server,
                "POST",
                "/api/write/create_candidate",
                body={
                    "scope": "PROJECT",
                    "project_id": "project-a",
                    "fact": "Concurrent candidate",
                    "memory_type": "FACT",
                    "source_provenance": {"source_type": "OWNER"},
                    "freshness_policy": {"kind": "STATIC"},
                    "idempotency_key": f"candidate-{threading.get_ident()}",
                },
                headers=headers,
                cookies=cookie,
            )
            statuses.append(response.status)

        first = threading.Thread(target=invoke)
        second = threading.Thread(target=invoke)
        first.start()
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)
        assert not first.is_alive() and not second.is_alive()
        assert sorted(statuses) == [200, 200]
        assert control.entered == 2
        assert len(control.loop_ids) == 2
        assert len(set(control.loop_ids)) == 1
        assert server.service_loop_thread_alive is True
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
    assert server.service_loop_thread_alive is False


def test_service_coroutine_wait_is_bounded_and_timed_out_task_is_cancelled(tmp_path) -> None:
    class SlowControl(RecordingControl):
        def __init__(self) -> None:
            super().__init__()
            self.cancelled = threading.Event()

        async def create_candidate(self, **kwargs):
            del kwargs
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            return {"status": "OK"}

    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=FakeNativeConfirmation())
    control = SlowControl()
    server = OwnerHTTPServer(
        socket_path=tmp_path / "private" / "owner-http.sock",
        origin=ORIGIN,
        auth_state=auth,
        overview_service=RecordingOverview(),
        control_service=control,
        request_timeout_seconds=0.1,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        session, csrf = establish_primary_session(server, auth)
        response, payload = request(
            server,
            "POST",
            "/api/write/create_candidate",
            body={
                "scope": "PROJECT",
                "project_id": "project-a",
                "fact": "Slow candidate",
                "memory_type": "FACT",
                "source_provenance": {"source_type": "OWNER"},
                "freshness_policy": {"kind": "STATIC"},
                "idempotency_key": "slow-candidate",
            },
            headers={"Origin": ORIGIN, "X-FactLane-CSRF": csrf},
            cookies={"__Host-factlane-session": session},
        )
        assert response.status == 504
        assert json_payload(payload)["code"] == "SERVICE_TIMEOUT"
        assert control.cancelled.wait(timeout=1)
        assert server.service_loop_thread_alive is True
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
    assert server.service_loop_thread_alive is False


def test_composition_create_use_and_cleanup_share_server_service_loop(tmp_path) -> None:
    class LoopBoundControl(RecordingControl):
        def __init__(self) -> None:
            super().__init__()
            self.lock = asyncio.Lock()
            self.created_loop = id(asyncio.get_running_loop())
            self.used_loops: list[int] = []
            self.closed_loop: int | None = None

        async def create_candidate(self, **kwargs):
            self.used_loops.append(id(asyncio.get_running_loop()))
            async with self.lock:
                await asyncio.sleep(0.03)
                return {"status": "OK", "scope": {"scope": kwargs["scope"]}}

        async def close(self) -> None:
            self.closed_loop = id(asyncio.get_running_loop())

    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=FakeNativeConfirmation())
    server = OwnerHTTPServer(
        socket_path=tmp_path / "private" / "owner-http.sock",
        origin=ORIGIN,
        auth_state=auth,
    )

    async def create_control() -> LoopBoundControl:
        return LoopBoundControl()

    control = server.run_service_coroutine(create_control)
    database = RecordingDatabase()
    server.configure_services(
        overview_service=RecordingOverview(),
        control_service=control,
        operations_service=RecordingOperations(),
        database_service=database,
    )
    assert server.database_service is database
    server.register_async_cleanup(control.close)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        session, csrf = establish_primary_session(server, auth)
        headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
        cookie = {"__Host-factlane-session": session}
        statuses: list[int] = []

        def invoke(index: int) -> None:
            response, _ = request(
                server,
                "POST",
                "/api/write/create_candidate",
                body={
                    "scope": "PROJECT",
                    "project_id": "project-a",
                    "fact": f"candidate-{index}",
                    "memory_type": "FACT",
                    "source_provenance": {"source_type": "OWNER"},
                    "freshness_policy": {"kind": "STATIC"},
                    "idempotency_key": f"candidate-{index}",
                },
                headers=headers,
                cookies=cookie,
            )
            statuses.append(response.status)

        first = threading.Thread(target=invoke, args=(1,))
        second = threading.Thread(target=invoke, args=(2,))
        first.start()
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)
        assert sorted(statuses) == [200, 200]
        assert set(control.used_loops) == {control.created_loop}
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert control.closed_loop == control.created_loop
    assert server.service_loop_thread_alive is False
    assert not server.socket_path.exists()


def test_async_cleanup_failure_still_closes_socket_and_loop_and_surfaces_safely(tmp_path) -> None:
    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=FakeNativeConfirmation())
    server = OwnerHTTPServer(
        socket_path=tmp_path / "private" / "owner-http.sock",
        origin=ORIGIN,
        auth_state=auth,
    )
    cleanup_loop: list[int] = []

    async def failing_cleanup() -> None:
        cleanup_loop.append(id(asyncio.get_running_loop()))
        raise RuntimeError("private cleanup detail must not escape")

    created_loop = server.run_service_coroutine(lambda: id(asyncio.get_running_loop()))
    server.register_async_cleanup(failing_cleanup)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    server.shutdown()
    with pytest.raises(RuntimeError, match="Owner HTTP async cleanup failed") as exc_info:
        server.server_close()
    server_thread.join(timeout=2)

    assert "private cleanup detail" not in str(exc_info.value)
    assert cleanup_loop == [created_loop]
    assert server.service_loop_thread_alive is False
    assert not server.socket_path.exists()


def test_concurrency_bound_rejects_excess_connections_without_spawning_unbounded_workers(tmp_path) -> None:
    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=FakeNativeConfirmation())
    server = OwnerHTTPServer(
        socket_path=tmp_path / "private" / "owner-http.sock",
        origin=ORIGIN,
        auth_state=auth,
        overview_service=RecordingOverview(),
        control_service=RecordingControl(),
        max_concurrency=1,
        queue_size=2,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    blocker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    blocker.settimeout(2)
    blocker.connect(str(server.socket_path))
    try:
        blocker.sendall(f"GET /health HTTP/1.1\r\nHost: {HOST}\r\n".encode("ascii"))
        response = raw_request(
            str(server.socket_path),
            f"GET /health HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n".encode("ascii"),
        )
        assert response.startswith(b"HTTP/1.1 503 Service Unavailable")
        assert b'"code":"SERVER_BUSY"' in response
    finally:
        blocker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_assets_health_and_csp_are_served_without_bootstrap_fallback(running_server) -> None:
    server, _, _, _, _, _ = running_server
    response, payload = request(server, "GET", "/")
    assert response.status == 200
    assert b"FactLane Owner" in payload
    assert "default-src 'self'" in response.getheader("Content-Security-Policy")
    assert "connect-src 'self'" in response.getheader("Content-Security-Policy")

    response, payload = request(server, "GET", "/assets/app.js")
    assert response.status == 200
    assert b"stateRenderers" in payload
    assert response.getheader("X-Content-Type-Options") == "nosniff"

    response, payload = request(server, "GET", "/health")
    assert response.status == 200
    assert json_payload(payload) == {"status": "OK"}

    response, _ = request(server, "POST", "/owner/bootstrap", body={}, headers={"Origin": ORIGIN})
    assert response.status == 404
    response, _ = request(server, "GET", "/assets/../owner_auth.py")
    assert response.status == 404


def test_exact_host_and_origin_are_enforced_and_forwarded_headers_are_ignored(running_server) -> None:
    server, _, _, _, _, _ = running_server
    response, _ = request(
        server,
        "GET",
        "/health",
        headers={"Host": "forged.test", "X-Forwarded-Host": HOST},
    )
    assert response.status == 403

    response, _ = request(
        server,
        "POST",
        "/auth/preauth",
        body={},
        headers={"Origin": "https://forged.test", "X-Forwarded-Origin": ORIGIN},
    )
    assert response.status == 403

    response, _ = request(
        server,
        "POST",
        "/auth/preauth",
        body={},
        headers={"Origin": ORIGIN, "X-Forwarded-Host": "forged.test", "X-Forwarded-For": "203.0.113.99"},
    )
    assert response.status == 200


def test_local_connect_poll_sets_secure_host_only_auth_cookies_and_separate_csrf(running_server) -> None:
    server, auth, _, _, _, confirmation = running_server
    pending = auth.begin_enrollment(caller_uid=OWNER_UID, source="local-launcher", device_label="Primary browser")
    preauth = issue_preauth(server)

    response, payload = request(
        server,
        "POST",
        "/auth/connect/claim",
        body={"locator": pending.locator},
        headers={"Origin": ORIGIN},
        cookies={"__Host-factlane-preauth": preauth},
    )
    assert response.status == 200
    assert json_payload(payload)["verification_phrase"] == pending.verification_phrase

    response, _ = request(
        server,
        "POST",
        "/auth/connect/poll",
        body={"locator": pending.locator},
        headers={"Origin": ORIGIN},
        cookies={"__Host-factlane-preauth": preauth},
    )
    assert response.status == 202

    auth.approve_pending(locator=pending.locator, caller_uid=OWNER_UID)
    assert confirmation.requests[-1].kind == "ENROLLMENT"
    response, payload = request(
        server,
        "POST",
        "/auth/connect/poll",
        body={"locator": pending.locator},
        headers={"Origin": ORIGIN},
        cookies={"__Host-factlane-preauth": preauth},
    )
    assert response.status == 200
    result = json_payload(payload)
    assert result["primary_local"] is True
    assert isinstance(result["csrf"], str) and result["csrf"]
    cookie_headers = set_cookie_headers(response)
    session_header = next(value for value in cookie_headers if value.startswith("__Host-factlane-session="))
    assert "Secure" in session_header
    assert "HttpOnly" in session_header
    assert "SameSite=Strict" in session_header
    assert "Path=/" in session_header
    assert "Domain=" not in session_header
    assert result["csrf"] not in session_header
    session_secret = response_cookies(response)["__Host-factlane-session"]
    assert session_secret.encode("utf-8") not in payload


def test_remote_enrollment_and_primary_approval_flow_use_browser_binding_and_sensitive_grant(running_server) -> None:
    server, auth, _, _, _, _ = running_server
    primary_session, primary_csrf = establish_primary_session(server, auth)

    remote_preauth = issue_preauth(server)
    response, payload = request(
        server,
        "POST",
        "/auth/enroll/remote",
        body={"device_label": "Remote laptop"},
        headers={"Origin": ORIGIN},
        cookies={"__Host-factlane-preauth": remote_preauth},
    )
    assert response.status == 200, payload
    remote = json_payload(payload)
    locator = str(remote["locator"])

    auth_headers = {"Origin": ORIGIN, "X-FactLane-CSRF": primary_csrf}
    primary_cookie = {"__Host-factlane-session": primary_session}
    response, payload = request(
        server,
        "POST",
        "/auth/remote/preview",
        body={"locator": locator},
        headers=auth_headers,
        cookies=primary_cookie,
    )
    assert response.status == 200, payload
    preview = str(json_payload(payload)["preview"])
    assert "Remote laptop" in preview
    assert locator in preview

    response, payload = request(
        server,
        "POST",
        "/auth/sensitive/challenge",
        body={"preview": preview},
        headers=auth_headers,
        cookies=primary_cookie,
    )
    assert response.status == 200, payload
    challenge_id = str(json_payload(payload)["challenge_id"])
    auth.approve_sensitive_challenge(challenge_id=challenge_id, caller_uid=OWNER_UID)

    response, payload = request(
        server,
        "POST",
        "/auth/sensitive/grant",
        body={"challenge_id": challenge_id},
        headers=auth_headers,
        cookies=primary_cookie,
    )
    assert response.status == 200, payload
    grant_secret = str(json_payload(payload)["grant"])
    response, payload = request(
        server,
        "POST",
        "/auth/remote/approve",
        body={"locator": locator, "grant": grant_secret},
        headers=auth_headers,
        cookies=primary_cookie,
    )
    assert response.status == 200, payload

    response, payload = request(
        server,
        "POST",
        "/auth/connect/poll",
        body={"locator": locator},
        headers={"Origin": ORIGIN},
        cookies={"__Host-factlane-preauth": remote_preauth},
    )
    assert response.status == 200, payload
    assert json_payload(payload)["primary_local"] is False


def test_authenticated_read_routes_require_csrf_and_exact_scope_before_service_call(running_server) -> None:
    server, auth, overview, control, _, _ = running_server
    session, csrf = establish_primary_session(server, auth)
    cookie = {"__Host-factlane-session": session}

    response, _ = request(
        server,
        "POST",
        "/api/read/search",
        body={"query": "fact", "scope": "PROJECT", "project_id": "project-a"},
        headers={"Origin": ORIGIN},
        cookies=cookie,
    )
    assert response.status == 403
    assert overview.search_calls == []

    response, _ = request(
        server,
        "POST",
        "/api/read/search",
        body={"query": "fact", "scope": "PROJECT", "project_id": "project-a"},
        headers={"Origin": ORIGIN, "X-FactLane-CSRF": "forged"},
        cookies=cookie,
    )
    assert response.status == 403
    assert overview.search_calls == []

    headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
    response, _ = request(
        server,
        "POST",
        "/api/read/search",
        body={
            "query": "fact",
            "scope": "PROJECT",
            "project_id": "project-a",
            "workflow_id": "forbidden-workflow",
        },
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 403
    assert overview.search_calls == []

    response, payload = request(
        server,
        "POST",
        "/api/read/search",
        body={"query": "fact", "scope": "PROJECT", "project_id": "project-a", "limit": 25},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 200, payload
    assert overview.search_calls[-1]["scope"] == "PROJECT"
    assert overview.search_calls[-1]["project_id"] == "project-a"
    assert overview.search_calls[-1]["limit"] == 25

    response, payload = request(
        server,
        "POST",
        "/api/read/inventory",
        body={"scope": "PROJECT", "project_id": "project-a", "lifecycle_state": "CANDIDATE"},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 200, payload
    assert control.inventory_calls[-1]["scope"] == "PROJECT"
    assert control.inventory_calls[-1]["project_id"] == "project-a"
    assert control.inventory_calls[-1]["lifecycle_state"] == "CANDIDATE"

    response, payload = request(
        server,
        "POST",
        "/api/read/detail",
        body={"scope": "PROJECT", "project_id": "project-a", "memory_id": "memory-1"},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 200, payload
    assert control.detail_calls[-1]["scope"] == "PROJECT"
    assert control.detail_calls[-1]["project_id"] == "project-a"
    assert control.detail_calls[-1]["memory_id"] == "memory-1"


def test_body_limit_and_unsupported_mutation_route_fail_closed(running_server) -> None:
    server, _, _, _, _, _ = running_server
    response_bytes = raw_request(
        str(server.socket_path),
        (
            "POST /auth/preauth HTTP/1.1\r\n"
            f"Host: {HOST}\r\n"
            f"Origin: {ORIGIN}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {MAX_BODY_BYTES + 1}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii"),
    )
    assert response_bytes.startswith(b"HTTP/1.1 413 ")
    assert b'"code":"BODY_TOO_LARGE"' in response_bytes

    response, _ = request(
        server,
        "POST",
        "/api/write/purge",
        body={},
        headers={"Origin": ORIGIN},
    )
    assert response.status == 404


def test_protected_overview_conflicts_origin_scope_csrf_and_get_mutation_boundaries(running_server) -> None:
    server, auth, overview, control, _, _ = running_server
    session, csrf = establish_primary_session(server, auth)
    cookie = {"__Host-factlane-session": session}
    headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}

    forged = raw_request(
        str(server.socket_path),
        (
            "POST /api/read/overview HTTP/1.1\r\n"
            f"Host: {HOST}\r\n"
            "Origin: https://forged.test\r\n"
            f"X-FactLane-CSRF: {csrf}\r\n"
            f"Cookie: __Host-factlane-session={session}\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii"),
    )
    assert forged.startswith(b"HTTP/1.1 403 ")
    assert overview.overview_calls == []

    response, _ = request(
        server,
        "POST",
        "/api/read/overview",
        body={"limit": 25},
        headers={"Origin": ORIGIN, "X-FactLane-CSRF": "forged"},
        cookies=cookie,
    )
    assert response.status == 403
    assert overview.overview_calls == []

    response, payload = request(
        server,
        "POST",
        "/api/read/overview",
        body={"limit": 25, "cursor": "next", "request_id": "overview-1"},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 200, payload
    assert overview.overview_calls == [{"limit": 25, "cursor": "next", "request_id": "overview-1"}]

    response, _ = request(
        server,
        "POST",
        "/api/read/conflicts",
        body={
            "scope": "PROJECT",
            "project_id": "project-a",
            "workflow_id": "forged-workflow",
        },
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 403
    assert control.conflicts_calls == []

    response, payload = request(
        server,
        "POST",
        "/api/read/conflicts",
        body={"scope": "PROJECT", "project_id": "project-a", "limit": 12, "request_id": "conflicts-1"},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 200, payload
    assert control.conflicts_calls[-1] == {
        "scope": "PROJECT",
        "project_id": "project-a",
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "limit": 12,
        "cursor": None,
        "request_id": "conflicts-1",
    }

    response, _ = request(
        server,
        "GET",
        "/api/write/revoke",
        headers={"Origin": ORIGIN, "X-FactLane-CSRF": csrf},
        cookies=cookie,
    )
    assert response.status in {404, 405}
    assert control.calls["revoke"] == []


def test_create_candidate_and_batch_review_forward_exact_kwargs_and_enforce_bounds(running_server) -> None:
    server, auth, _, control, _, _ = running_server
    session, csrf = establish_primary_session(server, auth)
    headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
    cookie = {"__Host-factlane-session": session}

    create_body = {
        "scope": "PROJECT",
        "project_id": "project-a",
        "fact": "User prefers compact diffs",
        "memory_type": "PREFERENCE",
        "source_provenance": {"source_type": "OWNER", "source_ref": "owner-ui"},
        "freshness_policy": {"kind": "STATIC"},
        "idempotency_key": "candidate-1",
        "source_timestamp": "2026-11-05T20:00:00Z",
        "confidence": 0.75,
        "tags": ["ui"],
        "subject": "review style",
        "request_id": "create-1",
    }
    response, payload = request(
        server,
        "POST",
        "/api/write/create_candidate",
        body=create_body,
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 200, payload
    assert control.calls["create_candidate"][-1] == {
        **create_body,
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
    }

    response, _ = request(
        server,
        "POST",
        "/api/write/create_candidate",
        body={**create_body, "unexpected": True},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 400
    assert len(control.calls["create_candidate"]) == 1

    items = [{"action": "REJECT", "memory_id": "m1"}]
    response, payload = request(
        server,
        "POST",
        "/api/write/batch_review",
        body={"scope": "PROJECT", "project_id": "project-a", "items": items, "request_id": "batch-1"},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 200, payload
    assert control.calls["batch_review"][-1] == {
        "scope": "PROJECT",
        "project_id": "project-a",
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
        "items": items,
        "request_id": "batch-1",
    }

    response, _ = request(
        server,
        "POST",
        "/api/write/batch_review",
        body={"scope": "PROJECT", "project_id": "project-a", "items": [{} for _ in range(51)]},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 400
    assert len(control.calls["batch_review"]) == 1


def test_governed_lifecycle_routes_are_explicit_and_resolve_forwards_exact_kwargs(running_server) -> None:
    server, auth, _, control, _, _ = running_server
    session, csrf = establish_primary_session(server, auth)
    headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
    cookie = {"__Host-factlane-session": session}
    scope = {"scope": "PROJECT", "project_id": "project-a"}
    common = {
        **scope,
        "memory_id": "memory-1",
        "expected_record_id": "record-1",
        "expected_revision": 2,
        "observed_group_token": "group-token",
        "idempotency_key": "idem-1",
        "request_id": "request-1",
    }

    route_bodies = {
        "revoke": {**common, "reason_code": "OWNER_REVOKED"},
        "reject": {**common, "reason_code": "OWNER_REJECTED"},
        "reverify": {**common, "verification": {"verified_by": "OWNER"}},
        "replace": {**common, "replacement": {"fact": "replacement"}},
        "manual_recheck": {**common, "evidence": {"outcome": "UNCHANGED"}},
        "readmit": {
            **scope,
            "memory_id": "memory-1",
            "revoked_record_id": "record-2",
            "expected_revoked_revision": 3,
            "observed_group_token": "group-token",
            "idempotency_key": "readmit-1",
            "verification": {"verified_by": "OWNER"},
            "request_id": "readmit-request",
        },
    }
    for operation, body in route_bodies.items():
        response, payload = request(
            server,
            "POST",
            f"/api/write/{operation}",
            body=body,
            headers=headers,
            cookies=cookie,
        )
        assert response.status == 200, (operation, payload)
        assert len(control.calls[operation]) == 1

    resolve_body = {
        **scope,
        "contradiction_key": "contradiction-1",
        "observed_group_token": "group-token",
        "winner_memory_id": "memory-1",
        "winner_record_id": "record-1",
        "winner_expected_revision": 2,
        "idempotency_key": "resolve-1",
        "reason_code": "OWNER_RESOLVED",
        "verification": {"verified_by": "OWNER"},
        "request_id": "resolve-request",
    }
    response, payload = request(
        server,
        "POST",
        "/api/write/resolve",
        body=resolve_body,
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 200, payload
    assert control.calls["resolve"][-1] == {
        **resolve_body,
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
    }

    before = len(control.calls["resolve"])
    response, _ = request(
        server,
        "POST",
        "/api/write/resolve",
        body={**resolve_body, "workflow_id": "forged-workflow"},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 403
    assert len(control.calls["resolve"]) == before


def test_operations_routes_forward_exact_kwargs_and_exclude_destructive_database_surfaces(running_server) -> None:
    server, auth, _, _, operations, _ = running_server
    session, csrf = establish_primary_session(server, auth)
    headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
    cookie = {"__Host-factlane-session": session}
    scoped = {
        "scope": "PROJECT",
        "project_id": "project-a",
        "max_records": 25,
        "required_free_bytes": 4096,
        "request_id": "ops-1",
    }

    for operation in ("housekeeping_preview", "run_housekeeping", "support_report_preview"):
        response, payload = request(
            server,
            "POST",
            f"/api/operations/{operation}",
            body=scoped,
            headers=headers,
            cookies=cookie,
        )
        assert response.status == 200, (operation, payload)
        assert operations.calls[operation][-1] == {
            **scoped,
            "worktree_id": None,
            "workflow_id": None,
            "agent_id": None,
        }

    for operation in ("agent_inventory", "settings_snapshot"):
        response, payload = request(
            server,
            "POST",
            f"/api/operations/{operation}",
            body={"request_id": f"{operation}-1"},
            headers=headers,
            cookies=cookie,
        )
        assert response.status == 200, payload
        assert operations.calls[operation][-1] == {"request_id": f"{operation}-1"}

    changes = {"request_timeout_seconds": 60}
    for operation in ("settings_preview", "settings_apply"):
        response, payload = request(
            server,
            "POST",
            f"/api/operations/{operation}",
            body={"changes": changes, "request_id": f"{operation}-1"},
            headers=headers,
            cookies=cookie,
        )
        assert response.status == 200, payload
        assert operations.calls[operation][-1] == {
            "changes": changes,
            "request_id": f"{operation}-1",
        }

    for excluded in ("purge", "restore", "import", "restore_apply", "logical_import"):
        response, _ = request(
            server,
            "POST",
            f"/api/operations/{excluded}",
            body={},
            headers=headers,
            cookies=cookie,
        )
        assert response.status == 404


def test_protected_routes_mark_user_activity_and_backend_errors_do_not_leak_fact_text(running_server) -> None:
    server, auth, overview, control, _, _ = running_server
    session, csrf = establish_primary_session(server, auth)
    activity: list[bool] = []
    original_validate = auth.validate_session

    def recording_validate(**kwargs):
        activity.append(bool(kwargs["user_activity"]))
        return original_validate(**kwargs)

    auth.validate_session = recording_validate  # type: ignore[method-assign]
    headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
    cookie = {"__Host-factlane-session": session}
    response, payload = request(
        server,
        "POST",
        "/api/read/overview",
        body={},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 200, payload
    assert activity[-1] is True
    assert overview.overview_calls

    async def leaking_revoke(**kwargs):
        del kwargs
        raise AdapterError("VERSION_CONFLICT", "secret fact body must never cross HTTP error boundary")

    control.revoke = leaking_revoke  # type: ignore[method-assign]
    response, payload = request(
        server,
        "POST",
        "/api/write/revoke",
        body={
            "scope": "PROJECT",
            "project_id": "project-a",
            "memory_id": "memory-1",
            "expected_record_id": "record-1",
            "expected_revision": 1,
            "observed_group_token": "group-token",
            "idempotency_key": "revoke-1",
            "reason_code": "OWNER_REVOKED",
        },
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 409
    decoded = json_payload(payload)
    assert decoded["code"] == "VERSION_CONFLICT"
    assert "secret fact body" not in payload.decode("utf-8")


def test_purge_preview_requires_auth_exact_target_identity_and_returns_server_binding(running_destructive_server) -> None:
    server, auth, destructive = running_destructive_server
    session, csrf = establish_primary_session(server, auth)
    cookie = {"__Host-factlane-session": session}
    operation_id = str(uuid.uuid4())
    body = {"operation_id": operation_id, "targets": [purge_target_body()]}

    response, _ = request(
        server,
        "POST",
        "/api/write/purge/preview",
        body=body,
        headers={"Origin": ORIGIN},
        cookies=cookie,
    )
    assert response.status == 403
    assert destructive.preview_calls == []

    response, _ = request(
        server,
        "POST",
        "/api/write/purge/preview",
        body={**body, "grant_secret": "must-not-be-accepted-on-preview"},
        headers={"Origin": ORIGIN, "X-FactLane-CSRF": csrf},
        cookies=cookie,
    )
    assert response.status == 400
    assert destructive.preview_calls == []

    response, _ = request(
        server,
        "POST",
        "/api/write/purge/preview",
        body={"operation_id": operation_id, "targets": [{**purge_target_body(), "path": "/forged"}]},
        headers={"Origin": ORIGIN, "X-FactLane-CSRF": csrf},
        cookies=cookie,
    )
    assert response.status == 400
    assert destructive.preview_calls == []

    forged = raw_request(
        str(server.socket_path),
        (
            "POST /api/write/purge/preview HTTP/1.1\r\n"
            f"Host: {HOST}\r\n"
            "Origin: https://forged.test\r\n"
            f"X-FactLane-CSRF: {csrf}\r\n"
            f"Cookie: __Host-factlane-session={session}\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii"),
    )
    assert forged.startswith(b"HTTP/1.1 403 ")
    assert destructive.preview_calls == []

    forged_target = {**purge_target_body(), "workflow_id": "forged-workflow"}
    response, _ = request(
        server,
        "POST",
        "/api/write/purge/preview",
        body={"operation_id": operation_id, "targets": [forged_target]},
        headers={"Origin": ORIGIN, "X-FactLane-CSRF": csrf},
        cookies=cookie,
    )
    assert response.status == 403
    assert destructive.preview_calls == []

    response, _ = request(
        server,
        "POST",
        "/api/write/purge/preview",
        body={"operation_id": operation_id, "targets": [purge_target_body() for _ in range(51)]},
        headers={"Origin": ORIGIN, "X-FactLane-CSRF": csrf},
        cookies=cookie,
    )
    assert response.status == 400
    assert destructive.preview_calls == []

    response, payload = request(
        server,
        "POST",
        "/api/write/purge/preview",
        body=body,
        headers={"Origin": ORIGIN, "X-FactLane-CSRF": csrf},
        cookies=cookie,
    )
    assert response.status == 200, payload
    decoded = json_payload(payload)
    assert decoded["status"] == "OK"
    preview = decoded["preview"]
    assert isinstance(preview, dict)
    assert preview["operation_id"] == operation_id
    assert preview["targets"] == [purge_target_body()]
    assert preview["preview_digest"] == "a" * 64
    assert preview["typed_confirmation"] == f"PURGE {'a' * 64}"
    authorization_preview = decoded["authorization_preview"]
    assert isinstance(authorization_preview, str)
    assert operation_id in authorization_preview
    assert "a" * 64 in authorization_preview
    exact_target = destructive.preview_calls[-1]["targets"][0]
    assert exact_target.to_mapping() == purge_target_body()
    assert exact_target.scope == RecoveryScope("PROJECT", project_id="project-a")


def test_purge_commit_and_recovery_retry_reuse_exact_server_preview(running_destructive_server) -> None:
    server, auth, destructive = running_destructive_server
    session, csrf = establish_primary_session(server, auth)
    headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
    cookie = {"__Host-factlane-session": session}
    operation_id = str(uuid.uuid4())
    preview_response, preview_payload = request(
        server,
        "POST",
        "/api/write/purge/preview",
        body={"operation_id": operation_id, "targets": [purge_target_body()]},
        headers=headers,
        cookies=cookie,
    )
    assert preview_response.status == 200, preview_payload
    preview_json = json_payload(preview_payload)
    preview = preview_json["preview"]
    assert isinstance(preview, dict)
    authorization_preview = str(preview_json["authorization_preview"])

    commit = {
        "operation_id": operation_id,
        "preview_digest": preview["preview_digest"],
        "typed_confirmation": preview["typed_confirmation"],
    }
    response, _ = request(
        server,
        "POST",
        "/api/write/purge/commit",
        body=commit,
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 400
    assert destructive.execute_calls == []

    other_session, other_csrf = establish_primary_session(server, auth)
    foreign_session_grant = mint_sensitive_grant(
        server,
        auth,
        session=other_session,
        csrf=other_csrf,
        preview=authorization_preview,
    )
    response, payload = request(
        server,
        "POST",
        "/api/write/purge/commit",
        body={**commit, "grant_secret": foreign_session_grant},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 403
    assert json_payload(payload)["code"] == "SENSITIVE_GRANT_BINDING_MISMATCH"
    assert destructive.execute_calls == []

    wrong_preview_grant = mint_sensitive_grant(
        server,
        auth,
        session=session,
        csrf=csrf,
        preview="different purge preview",
    )
    response, payload = request(
        server,
        "POST",
        "/api/write/purge/commit",
        body={**commit, "grant_secret": wrong_preview_grant},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 403
    assert json_payload(payload)["code"] == "SENSITIVE_PREVIEW_MISMATCH"
    assert destructive.execute_calls == []

    response, _ = request(
        server,
        "POST",
        "/api/write/purge/commit",
        body={**commit, "preview_digest": "f" * 64, "grant_secret": "grant-1"},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 409
    assert destructive.execute_calls == []

    response, _ = request(
        server,
        "POST",
        "/api/write/purge/commit",
        body={**commit, "grant_secret": "grant-1", "targets": [purge_target_body()]},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 400
    assert destructive.execute_calls == []

    first_grant = mint_sensitive_grant(
        server,
        auth,
        session=session,
        csrf=csrf,
        preview=authorization_preview,
    )
    destructive.fail_recovery_once = True
    before = time.time()
    response, payload = request(
        server,
        "POST",
        "/api/write/purge/commit",
        body={**commit, "grant_secret": first_grant},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 409
    assert json_payload(payload) == {
        "status": "ERROR",
        "code": "PURGE_RECOVERY_REQUIRED",
        "recovery_required": True,
    }
    assert len(destructive.execute_calls) == 1
    first = destructive.execute_calls[0]
    assert first["grant_secret"] == first_grant
    assert first["typed_confirmation"] == commit["typed_confirmation"]
    assert before < float(first["drain_deadline"]) <= before + 31.0

    response, payload = request(
        server,
        "POST",
        "/api/write/purge/retry",
        body={**commit, "grant_secret": first_grant},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 403
    assert json_payload(payload)["code"] == "SENSITIVE_GRANT_INVALID"
    assert len(destructive.execute_calls) == 1

    retry_grant = mint_sensitive_grant(
        server,
        auth,
        session=session,
        csrf=csrf,
        preview=authorization_preview,
    )
    response, payload = request(
        server,
        "POST",
        "/api/write/purge/retry",
        body={**commit, "grant_secret": retry_grant},
        headers=headers,
        cookies=cookie,
    )
    assert response.status == 200, payload
    result = json_payload(payload)
    assert result["operation_id"] == operation_id
    assert result["preview_digest"] == preview["preview_digest"]
    assert result["status"] == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
    assert "receipt_path" not in result
    assert "state_path" not in result
    assert len(destructive.execute_calls) == 2
    second = destructive.execute_calls[1]
    assert second["grant_secret"] == retry_grant
    assert second["preview"] is first["preview"]
    assert second["preview"].operation_id == operation_id

    response, _ = request(
        server,
        "GET",
        "/api/write/purge/retry",
        headers=headers,
        cookies=cookie,
    )
    assert response.status in {404, 405}
    assert len(destructive.execute_calls) == 2


def test_purge_recovery_retry_after_new_server_recovers_durable_preview_and_keeps_one_logical_delete(tmp_path) -> None:
    journal_path = tmp_path / "purge-state" / "durable-preview.json"
    destructive = DurableRecordingDestructive(journal_path)
    destructive.fail_recovery_once = True
    auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=FakeNativeConfirmation())
    socket_path = tmp_path / "private" / "owner-http.sock"
    server = OwnerHTTPServer(
        socket_path=socket_path,
        origin=ORIGIN,
        auth_state=auth,
        destructive_service=destructive,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    operation_id = str(uuid.uuid4())
    try:
        session, csrf = establish_primary_session(server, auth)
        headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
        cookie = {"__Host-factlane-session": session}
        response, payload = request(
            server,
            "POST",
            "/api/write/purge/preview",
            body={"operation_id": operation_id, "targets": [purge_target_body()]},
            headers=headers,
            cookies=cookie,
        )
        assert response.status == 200, payload
        preview_response = json_payload(payload)
        preview = preview_response["preview"]
        assert isinstance(preview, dict)
        authorization_preview = str(preview_response["authorization_preview"])
        grant = mint_sensitive_grant(
            server,
            auth,
            session=session,
            csrf=csrf,
            preview=authorization_preview,
        )
        commit = {
            "operation_id": operation_id,
            "preview_digest": preview["preview_digest"],
            "typed_confirmation": preview["typed_confirmation"],
            "grant_secret": grant,
        }
        response, payload = request(
            server,
            "POST",
            "/api/write/purge/commit",
            body=commit,
            headers=headers,
            cookies=cookie,
        )
        assert response.status == 409
        assert json_payload(payload)["code"] == "PURGE_RECOVERY_REQUIRED"
        assert len(destructive.execute_calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    durable = json.loads(journal_path.read_text(encoding="utf-8"))
    assert durable["logical_deletions"] == 1

    resumed_destructive = DurableRecordingDestructive(journal_path)
    resumed_auth = OwnerAuthState(owner_uid=OWNER_UID, native_confirmation=FakeNativeConfirmation())
    resumed_server = OwnerHTTPServer(
        socket_path=socket_path,
        origin=ORIGIN,
        auth_state=resumed_auth,
        destructive_service=resumed_destructive,
    )
    resumed_thread = threading.Thread(target=resumed_server.serve_forever, daemon=True)
    resumed_thread.start()
    try:
        resumed_session, resumed_csrf = establish_primary_session(resumed_server, resumed_auth)
        resumed_headers = {"Origin": ORIGIN, "X-FactLane-CSRF": resumed_csrf}
        resumed_cookie = {"__Host-factlane-session": resumed_session}
        recovery_grant = mint_sensitive_grant(
            resumed_server,
            resumed_auth,
            session=resumed_session,
            csrf=resumed_csrf,
            preview=authorization_preview,
        )
        retry = {
            "operation_id": operation_id,
            "preview_digest": preview["preview_digest"],
            "typed_confirmation": preview["typed_confirmation"],
            "grant_secret": recovery_grant,
        }

        response, payload = request(
            resumed_server,
            "POST",
            "/api/write/purge/retry",
            body={**retry, "preview_digest": "f" * 64},
            headers=resumed_headers,
            cookies=resumed_cookie,
        )
        assert response.status == 409
        assert json_payload(payload)["code"] == "PURGE_PREVIEW_MISMATCH"
        assert resumed_destructive.execute_calls == []

        response, payload = request(
            resumed_server,
            "POST",
            "/api/write/purge/retry",
            body=retry,
            headers=resumed_headers,
            cookies=resumed_cookie,
        )
        assert response.status == 200, payload
        result = json_payload(payload)
        assert result["operation_id"] == operation_id
        assert result["preview_digest"] == preview["preview_digest"]
        assert resumed_destructive.recoverable_preview_calls == [operation_id, operation_id]
        assert len(resumed_destructive.execute_calls) == 1
        recovered = resumed_destructive.execute_calls[0]["preview"]
        assert recovered.to_mapping() == preview
        assert recovered.authorization_preview == authorization_preview
        assert json.loads(journal_path.read_text(encoding="utf-8"))["logical_deletions"] == 1
    finally:
        resumed_server.shutdown()
        resumed_server.server_close()
        resumed_thread.join(timeout=2)


def test_database_json_routes_are_strict_digest_bound_and_jobs_poll_without_activity(running_database_server) -> None:
    server, auth, database = running_database_server
    artifact_id = "artifact-1"
    database.artifacts[artifact_id] = {
        "artifact_id": artifact_id,
        "kind": "LOGICAL_IMPORT",
        "size_bytes": 2,
        "sha256": "a" * 64,
        "relative_path": "private/artifact-1",
    }
    session, csrf = establish_primary_session(server, auth)
    headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
    cookies = {"__Host-factlane-session": session}

    response, payload = request(server, "POST", "/api/database/artifacts/list", body={}, headers=headers, cookies=cookies)
    assert response.status == 200, payload
    assert "relative_path" not in payload.decode("utf-8")

    response, payload = request(
        server,
        "POST",
        "/api/database/artifacts/delete/preview",
        body={"artifact_id": artifact_id},
        headers=headers,
        cookies=cookies,
    )
    assert response.status == 200, payload
    assert json_payload(payload)["preview"]["preview_digest"] == "b" * 64
    response, _ = request(
        server,
        "POST",
        "/api/database/artifacts/delete/commit",
        body={"artifact_id": artifact_id, "preview_digest": "c" * 64},
        headers=headers,
        cookies=cookies,
    )
    assert response.status == 400
    response, payload = request(
        server,
        "POST",
        "/api/database/artifacts/delete/commit",
        body={"artifact_id": artifact_id, "preview_digest": "b" * 64},
        headers=headers,
        cookies=cookies,
    )
    assert response.status == 200, payload

    operation_id = str(uuid.uuid4())
    response, payload = request(
        server,
        "POST",
        "/api/database/backup/create",
        body={"operation_id": operation_id},
        headers=headers,
        cookies=cookies,
    )
    assert response.status == 202, payload
    backup_job = json_payload(payload)["job"]

    response, payload = request(
        server,
        "POST",
        "/api/database/export/create",
        body={
            "scope": "PROJECT",
            "project_id": "project-a",
            "worktree_id": None,
            "workflow_id": None,
            "agent_id": None,
            "lifecycle_states": ["CANDIDATE", "REVOKED"],
            "max_records": 1000,
            "operation_id": operation_id,
        },
        headers=headers,
        cookies=cookies,
    )
    assert response.status == 202, payload
    export_job = json_payload(payload)["job"]
    assert database.calls["begin_logical_export_job"][-1] == {
        "selection": {
            "scope": "PROJECT",
            "project_id": "project-a",
            "worktree_id": None,
            "workflow_id": None,
            "agent_id": None,
            "lifecycle_states": ["CANDIDATE", "REVOKED"],
            "max_records": 1000,
        },
        "operation_id": operation_id,
    }
    assert "factlane_version" not in payload.decode("utf-8")
    assert "storage_contract_version" not in payload.decode("utf-8")
    assert "embedding_profile_id" not in payload.decode("utf-8")
    response, _ = request(
        server,
        "POST",
        "/api/database/export/create",
        body={"records": [], "scope": "PROJECT", "project_id": "project-a"},
        headers=headers,
        cookies=cookies,
    )
    assert response.status == 400

    for path in ("/api/database/import/preview", "/api/database/import/commit"):
        body = {"artifact_id": artifact_id, "operation_id": operation_id}
        response, payload = request(server, "POST", path, body=body, headers=headers, cookies=cookies)
        assert response.status == 202, (path, payload)

    response, payload = request(
        server,
        "POST",
        "/api/database/restore/preview",
        body={
            "artifact_id": artifact_id,
            "operation_id": operation_id,
            "trusted_origin_attested": False,
        },
        headers=headers,
        cookies=cookies,
    )
    assert response.status == 202, payload
    assert database.calls["begin_restore_preview_job"][-1] == {
        "artifact_id": artifact_id,
        "operation_id": operation_id,
        "trusted_origin_attested": False,
    }

    for path in ("/api/database/restore/commit", "/api/database/restore/retry"):
        response, payload = request(
            server,
            "POST",
            path,
            body={
                "artifact_id": artifact_id,
                "operation_id": operation_id,
                "state_restoration_acknowledged": True,
                "grant_secret": "grant-value",
            },
            headers=headers,
            cookies=cookies,
        )
        assert response.status == 202, (path, payload)
        restore_job = json_payload(payload)["job"]
        assert "grant-value" not in payload.decode("utf-8")
        assert session not in payload.decode("utf-8")
        assert "preview" not in restore_job
        assert "preview_digest" not in restore_job

    deadline = time.time() + 1.0
    while len(database.calls.get("run_restore_job", [])) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert len(database.calls.get("run_restore_job", [])) == 2
    restore_job_ids = {call["job_id"] for call in database.calls["run_restore_job"]}
    generic_job_ids = {call["job_id"] for call in database.calls.get("run_job", [])}
    assert restore_job_ids.isdisjoint(generic_job_ids)
    assert export_job["job_id"] in generic_job_ids
    assert len(database.restore_authorize_callbacks) == 2
    assert database.restore_authorize_errors == []

    activity: list[bool] = []
    original_validate = auth.validate_session
    def recording_validate(**kwargs):
        activity.append(bool(kwargs["user_activity"]))
        return original_validate(**kwargs)
    auth.validate_session = recording_validate  # type: ignore[method-assign]
    response, payload = request(
        server,
        "POST",
        "/api/database/jobs/status",
        body={"job_id": backup_job["job_id"]},
        headers=headers,
        cookies=cookies,
    )
    assert response.status == 200, payload
    assert activity[-1] is False


def test_database_restore_transport_uses_lazy_fresh_grant_binding_for_commit_and_retry(running_database_server) -> None:
    server, auth, database = running_database_server
    session, csrf = establish_primary_session(server, auth)
    headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
    cookies = {"__Host-factlane-session": session}
    database.invoke_restore_authorize = True
    artifact_id = "restore-artifact"

    def submit(path: str, *, grant_secret: str) -> tuple[dict[str, object], bytes]:
        response, payload = request(
            server,
            "POST",
            path,
            body={
                "artifact_id": artifact_id,
                "operation_id": str(uuid.uuid4()),
                "state_restoration_acknowledged": True,
                "grant_secret": grant_secret,
            },
            headers=headers,
            cookies=cookies,
        )
        assert response.status == 202, payload
        decoded = json_payload(payload)
        assert grant_secret not in payload.decode("utf-8")
        assert session not in payload.decode("utf-8")
        assert "preview" not in decoded["job"]
        assert "preview_digest" not in decoded["job"]
        return decoded["job"], payload

    def wait_terminal(job: dict[str, object]) -> str:
        job_id = str(job["job_id"])
        deadline = time.time() + 1.0
        while database.jobs[job_id]["phase"] == "QUEUED" and time.time() < deadline:
            time.sleep(0.01)
        return str(database.jobs[job_id]["phase"])

    valid_grant = mint_sensitive_grant(
        server,
        auth,
        session=session,
        csrf=csrf,
        preview=database.restore_authorization_preview,
    )
    first_job, _ = submit("/api/database/restore/commit", grant_secret=valid_grant)
    assert wait_terminal(first_job) == "SUCCEEDED"
    assert database.restore_authorize_errors == []

    replay_job, _ = submit("/api/database/restore/retry", grant_secret=valid_grant)
    assert wait_terminal(replay_job) == "FAILED"
    assert getattr(database.restore_authorize_errors[-1], "code", None) == "SENSITIVE_GRANT_INVALID"

    other_session, other_csrf = establish_primary_session(server, auth)
    foreign_grant = mint_sensitive_grant(
        server,
        auth,
        session=other_session,
        csrf=other_csrf,
        preview=database.restore_authorization_preview,
    )
    foreign_job, _ = submit("/api/database/restore/retry", grant_secret=foreign_grant)
    assert wait_terminal(foreign_job) == "FAILED"
    assert getattr(database.restore_authorize_errors[-1], "code", None) == "SENSITIVE_GRANT_BINDING_MISMATCH"

    wrong_preview_grant = mint_sensitive_grant(
        server,
        auth,
        session=session,
        csrf=csrf,
        preview="different restore authorization preview",
    )
    wrong_preview_job, _ = submit("/api/database/restore/retry", grant_secret=wrong_preview_grant)
    assert wait_terminal(wrong_preview_job) == "FAILED"
    assert getattr(database.restore_authorize_errors[-1], "code", None) == "SENSITIVE_PREVIEW_MISMATCH"

    retry_grant = mint_sensitive_grant(
        server,
        auth,
        session=session,
        csrf=csrf,
        preview=database.restore_authorization_preview,
    )
    retry_job, _ = submit("/api/database/restore/retry", grant_secret=retry_grant)
    assert wait_terminal(retry_job) == "SUCCEEDED"

    restore_job_ids = {call["job_id"] for call in database.calls["run_restore_job"]}
    generic_job_ids = {call["job_id"] for call in database.calls.get("run_job", [])}
    assert restore_job_ids.isdisjoint(generic_job_ids)
    assert len(database.restore_authorize_callbacks) == 5


def test_database_restore_boolean_fields_are_strict_and_forwarded(running_database_server) -> None:
    server, auth, database = running_database_server
    session, csrf = establish_primary_session(server, auth)
    headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
    cookies = {"__Host-factlane-session": session}
    artifact_id = "restore-artifact"
    operation_id = str(uuid.uuid4())

    for value in ("true", 1, None):
        body = {"artifact_id": artifact_id, "operation_id": operation_id}
        if value is not None:
            body["trusted_origin_attested"] = value
        response, _ = request(
            server,
            "POST",
            "/api/database/restore/preview",
            body=body,
            headers=headers,
            cookies=cookies,
        )
        assert response.status == 400
    assert database.calls.get("begin_restore_preview_job", []) == []

    response, payload = request(
        server,
        "POST",
        "/api/database/restore/preview",
        body={
            "artifact_id": artifact_id,
            "operation_id": operation_id,
            "trusted_origin_attested": False,
        },
        headers=headers,
        cookies=cookies,
    )
    assert response.status == 202, payload
    assert database.calls["begin_restore_preview_job"][-1]["trusted_origin_attested"] is False

    for value in ("true", 1, None):
        body = {
            "artifact_id": artifact_id,
            "operation_id": operation_id,
            "grant_secret": "unused-grant",
        }
        if value is not None:
            body["state_restoration_acknowledged"] = value
        before = len(database.calls.get("begin_restore_commit_job", []))
        response, _ = request(
            server,
            "POST",
            "/api/database/restore/commit",
            body=body,
            headers=headers,
            cookies=cookies,
        )
        assert response.status == 400
        assert len(database.calls.get("begin_restore_commit_job", [])) == before

    response, payload = request(
        server,
        "POST",
        "/api/database/restore/commit",
        body={
            "artifact_id": artifact_id,
            "operation_id": operation_id,
            "state_restoration_acknowledged": False,
            "grant_secret": "unused-grant",
        },
        headers=headers,
        cookies=cookies,
    )
    assert response.status == 400
    assert json_payload(payload)["code"] == "RESTORE_ACK_REQUIRED"
    assert database.calls["begin_restore_commit_job"][-1]["state_restoration_acknowledged"] is False


def test_raw_logical_upload_bypasses_json_limit_and_returns_path_free_receipt(running_database_server) -> None:
    server, auth, database = running_database_server
    session, csrf = establish_primary_session(server, auth)
    cookies = {"__Host-factlane-session": session}
    payload = b"x" * (MAX_BODY_BYTES + 1024)
    digest = hashlib.sha256(payload).hexdigest()
    headers = {
        "Origin": ORIGIN,
        "X-FactLane-CSRF": csrf,
        "Content-Type": "application/json",
        "X-FactLane-SHA256": digest,
    }
    response, body = request(server, "POST", "/api/database/import/upload", body=payload, headers=headers, cookies=cookies)
    assert response.status == 201, body
    decoded = json_payload(body)
    assert decoded["artifact"]["size_bytes"] == len(payload)
    assert decoded["upload_id"] == database.sinks[-1].upload_id
    assert "path" not in body.decode("utf-8")
    assert bytes(database.sinks[-1].data) == payload

    restore = b"db!!"
    response, body = request(
        server,
        "POST",
        "/api/database/restore/upload",
        body=restore,
        headers={
            "Origin": ORIGIN,
            "X-FactLane-CSRF": csrf,
            "Content-Type": "application/octet-stream",
            "X-FactLane-SHA256": hashlib.sha256(restore).hexdigest(),
        },
        cookies=cookies,
    )
    assert response.status == 201, body
    assert json_payload(body)["artifact"]["kind"] == "RESTORE_UPLOAD"
    assert "path" not in body.decode("utf-8")

    response, _ = request(
        server,
        "POST",
        "/api/database/artifacts/list",
        body={"padding": "x" * MAX_BODY_BYTES},
        headers={"Origin": ORIGIN, "X-FactLane-CSRF": csrf},
        cookies=cookies,
    )
    assert response.status == 413


def test_raw_upload_rejects_invalid_transport_and_aborts_incomplete_or_stalled_body(running_database_server, monkeypatch) -> None:
    server, auth, database = running_database_server
    session, csrf = establish_primary_session(server, auth)
    cookie = f"__Host-factlane-session={session}"
    base = (
        f"Host: {HOST}\r\nOrigin: {ORIGIN}\r\nX-FactLane-CSRF: {csrf}\r\nCookie: {cookie}\r\n"
    )
    unauthorized = raw_request(
        str(server.socket_path),
        (
            "POST /api/database/import/upload HTTP/1.1\r\n"
            f"Host: {HOST}\r\nOrigin: {ORIGIN}\r\nCookie: {cookie}\r\n"
            "Content-Type: application/json\r\nContent-Length: 1\r\nConnection: close\r\n\r\nx"
        ).encode("ascii"),
    )
    assert unauthorized.startswith(b"HTTP/1.1 403 ")
    assert database.sinks == []
    oversize = raw_request(
        str(server.socket_path),
        ("POST /api/database/import/upload HTTP/1.1\r\n" + base + "Content-Type: application/json\r\nContent-Length: 10485761\r\nConnection: close\r\n\r\n").encode("ascii"),
    )
    assert oversize.startswith(b"HTTP/1.1 413 ")
    transfer = raw_request(
        str(server.socket_path),
        ("POST /api/database/import/upload HTTP/1.1\r\n" + base + "Content-Type: application/json\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\nx").encode("ascii"),
    )
    assert transfer.startswith(b"HTTP/1.1 400 ")
    duplicate_length = raw_request(
        str(server.socket_path),
        (
            "POST /api/database/import/upload HTTP/1.1\r\n"
            + base
            + "Content-Type: application/json\r\nContent-Length: 1\r\nContent-Length: 1\r\nConnection: close\r\n\r\nx"
        ).encode("ascii"),
    )
    assert duplicate_length.startswith(b"HTTP/1.1 400 ")
    invalid_checksum = raw_request(
        str(server.socket_path),
        (
            "POST /api/database/import/upload HTTP/1.1\r\n"
            + base
            + "Content-Type: application/json\r\nContent-Length: 1\r\nX-FactLane-SHA256: not-a-digest\r\nConnection: close\r\n\r\nx"
        ).encode("ascii"),
    )
    assert invalid_checksum.startswith(b"HTTP/1.1 400 ")
    wrong_type = raw_request(
        str(server.socket_path),
        ("POST /api/database/import/upload HTTP/1.1\r\n" + base + "Content-Type: multipart/form-data\r\nContent-Length: 1\r\nConnection: close\r\n\r\nx").encode("ascii"),
    )
    assert wrong_type.startswith(b"HTTP/1.1 415 ")
    restore_oversize = raw_request(
        str(server.socket_path),
        (
            "POST /api/database/restore/upload HTTP/1.1\r\n"
            + base
            + "Content-Type: application/octet-stream\r\nContent-Length: 1073741825\r\nConnection: close\r\n\r\n"
        ).encode("ascii"),
    )
    assert restore_oversize.startswith(b"HTTP/1.1 413 ")

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(server.socket_path))
    client.sendall(("POST /api/database/import/upload HTTP/1.1\r\n" + base + "Content-Type: application/json\r\nContent-Length: 5\r\nConnection: close\r\n\r\nxy").encode("ascii"))
    client.shutdown(socket.SHUT_WR)
    incomplete = b"".join(iter(lambda: client.recv(8192), b""))
    client.close()
    assert incomplete.startswith(b"HTTP/1.1 400 ")
    assert database.sinks[-1].aborted is True

    monkeypatch.setattr(owner_http_module, "UPLOAD_READ_PROGRESS_TIMEOUT_SECONDS", 0.05)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(server.socket_path))
    client.sendall(("POST /api/database/import/upload HTTP/1.1\r\n" + base + "Content-Type: application/json\r\nContent-Length: 5\r\nConnection: close\r\n\r\nx").encode("ascii"))
    stalled = b"".join(iter(lambda: client.recv(8192), b""))
    client.close()
    assert stalled.startswith(b"HTTP/1.1 408 ")
    assert database.sinks[-1].aborted is True


def test_authenticated_download_denies_before_open_then_streams_exact_verified_handle(running_database_server) -> None:
    server, auth, database = running_database_server
    session, csrf = establish_primary_session(server, auth)
    cookies = {"__Host-factlane-session": session}
    artifact_id = "artifact-safe"
    response, _ = request(
        server,
        "GET",
        f"/api/database/artifacts/{artifact_id}/download",
        cookies=cookies,
    )
    assert response.status == 403
    assert database.download_open_calls == []

    response, _ = request(
        server,
        "GET",
        f"/api/database/artifacts/{artifact_id}/download",
        headers={"X-FactLane-CSRF": "forged-csrf"},
        cookies=cookies,
    )
    assert response.status == 403
    assert database.download_open_calls == []

    response, _ = request(
        server,
        "GET",
        "/api/database/artifacts/../download",
        headers={"X-FactLane-CSRF": csrf},
        cookies=cookies,
    )
    assert response.status == 404
    assert database.download_open_calls == []

    response, payload = request(
        server,
        "GET",
        f"/api/database/artifacts/{artifact_id}/download",
        headers={"X-FactLane-CSRF": csrf},
        cookies=cookies,
    )
    assert response.status == 200
    assert payload == database.download_payload
    assert response.getheader("Content-Length") == str(len(payload))
    assert response.getheader("Content-Disposition") == f'attachment; filename="factlane-logical-import-{artifact_id}.json"'
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("Cache-Control") == "no-store"
    assert database.download_open_calls == [artifact_id]
    assert database.download_handles[-1].closed is True


def test_database_json_routes_reject_unknown_fields_before_facade_calls(running_database_server) -> None:
    server, auth, database = running_database_server
    session, csrf = establish_primary_session(server, auth)
    headers = {"Origin": ORIGIN, "X-FactLane-CSRF": csrf}
    cookies = {"__Host-factlane-session": session}
    cases = (
        ("/api/database/artifacts/list", {"unexpected": True}),
        ("/api/database/backup/create", {"unexpected": True}),
        ("/api/database/import/preview", {"artifact_id": "artifact-1", "operation_id": str(uuid.uuid4()), "unexpected": True}),
        ("/api/database/jobs/status", {"job_id": str(uuid.uuid4()), "unexpected": True}),
        (
            "/api/database/restore/preview",
            {
                "artifact_id": "artifact-1",
                "operation_id": str(uuid.uuid4()),
                "trusted_origin_attested": True,
                "preview": {},
            },
        ),
        (
            "/api/database/restore/commit",
            {
                "artifact_id": "artifact-1",
                "operation_id": str(uuid.uuid4()),
                "state_restoration_acknowledged": True,
                "grant_secret": "grant",
                "preview_digest": "a" * 64,
            },
        ),
        (
            "/api/database/export/create",
            {
                "scope": "PROJECT",
                "project_id": "project-a",
                "worktree_id": None,
                "workflow_id": None,
                "agent_id": None,
                "lifecycle_states": ["CANDIDATE"],
                "max_records": 1000,
                "factlane_version": "browser-controlled",
            },
        ),
    )
    before = {name: len(calls) for name, calls in database.calls.items()}
    for path, body in cases:
        response, _ = request(server, "POST", path, body=body, headers=headers, cookies=cookies)
        assert response.status == 400, path
    after = {name: len(calls) for name, calls in database.calls.items()}
    assert after == before
