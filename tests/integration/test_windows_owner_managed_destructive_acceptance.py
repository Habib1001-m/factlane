from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import socket
import sqlite3
import ssl
import sys
import time
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows managed Owner destructive acceptance only")

import factlane.webui_app as webui_app
from factlane.adapter import MemoryAdapter, _establish_embedding_compatibility, trusted_write_context_for_profile
from factlane.contract import ScopeContext
from factlane.embeddings import EmbeddingProfile
from factlane.owner_auth import NativeConfirmationRequest
from factlane.owner_http import WindowsOwnerHTTPServer
from factlane.owner_native_control import WindowsOwnerNativeControlServer
from factlane.storage import SQLiteVecEngine
from factlane.webui_platform import WindowsNativeLauncherClient
from factlane.webui_runtime import BackendLaunchSpec, BackendMode, BackendTransportKind, PlatformKind
from factlane.webui_service import WebUIServiceState
from factlane.webui_windows import WindowsOwnerPaths
from factlane.windows_security import current_user_sid, ensure_owner_private_directory
from factlane.windows_tls import build_windows_client_ssl_context, ensure_windows_tls_material


PROFILE_NAME = "nomic-256"
PROJECT_ID = "factlane"


class _DeterministicProvider:
    def __init__(self, profile: EmbeddingProfile) -> None:
        self.profile = profile
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        vector = [0.0] * (self.profile.output_dimension - 1) + [1.0]
        return [list(vector) for _ in texts]

    def embed_query(self, _text: str) -> list[float]:
        self.query_calls += 1
        return [0.0] * (self.profile.output_dimension - 1) + [1.0]

    def provider_status(self) -> dict[str, object]:
        return {
            "local_only": True,
            "provider_kind": self.profile.provider_kind,
            "model": self.profile.base_model_identity,
            "digest": self.profile.model_digest,
            "ollama_version": "0.34.2",
            "semantic_family": self.profile.semantic_family,
            "native_dimension": self.profile.source_dimension,
            "output_dimension": self.profile.output_dimension,
            "effective_context_window": self.profile.minimum_context_window,
        }


class _ApproveNativeConfirmation:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    def confirm(self, request: NativeConfirmationRequest) -> bool:
        self.kinds.append(request.kind)
        return True


class _LoopbackHTTPSConnection(http.client.HTTPSConnection):
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


def _request(
    server: WindowsOwnerHTTPServer,
    trust_context: ssl.SSLContext,
    method: str,
    path: str,
    *,
    body: dict[str, object] | bytes | None = None,
    session: str | None = None,
    csrf: str | None = None,
    content_type: str = "application/json",
    extra_headers: dict[str, str] | None = None,
) -> tuple[http.client.HTTPResponse, bytes]:
    if isinstance(body, dict):
        raw_body: bytes | None = json.dumps(body, separators=(",", ":")).encode("utf-8")
    else:
        raw_body = body
    headers = {
        "Host": server.host,
        "Origin": server.origin,
        **(extra_headers or {}),
    }
    if raw_body is not None:
        headers.setdefault("Content-Type", content_type)
        headers.setdefault("Content-Length", str(len(raw_body)))
    if session is not None:
        headers["Cookie"] = f"__Host-factlane-session={session}"
    if csrf is not None:
        headers["X-FactLane-CSRF"] = csrf
    connection = _LoopbackHTTPSConnection(
        server.hostname,
        server.origin_port,
        timeout=10.0,
        context=trust_context,
    )
    connection.request(method, path, body=raw_body, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response, payload


def _json(payload: bytes) -> dict[str, Any]:
    decoded = json.loads(payload.decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


async def _open_deterministic_adapter(
    db_path: str,
    profile_name: str,
    *,
    runtime_agent_id: str,
    trusted_write_context: Any,
) -> tuple[SQLiteVecEngine, MemoryAdapter]:
    profile = webui_app._configured_embedding_profile(profile_name)
    engine = SQLiteVecEngine(db_path, profile)
    await engine.open()
    provider = _DeterministicProvider(profile)
    fingerprint = await _establish_embedding_compatibility(
        engine,
        provider,  # type: ignore[arg-type]
        provider.provider_status(),
    )
    engine.require_embedding_compatibility()
    adapter = MemoryAdapter(
        engine,
        provider,  # type: ignore[arg-type]
        trusted_write_context=trusted_write_context,
        runtime_agent_id=runtime_agent_id,
        embedding_runtime_fingerprint=fingerprint,
    )
    return engine, adapter


async def _adapter_factory(db_path: str, profile_name: str, **kwargs: object) -> MemoryAdapter:
    _engine, adapter = await _open_deterministic_adapter(
        db_path,
        profile_name,
        runtime_agent_id=str(kwargs["runtime_agent_id"]),
        trusted_write_context=kwargs["trusted_write_context"],
    )
    return adapter


def _provenance(key: str, marker: str) -> dict[str, str]:
    return {
        "source_class": "CURRENT_REPO",
        "source_ref": key,
        "source_hash": marker * 64,
        "review_ref": "windows-managed-owner-destructive-acceptance",
        "extraction_method": "AUTOMATED_CHECK",
    }


async def _store(adapter: MemoryAdapter, *, key: str, fact: str, marker: str) -> dict[str, Any]:
    stamp = "2026-11-06T00:00:00Z"
    result = await adapter.store(
        fact=fact,
        scope="PROJECT",
        memory_type="PROJECT_LEARNED_FACT",
        source_provenance=_provenance(key, marker),
        freshness_policy={"kind": "manual"},
        idempotency_key=key,
        project_id=PROJECT_ID,
        source_timestamp=stamp,
        last_verified_at=stamp,
        verified_by="OWNER",
        requested_lifecycle_state="VALIDATED_CURRENT",
        confidence=0.95,
        tags=["subject:windows-managed-acceptance"],
    )
    return result["results"][0]


async def _prepare_restore_database(
    db_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]:
    engine, adapter = await _open_deterministic_adapter(
        str(db_path),
        PROFILE_NAME,
        runtime_agent_id="windows-managed-restore-seed",
        trusted_write_context=trusted_write_context_for_profile(
            "owner-current",
            contributor_ref="windows-managed-restore-seed",
        ),
    )
    sensitive_token = "managedwindowspurgetarget9713"
    marker_fact = f"Restored managed Owner marker {uuid.uuid4().hex}."
    target = await _store(
        adapter,
        key="windows-managed-purge-target",
        fact=f"Sensitive target {sensitive_token}.",
        marker="a",
    )
    survivor = await _store(
        adapter,
        key="windows-managed-purge-survivor",
        fact="Unrelated managed Owner survivor must remain exact.",
        marker="b",
    )
    restored_marker = await _store(
        adapter,
        key="windows-managed-restore-marker",
        fact=marker_fact,
        marker="c",
    )
    target_rows = await engine.get_record(
        target["memory_id"],
        ScopeContext("PROJECT", project_id=PROJECT_ID),
        history=True,
    )
    survivor_rows = await engine.get_record(
        survivor["memory_id"],
        ScopeContext("PROJECT", project_id=PROJECT_ID),
        history=True,
    )
    assert len(target_rows) == len(survivor_rows) == 1
    target_row = target_rows[0]
    survivor_row = survivor_rows[0]
    assert engine.conn is not None
    engine.conn.execute(
        "INSERT INTO memory_graph(source_hash,target_hash,similarity,connection_types,metadata,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            target_row["native_content_hash"],
            survivor_row["native_content_hash"],
            0.9,
            '["related"]',
            "{}",
            1.0,
        ),
    )
    await adapter.close()
    checkpoint = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    try:
        assert tuple(checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) == (0, 0, 0)
    finally:
        checkpoint.close()
    assert sensitive_token.encode("utf-8") in db_path.read_bytes()
    return target_row, survivor_row, restored_marker, sensitive_token, marker_fact


def _establish_session(
    server: WindowsOwnerHTTPServer,
    trust_context: ssl.SSLContext,
    native_client: WindowsNativeLauncherClient,
) -> tuple[str, str]:
    response, payload = _request(server, trust_context, "POST", "/auth/preauth", body={})
    assert response.status == 200, payload
    preauth = _response_cookies(response)["__Host-factlane-preauth"]
    pending = native_client.begin_local_enrollment(device_label="Managed Owner destructive acceptance")
    response, payload = _request(
        server,
        trust_context,
        "POST",
        "/auth/connect/claim",
        body={"locator": pending.locator},
        extra_headers={"Cookie": f"__Host-factlane-preauth={preauth}"},
    )
    assert response.status == 200, payload
    native_client.approve_pending(locator=pending.locator)
    response, payload = _request(
        server,
        trust_context,
        "POST",
        "/auth/connect/poll",
        body={"locator": pending.locator},
        extra_headers={"Cookie": f"__Host-factlane-preauth={preauth}"},
    )
    assert response.status == 200, payload
    result = _json(payload)
    cookies = _response_cookies(response)
    assert result["primary_local"] is True
    return cookies["__Host-factlane-session"], str(result["csrf"])


def _mint_sensitive_grant(
    server: WindowsOwnerHTTPServer,
    trust_context: ssl.SSLContext,
    native_client: WindowsNativeLauncherClient,
    *,
    session: str,
    csrf: str,
    preview: str,
) -> str:
    response, payload = _request(
        server,
        trust_context,
        "POST",
        "/auth/sensitive/challenge",
        body={"preview": preview},
        session=session,
        csrf=csrf,
    )
    assert response.status == 200, payload
    challenge_id = str(_json(payload)["challenge_id"])
    native_client.approve_pending_sensitive()
    response, payload = _request(
        server,
        trust_context,
        "POST",
        "/auth/sensitive/grant",
        body={"challenge_id": challenge_id},
        session=session,
        csrf=csrf,
    )
    assert response.status == 200, payload
    return str(_json(payload)["grant"])


def _wait_job(
    server: WindowsOwnerHTTPServer,
    trust_context: ssl.SSLContext,
    *,
    session: str,
    csrf: str,
    job_id: str,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        response, payload = _request(
            server,
            trust_context,
            "POST",
            "/api/database/jobs/status",
            body={"job_id": job_id},
            session=session,
            csrf=csrf,
        )
        assert response.status == 200, payload
        job = _json(payload)["job"]
        assert isinstance(job, dict)
        if job.get("phase") in {"SUCCEEDED", "FAILED"}:
            return job
        assert time.monotonic() < deadline, job
        time.sleep(0.02)


def _wait_for_restore_auth_epoch_change(
    server: WindowsOwnerHTTPServer,
    trust_context: ssl.SSLContext,
    *,
    session: str,
    csrf: str,
    timeout_seconds: float = 20.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        response, payload = _request(
            server,
            trust_context,
            "POST",
            "/api/read/overview",
            body={"limit": 1},
            session=session,
            csrf=csrf,
        )
        if response.status in {401, 403}:
            code = _json(payload).get("code")
            assert code in {"SESSION_INVALID", "SESSION_EXPIRED", "CSRF_INVALID"}, payload
            return
        assert response.status in {200, 409, 503}, payload
        assert time.monotonic() < deadline, payload
        time.sleep(0.02)


def _sqlite_integrity(db_path: Path) -> None:
    connection = sqlite3.connect(db_path, timeout=5.0)
    try:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def _assert_restored_marker(db_path: Path, marker: dict[str, Any], marker_fact: str) -> None:
    connection = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = connection.execute(
            "SELECT fact FROM adapter_records WHERE memory_id=? AND record_id=?",
            (marker["memory_id"], marker["record_id"]),
        ).fetchone()
        assert row == (marker_fact,)
    finally:
        connection.close()


def _assert_purge_invariants(
    db_path: Path,
    *,
    target_row: dict[str, Any],
    survivor_row: dict[str, Any],
    token: str,
) -> None:
    connection = sqlite3.connect(db_path, timeout=5.0)
    try:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        target = connection.execute(
            "SELECT fact,lifecycle_state FROM adapter_records WHERE record_id=?",
            (target_row["record_id"],),
        ).fetchone()
        assert target == ("[purged-sensitive-memory]", "QUARANTINED")
        assert connection.execute(
            "SELECT COUNT(*) FROM memories WHERE content_hash=?",
            (target_row["native_content_hash"],),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_graph WHERE source_hash=? OR target_hash=?",
            (target_row["native_content_hash"], target_row["native_content_hash"]),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_content_fts WHERE memory_content_fts MATCH ?",
            (token,),
        ).fetchone()[0] == 0
        survivor = connection.execute(
            "SELECT fact FROM adapter_records WHERE record_id=?",
            (survivor_row["record_id"],),
        ).fetchone()
        assert survivor == (survivor_row["fact"],)
        assert connection.execute(
            "SELECT COUNT(*) FROM lifecycle_events WHERE record_id=? AND action='SENSITIVE_PURGE'",
            (target_row["record_id"],),
        ).fetchone()[0] == 1
    finally:
        connection.close()

    raw = db_path.read_bytes()
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            raw += sidecar.read_bytes()
    assert token.encode("utf-8") not in raw


def test_managed_windows_owner_https_named_pipe_restore_then_purge_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_sid = current_user_sid()
    ensure_owner_private_directory(tmp_path, owner_sid=owner_sid)
    owner_root = tmp_path / "owner"
    config_dir = owner_root / "config"
    runtime_dir = owner_root / "runtime"
    state_dir = owner_root / "state"
    browser_dir = owner_root / "browser"
    for directory in (owner_root, config_dir, runtime_dir, state_dir, browser_dir):
        ensure_owner_private_directory(directory, owner_sid=owner_sid)

    pipe_suffix = uuid.uuid4().hex
    owner_paths = WindowsOwnerPaths(
        root_dir=str(owner_root),
        owner_config_home=str(config_dir),
        service_config_path=str(config_dir / "webui.json"),
        runtime_dir=str(runtime_dir),
        state_dir=str(state_dir),
        browser_profile_dir=str(browser_dir),
        native_control_socket=rf"\\.\pipe\factlane-owner-managed-{pipe_suffix}",
        backend_endpoint=rf"\\.\pipe\factlane-owner-http-managed-{pipe_suffix}",
        task_receipt_path=str(state_dir / "task-scheduler.json"),
    )
    monkeypatch.setattr(webui_app, "windows_owner_paths", lambda *, owner_sid: owner_paths)

    restore_db = tmp_path / "restore-source.db"
    target_row, survivor_row, restored_marker, sensitive_token, marker_fact = asyncio.run(
        _prepare_restore_database(restore_db)
    )
    active_db = tmp_path / "active.db"
    port = _loopback_port()
    hostname = f"factlane-managed-{uuid.uuid4().hex[:12]}.test"
    origin = f"https://{hostname}:{port}"
    spec = BackendLaunchSpec(
        db=str(active_db),
        profile=PROFILE_NAME,
        host_id="windows-managed-owner-acceptance",
        expected_origin=origin,
        socket_path=None,
        runtime_dir=str(runtime_dir),
        state_dir=str(state_dir),
        mode=BackendMode.NORMAL,
        normal_adapter_required=True,
        platform_kind=PlatformKind.WINDOWS,
        backend_transport=BackendTransportKind.WINDOWS_HTTPS,
    )
    service_state = WebUIServiceState(state_dir / "webui-service.json")
    service_state.mark_ready()
    tls_material = ensure_windows_tls_material(state_dir / "tls", hostname, owner_sid=owner_sid)
    confirmation = _ApproveNativeConfirmation()
    backend = webui_app.create_owner_runtime_backend(
        spec,
        service_state,
        adapter_factory=_adapter_factory,
        native_confirmation=confirmation,
    )
    assert isinstance(backend.http_server, WindowsOwnerHTTPServer)
    assert isinstance(backend.native_server, WindowsOwnerNativeControlServer)
    assert backend.runtime_registry is not None
    assert backend.maintenance_coordinator is not None
    assert backend.full_restore_service is not None
    assert backend.full_restore_service._windows_restore_operator_factory is webui_app.WindowsOwnerRestoreOperator
    assert backend.http_server.destructive_service is not None
    assert backend.http_server.destructive_service._recovery_operator.__class__ is webui_app.WindowsOwnerPurgeOperator
    assert backend.native_server.pipe_name == owner_paths.native_control_socket

    trust_context = build_windows_client_ssl_context(tls_material)
    backend.start()
    try:
        assert backend.is_ready() is True
        native_client = WindowsNativeLauncherClient(
            owner_paths.native_control_socket,
            expected_server=backend.native_server.process_identity,
            owner_sid=owner_sid,
        )
        session, csrf = _establish_session(backend.http_server, trust_context, native_client)

        restore_bytes = restore_db.read_bytes()
        response, payload = _request(
            backend.http_server,
            trust_context,
            "POST",
            "/api/database/restore/upload",
            body=restore_bytes,
            session=session,
            csrf=csrf,
            content_type="application/octet-stream",
            extra_headers={"X-FactLane-SHA256": hashlib.sha256(restore_bytes).hexdigest()},
        )
        assert response.status == 201, payload
        artifact_id = str(_json(payload)["artifact_id"])
        restore_operation_id = str(uuid.uuid4())
        response, payload = _request(
            backend.http_server,
            trust_context,
            "POST",
            "/api/database/restore/preview",
            body={
                "artifact_id": artifact_id,
                "operation_id": restore_operation_id,
                "trusted_origin_attested": True,
            },
            session=session,
            csrf=csrf,
        )
        assert response.status == 202, payload
        preview_job_id = str(_json(payload)["job"]["job_id"])
        preview_job = _wait_job(
            backend.http_server,
            trust_context,
            session=session,
            csrf=csrf,
            job_id=preview_job_id,
        )
        assert preview_job["phase"] == "SUCCEEDED", preview_job
        preview_result = preview_job["result"]
        assert isinstance(preview_result, dict)
        authorization_preview = str(preview_result["authorization_preview"])
        restore_grant = _mint_sensitive_grant(
            backend.http_server,
            trust_context,
            native_client,
            session=session,
            csrf=csrf,
            preview=authorization_preview,
        )
        response, payload = _request(
            backend.http_server,
            trust_context,
            "POST",
            "/api/database/restore/commit",
            body={
                "artifact_id": artifact_id,
                "operation_id": restore_operation_id,
                "state_restoration_acknowledged": True,
                "grant_secret": restore_grant,
            },
            session=session,
            csrf=csrf,
        )
        assert response.status == 202, payload
        restore_job_id = str(_json(payload)["job"]["job_id"])

        _wait_for_restore_auth_epoch_change(
            backend.http_server,
            trust_context,
            session=session,
            csrf=csrf,
        )
        session, csrf = _establish_session(backend.http_server, trust_context, native_client)
        restore_job = _wait_job(
            backend.http_server,
            trust_context,
            session=session,
            csrf=csrf,
            job_id=restore_job_id,
        )
        assert restore_job["phase"] == "SUCCEEDED", restore_job
        restore_result = restore_job["result"]
        assert isinstance(restore_result, dict)
        assert restore_result["status"] == "OK"
        assert restore_result["operation_id"] == restore_operation_id
        verification = restore_result["post_activation_verification"]
        assert verification["status"] == "VERIFIED"
        assert verification["bounded_read_verified"] is True
        active_info = active_db.stat()
        assert restore_result["activation_device"] == active_info.st_dev
        assert restore_result["activation_inode"] == active_info.st_ino
        _assert_restored_marker(active_db, restored_marker, marker_fact)
        _sqlite_integrity(active_db)

        purge_target = {
            "scope": "PROJECT",
            "project_id": PROJECT_ID,
            "worktree_id": None,
            "workflow_id": None,
            "agent_id": None,
            "memory_id": target_row["memory_id"],
            "record_id": target_row["record_id"],
            "revision": target_row["revision"],
        }
        purge_operation_id = str(uuid.uuid4())
        response, payload = _request(
            backend.http_server,
            trust_context,
            "POST",
            "/api/write/purge/preview",
            body={"operation_id": purge_operation_id, "targets": [purge_target]},
            session=session,
            csrf=csrf,
        )
        assert response.status == 200, payload
        purge_payload = _json(payload)
        purge_preview = purge_payload["preview"]
        assert purge_preview["targets"] == [purge_target]
        purge_grant = _mint_sensitive_grant(
            backend.http_server,
            trust_context,
            native_client,
            session=session,
            csrf=csrf,
            preview=str(purge_payload["authorization_preview"]),
        )
        response, payload = _request(
            backend.http_server,
            trust_context,
            "POST",
            "/api/write/purge/commit",
            body={
                "operation_id": purge_operation_id,
                "preview_digest": purge_preview["preview_digest"],
                "typed_confirmation": purge_preview["typed_confirmation"],
                "grant_secret": purge_grant,
            },
            session=session,
            csrf=csrf,
        )
        assert response.status == 200, payload
        purge_result = _json(payload)
        assert purge_result["status"] == "S1_LOCAL_FACTLANE_PURGE_VERIFIED"
        assert purge_result["operation_id"] == purge_operation_id
        _assert_purge_invariants(
            active_db,
            target_row=target_row,
            survivor_row=survivor_row,
            token=sensitive_token,
        )

        response, payload = _request(
            backend.http_server,
            trust_context,
            "POST",
            "/api/read/overview",
            body={"limit": 25},
            session=session,
            csrf=csrf,
        )
        assert response.status == 200, payload
        overview = _json(payload)
        assert overview["status"] == "OK"
        assert "scope_index" in overview
        assert backend.adapter is not None
        assert backend.owner_adapter_participant is not None
        assert backend.owner_adapter_participant.state == "RUNNING"
        assert confirmation.kinds.count("ENROLLMENT") >= 2
        assert confirmation.kinds.count("SENSITIVE_ACTION") == 2
    finally:
        backend.begin_drain()
        assert backend.wait_stopped(5.0) is True
