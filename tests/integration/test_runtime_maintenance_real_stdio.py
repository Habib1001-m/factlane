from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
from typing import AsyncIterator, Iterator
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from factlane.adapter import MemoryAdapter
from factlane.maintenance_coordinator import (
    MaintenanceBarrierStore,
    MaintenanceCoordinator,
    MaintenanceCoordinatorError,
)
from factlane.owner_auth import NativeConfirmationRequest
from factlane.recovery import MaintenanceLease, RecoveryHold
from factlane.runtime_control import RuntimeControlClient, RuntimeRegistry
from factlane.server import _default_barrier_root, _default_runtime_root
from factlane.webui_app import create_owner_runtime_backend
from factlane.webui_runtime import BackendLaunchSpec, BackendMode
from factlane.webui_service import WebUIServiceState


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"),
    reason="real runtime-control maintenance qualification requires Linux AF_UNIX peer credentials",
)

_MODEL_DIGEST = "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f"
_TIMEOUT = 20.0
_OWNER_ORIGIN = "https://owner.test"
_OWNER_HOST = "owner.test"


class _FakeOllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _reply(self, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path == "/api/version":
            self._reply({"version": "0.34.2"})
            return
        if self.path == "/api/tags":
            self._reply(
                {
                    "models": [
                        {
                            "name": "nomic-embed-text:latest",
                            "digest": _MODEL_DIGEST,
                            "size": 1,
                            "details": {"family": "nomic-bert"},
                        }
                    ]
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size) or b"{}")
        if self.path == "/api/show":
            self._reply(
                {
                    "model_info": {
                        "nomic-bert.embedding_length": 768,
                        "nomic-bert.context_length": 2048,
                    },
                    "capabilities": ["embedding"],
                }
            )
            return
        if self.path == "/api/embed":
            inputs = request.get("input")
            dimensions = request.get("dimensions")
            assert isinstance(inputs, list) and isinstance(dimensions, int)
            vector = [1.0] + [0.0] * (dimensions - 1)
            self._reply({"embeddings": [vector for _ in inputs]})
            return
        self.send_error(404)


@contextmanager
def _fake_ollama() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _process_env(home: Path, runtime: Path, state: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_STATE_HOME": str(state),
        }
    )
    return env


@asynccontextmanager
async def _real_factlane_session(
    *,
    db_path: Path,
    ollama_url: str,
    host_id: str,
    env: dict[str, str],
    write_profile: str | None = None,
) -> AsyncIterator[ClientSession]:
    command = shutil.which("factlane")
    assert command is not None, "the shipped factlane console entry point must be installed for this integration test"
    args = [
        "--db",
        str(db_path),
        "--host-id",
        host_id,
        "--profile",
        "nomic-256",
        "--ollama-url",
        ollama_url,
    ]
    if write_profile is not None:
        args.extend(["--write-profile", write_profile])
    params = StdioServerParameters(command=command, args=args, env=env)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await asyncio.wait_for(session.initialize(), timeout=_TIMEOUT)
            yield session


async def _call(session: ClientSession, tool: str, request: dict[str, object]) -> dict[str, object]:
    result = await asyncio.wait_for(session.call_tool(tool, {"request": request}), timeout=_TIMEOUT)
    assert len(result.content) == 1
    text = result.content[0].text  # type: ignore[union-attr]
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__(_OWNER_HOST, timeout=_TIMEOUT)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


class _ApproveNativeConfirmation:
    def __init__(self) -> None:
        self.requests: list[NativeConfirmationRequest] = []

    def confirm(self, request: NativeConfirmationRequest) -> bool:
        self.requests.append(request)
        return True


def _owner_http_request(
    socket_path: str,
    method: str,
    path: str,
    *,
    body: dict[str, object] | bytes | None = None,
    session: str | None = None,
    csrf: str | None = None,
    preauth: str | None = None,
    content_type: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, list[tuple[str, str]], bytes]:
    headers = {"Host": _OWNER_HOST, **(extra_headers or {})}
    if method == "POST":
        headers["Origin"] = _OWNER_ORIGIN
    if csrf is not None:
        headers["X-FactLane-CSRF"] = csrf
    cookies: list[str] = []
    if session is not None:
        cookies.append(f"__Host-factlane-session={session}")
    if preauth is not None:
        cookies.append(f"__Host-factlane-preauth={preauth}")
    if cookies:
        headers["Cookie"] = "; ".join(cookies)

    raw: bytes | None
    if isinstance(body, dict):
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    else:
        raw = body
        if content_type is not None:
            headers["Content-Type"] = content_type
    if raw is not None:
        headers["Content-Length"] = str(len(raw))

    connection = _UnixHTTPConnection(socket_path)
    try:
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        return response.status, response.getheaders(), payload
    finally:
        connection.close()


def _response_cookies(headers: list[tuple[str, str]]) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for name, value in headers:
        if name.casefold() != "set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(value)
        for key, morsel in parsed.items():
            cookies[key] = morsel.value
    return cookies


def _json_response(payload: bytes) -> dict[str, object]:
    decoded = json.loads(payload.decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


def _native_exchange(socket_path: str, payload: dict[str, object]) -> dict[str, object]:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(_TIMEOUT)
    client.connect(socket_path)
    try:
        client.sendall(struct.pack("!I", len(raw)) + raw)
        header = client.recv(4)
        assert len(header) == 4
        size = struct.unpack("!I", header)[0]
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = client.recv(remaining)
            assert chunk
            chunks.append(chunk)
            remaining -= len(chunk)
        decoded = json.loads(b"".join(chunks).decode("utf-8"))
        assert isinstance(decoded, dict)
        return decoded
    finally:
        client.close()


async def _authenticate_owner(http_socket: str, native_socket: str, *, label: str) -> tuple[str, str]:
    enrollment = await asyncio.to_thread(
        _native_exchange,
        native_socket,
        {"op": "BEGIN_LOCAL_ENROLLMENT", "device_label": label},
    )
    assert enrollment["status"] == "OK"
    locator = str(enrollment["locator"])

    status, headers, payload = await asyncio.to_thread(
        _owner_http_request,
        http_socket,
        "POST",
        "/auth/preauth",
        body={},
    )
    assert status == 200, payload
    preauth = _response_cookies(headers)["__Host-factlane-preauth"]

    status, _, payload = await asyncio.to_thread(
        _owner_http_request,
        http_socket,
        "POST",
        "/auth/connect/claim",
        body={"locator": locator},
        preauth=preauth,
    )
    assert status == 200, payload

    approved = await asyncio.to_thread(
        _native_exchange,
        native_socket,
        {"op": "APPROVE_PENDING", "locator": locator},
    )
    assert approved == {"status": "OK", "locator": locator}

    status, headers, payload = await asyncio.to_thread(
        _owner_http_request,
        http_socket,
        "POST",
        "/auth/connect/poll",
        body={"locator": locator},
        preauth=preauth,
    )
    assert status == 200, payload
    response = _json_response(payload)
    session = _response_cookies(headers)["__Host-factlane-session"]
    csrf = str(response["csrf"])
    assert response["primary_local"] is True
    return session, csrf


async def _mint_sensitive_grant(
    http_socket: str,
    native_socket: str,
    *,
    session: str,
    csrf: str,
    preview: str,
) -> str:
    status, _, payload = await asyncio.to_thread(
        _owner_http_request,
        http_socket,
        "POST",
        "/auth/sensitive/challenge",
        body={"preview": preview},
        session=session,
        csrf=csrf,
    )
    assert status == 200, payload
    challenge_id = str(_json_response(payload)["challenge_id"])
    approved = await asyncio.to_thread(
        _native_exchange,
        native_socket,
        {"op": "APPROVE_PENDING_SENSITIVE"},
    )
    assert approved == {"status": "OK", "state": "SENSITIVE_APPROVED"}
    status, _, payload = await asyncio.to_thread(
        _owner_http_request,
        http_socket,
        "POST",
        "/auth/sensitive/grant",
        body={"challenge_id": challenge_id},
        session=session,
        csrf=csrf,
    )
    assert status == 200, payload
    return str(_json_response(payload)["grant"])


async def _database_job_status(
    http_socket: str,
    *,
    session: str,
    csrf: str,
    job_id: str,
) -> tuple[int, dict[str, object]]:
    status, _, payload = await asyncio.to_thread(
        _owner_http_request,
        http_socket,
        "POST",
        "/api/database/jobs/status",
        body={"job_id": job_id},
        session=session,
        csrf=csrf,
    )
    return status, _json_response(payload)


async def _wait_database_job(
    http_socket: str,
    *,
    session: str,
    csrf: str,
    job_id: str,
) -> dict[str, object]:
    deadline = time.monotonic() + _TIMEOUT
    while True:
        status, response = await _database_job_status(
            http_socket,
            session=session,
            csrf=csrf,
            job_id=job_id,
        )
        assert status == 200, response
        job = response.get("job")
        assert isinstance(job, dict)
        if job.get("phase") in {"SUCCEEDED", "FAILED"}:
            return job
        assert time.monotonic() < deadline, f"database job {job_id} did not reach a terminal phase"
        await asyncio.sleep(0.05)


def _fixture_request(label: str) -> dict[str, object]:
    return {
        "fact": f"Disposable D12 fixture: {label}",
        "scope": "CROSS_PROJECT_WORKFLOW",
        "memory_type": "WORKFLOW_RULE",
        "source_provenance": {
            "source_class": "OWNER_INPUT",
            "source_ref": f"real-stdio-d12:{label}",
            "source_hash": hashlib.sha256(label.encode("utf-8")).hexdigest(),
            "review_ref": f"real-stdio-d12:{label}",
            "extraction_method": "direct-input",
        },
        "freshness_policy": {"kind": "manual"},
        "idempotency_key": f"real-stdio-d12:{label}",
    }


async def _seed_fixture(
    *,
    db_path: Path,
    ollama_url: str,
    host_id: str,
    env: dict[str, str],
    label: str,
) -> dict[str, object]:
    async with _real_factlane_session(
        db_path=db_path,
        ollama_url=ollama_url,
        host_id=host_id,
        env=env,
        write_profile="delegated-candidate",
    ) as session:
        stored = await _call(session, "memory_store", _fixture_request(label))
        assert stored["status"] == "OK"
        row = stored["results"][0]  # type: ignore[index]
        assert isinstance(row, dict)
        assert row["lifecycle_state"] == "CANDIDATE"
        return dict(row)


def _exclusive_exclusion(db_path: str) -> bool:
    try:
        with MaintenanceLease(db_path, f"d12-exclusion-{uuid.uuid4()}"):
            return True
    except RecoveryHold as exc:
        if exc.code == "HOLD_NONQUIESCENT_NO_MUTATION":
            return False
        raise


def _candidate_request() -> dict[str, object]:
    return {
        "fact": "Disposable real-stdio D12 Candidate survives a governed maintenance generation change.",
        "scope": "CROSS_PROJECT_WORKFLOW",
        "memory_type": "WORKFLOW_RULE",
        "source_provenance": {
            "source_class": "OWNER_INPUT",
            "source_ref": "real-stdio-d12",
            "source_hash": "d" * 64,
            "review_ref": "real-stdio-d12",
            "extraction_method": "direct-input",
        },
        "freshness_policy": {"kind": "manual"},
        "idempotency_key": "real-stdio-d12-candidate",
    }


@contextmanager
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path, Path]]:
    # AF_UNIX paths are commonly capped at 108 bytes. Keep this disposable root
    # intentionally short instead of depending on pytest's often-deep tmp_path.
    with tempfile.TemporaryDirectory(prefix="fl-d12-", dir="/tmp") as root_text:
        root = Path(root_text)
        home = _private_dir(root / "home")
        runtime = _private_dir(root / "runtime")
        state = _private_dir(root / "state")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        monkeypatch.setenv("XDG_STATE_HOME", str(state))
        yield home, runtime, state


def test_real_stdio_runtimes_park_block_public_calls_and_resume_fresh_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qualify the real shipped stdio/runtime-control boundary without pretending to execute restore."""

    db_path = tmp_path / "d12-real-stdio.db"
    db_path.touch(mode=0o600)

    async def run(ollama_url: str) -> None:
        env = _process_env(home, runtime, state)
        async with _real_factlane_session(
            db_path=db_path,
            ollama_url=ollama_url,
            host_id="d12-real-a",
            env=env,
            write_profile="delegated-candidate",
        ) as session_a:
            async with _real_factlane_session(
                db_path=db_path,
                ollama_url=ollama_url,
                host_id="d12-real-b",
                env=env,
            ) as session_b:
                runtime_root = _default_runtime_root(str(db_path))
                assert runtime_root is not None
                registry = RuntimeRegistry(runtime_root, owner_uid=os.getuid())
                registrations = registry.registrations(database_path=str(db_path), require_live=True)
                assert len(registrations) == 2
                assert {item.lifecycle_state for item in registrations} == {"OPEN"}
                assert {item.generation for item in registrations} == {1}

                candidate = await _call(session_a, "memory_store", _candidate_request())
                assert candidate["status"] == "OK"
                row = candidate["results"][0]  # type: ignore[index]
                assert row["lifecycle_state"] == "CANDIDATE"
                get_request = {
                    "memory_id": row["memory_id"],
                    "scope": "CROSS_PROJECT_WORKFLOW",
                    "retrieval_mode": "REVIEW_HISTORY",
                }
                before = await _call(session_b, "memory_get", get_request)
                assert before["status"] == "OK"
                assert before["results"][0]["record_id"] == row["record_id"]  # type: ignore[index]

                barrier = MaintenanceBarrierStore(_default_barrier_root(str(db_path)), str(db_path))
                client = RuntimeControlClient(registry)
                coordinator = MaintenanceCoordinator(
                    barrier=barrier,
                    registry=registry,
                    call_runtime=client.call,
                    exclusion_check=_exclusive_exclusion,
                )
                operation_id = str(uuid.uuid4())
                prepared = await asyncio.wait_for(
                    coordinator.prepare_close(operation_id, drain_deadline=time.time() + 10),
                    timeout=_TIMEOUT,
                )
                assert len(prepared.initially_open_instance_ids) == 2
                parked = registry.registrations(database_path=str(db_path), require_live=True)
                assert len(parked) == 2
                assert {item.lifecycle_state for item in parked} == {"CLOSED"}

                for session in (session_a, session_b):
                    blocked = await _call(session, "memory_status", {"scope": "GLOBAL_USER"})
                    assert blocked["status"] == "BLOCKED"
                    assert blocked["error_code"] == "MAINTENANCE_IN_PROGRESS"

                # This is intentionally generation-only coordination. No database
                # replacement is claimed here: the active inode remains unchanged.
                original_identity = db_path.stat()
                activating = coordinator.begin_activation(prepared, new_generation=2)
                assert activating.state == "ACTIVATING"
                assert activating.generation == 2
                assert (activating.device, activating.inode) == (
                    original_identity.st_dev,
                    original_identity.st_ino,
                )
                published = coordinator.publish_running(prepared, new_generation=2)
                assert published.state == "RUNNING"
                assert published.generation == 2
                resumed = await asyncio.wait_for(
                    coordinator.resume_running(prepared, new_generation=2),
                    timeout=_TIMEOUT,
                )
                assert resumed.failures == {}
                assert set(resumed.ready_instance_ids) == set(prepared.initially_open_instance_ids)

                fresh_registrations = registry.registrations(database_path=str(db_path), require_live=True)
                assert len(fresh_registrations) == 2
                assert {item.lifecycle_state for item in fresh_registrations} == {"OPEN"}
                assert {item.generation for item in fresh_registrations} == {2}
                assert {(item.device, item.inode) for item in fresh_registrations} == {
                    (published.device, published.inode)
                }

                for session in (session_a, session_b):
                    fresh = await _call(session, "memory_get", get_request)
                    assert fresh["status"] == "OK"
                    assert fresh["results"][0]["record_id"] == row["record_id"]  # type: ignore[index]

    with _isolated_env(monkeypatch):
        home = Path(os.environ["HOME"])
        runtime = Path(os.environ["XDG_RUNTIME_DIR"])
        state = Path(os.environ["XDG_STATE_HOME"])
        with _fake_ollama() as ollama_url:
            asyncio.run(run(ollama_url))


def test_disposable_d12_http_restore_preserves_three_real_stdio_sessions_and_candidate_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D12 acceptance: real Owner HTTP restore, real runtime control, and three live shipped stdio clients."""

    with _isolated_env(monkeypatch):
        home = Path(os.environ["HOME"])
        runtime = Path(os.environ["XDG_RUNTIME_DIR"])
        state = Path(os.environ["XDG_STATE_HOME"])
        root = home.parent
        active_db = root / "active.db"
        restored_db = root / "restored.db"
        process_env = _process_env(home, runtime, state)

        with _fake_ollama() as ollama_url:
            async def seed() -> tuple[dict[str, object], dict[str, object]]:
                active = await _seed_fixture(
                    db_path=active_db,
                    ollama_url=ollama_url,
                    host_id="d12-seed-active",
                    env=process_env,
                    label="active-only",
                )
                restored = await _seed_fixture(
                    db_path=restored_db,
                    ollama_url=ollama_url,
                    host_id="d12-seed-restored",
                    env=process_env,
                    label="restored-only",
                )
                return active, restored

            active_fixture, restored_fixture = asyncio.run(seed())
            assert active_db.is_file() and restored_db.is_file()
            restore_bytes = restored_db.read_bytes()
            assert restore_bytes

            runtime_dir = _private_dir(runtime / "factlane")
            state_dir = _private_dir(state / "factlane")
            spec = BackendLaunchSpec(
                db=str(active_db),
                profile="nomic-256",
                host_id="d12-owner-http",
                expected_origin=_OWNER_ORIGIN,
                socket_path=str(runtime_dir / "owner-http.sock"),
                runtime_dir=str(runtime_dir),
                state_dir=str(state_dir),
                mode=BackendMode.NORMAL,
                normal_adapter_required=True,
            )
            service_state = WebUIServiceState(state_dir / "webui-service.json")
            service_state.mark_ready()
            native_confirmation = _ApproveNativeConfirmation()

            async def owner_adapter_factory(db_path: str, profile: str, **kwargs: object) -> MemoryAdapter:
                return await MemoryAdapter.create(
                    db_path,
                    profile,
                    ollama_url=ollama_url,
                    **kwargs,  # type: ignore[arg-type]
                )

            backend = create_owner_runtime_backend(
                spec,
                service_state,
                adapter_factory=owner_adapter_factory,
                native_confirmation=native_confirmation,
                owner_uid=os.getuid(),
            )
            backend.start()
            assert backend.is_ready()
            assert backend.runtime_registry is not None
            assert backend.full_restore_service is not None

            async def run_acceptance() -> None:
                async with _real_factlane_session(
                    db_path=active_db,
                    ollama_url=ollama_url,
                    host_id="d12-real-a",
                    env=process_env,
                    write_profile="delegated-candidate",
                ) as session_a:
                    async with _real_factlane_session(
                        db_path=active_db,
                        ollama_url=ollama_url,
                        host_id="d12-real-b",
                        env=process_env,
                    ) as session_b:
                        async with _real_factlane_session(
                            db_path=active_db,
                            ollama_url=ollama_url,
                            host_id="d12-real-c",
                            env=process_env,
                        ) as session_c:
                            registry = backend.runtime_registry
                            assert registry is not None
                            initial = registry.registrations(database_path=str(active_db), require_live=True)
                            assert len(initial) == 3
                            assert {identity.lifecycle_state for identity in initial} == {"OPEN"}
                            assert {identity.generation for identity in initial} == {1}
                            initial_by_id = {identity.instance_id: identity for identity in initial}
                            assert len(initial_by_id) == 3

                            active_request = {
                                "memory_id": active_fixture["memory_id"],
                                "scope": "CROSS_PROJECT_WORKFLOW",
                                "retrieval_mode": "REVIEW_HISTORY",
                            }
                            restored_request = {
                                "memory_id": restored_fixture["memory_id"],
                                "scope": "CROSS_PROJECT_WORKFLOW",
                                "retrieval_mode": "REVIEW_HISTORY",
                            }
                            active_before = await _call(session_b, "memory_get", active_request)
                            assert active_before["status"] == "OK"
                            assert active_before["results"][0]["record_id"] == active_fixture["record_id"]  # type: ignore[index]
                            # session_c intentionally remains idle between initialize and post-restore verification.

                            http_socket = str(backend.http_server.socket_path)
                            native_socket = str(backend.native_server.socket_path)
                            owner_session, owner_csrf = await _authenticate_owner(
                                http_socket,
                                native_socket,
                                label="D12 restore Owner",
                            )

                            upload_status, _, upload_payload = await asyncio.to_thread(
                                _owner_http_request,
                                http_socket,
                                "POST",
                                "/api/database/restore/upload",
                                body=restore_bytes,
                                session=owner_session,
                                csrf=owner_csrf,
                                content_type="application/octet-stream",
                                extra_headers={"X-FactLane-SHA256": hashlib.sha256(restore_bytes).hexdigest()},
                            )
                            assert upload_status == 201, upload_payload
                            upload = _json_response(upload_payload)
                            artifact_id = str(upload["artifact_id"])
                            assert "path" not in upload_payload.decode("utf-8").casefold()

                            operation_id = str(uuid.uuid4())
                            preview_status, _, preview_payload = await asyncio.to_thread(
                                _owner_http_request,
                                http_socket,
                                "POST",
                                "/api/database/restore/preview",
                                body={
                                    "artifact_id": artifact_id,
                                    "operation_id": operation_id,
                                    "trusted_origin_attested": True,
                                },
                                session=owner_session,
                                csrf=owner_csrf,
                            )
                            assert preview_status == 202, preview_payload
                            preview_job_id = str(_json_response(preview_payload)["job"]["job_id"])  # type: ignore[index]
                            preview_job = await _wait_database_job(
                                http_socket,
                                session=owner_session,
                                csrf=owner_csrf,
                                job_id=preview_job_id,
                            )
                            assert preview_job["phase"] == "SUCCEEDED", preview_job
                            preview_result = preview_job["result"]
                            assert isinstance(preview_result, dict)
                            assert preview_result["trusted_origin_attested"] is True
                            authorization_preview = str(preview_result["authorization_preview"])

                            grant = await _mint_sensitive_grant(
                                http_socket,
                                native_socket,
                                session=owner_session,
                                csrf=owner_csrf,
                                preview=authorization_preview,
                            )
                            assert native_confirmation.requests[-1].kind == "SENSITIVE_ACTION"
                            assert native_confirmation.requests[-1].preview == authorization_preview

                            commit_status, _, commit_payload = await asyncio.to_thread(
                                _owner_http_request,
                                http_socket,
                                "POST",
                                "/api/database/restore/commit",
                                body={
                                    "artifact_id": artifact_id,
                                    "operation_id": operation_id,
                                    "state_restoration_acknowledged": True,
                                    "grant_secret": grant,
                                },
                                session=owner_session,
                                csrf=owner_csrf,
                            )
                            assert commit_status == 202, commit_payload
                            commit_job_id = str(_json_response(commit_payload)["job"]["job_id"])  # type: ignore[index]
                            assert grant not in commit_payload.decode("utf-8")
                            assert owner_session not in commit_payload.decode("utf-8")

                            invalidation_deadline = time.monotonic() + _TIMEOUT
                            while True:
                                old_status, old_response = await _database_job_status(
                                    http_socket,
                                    session=owner_session,
                                    csrf=owner_csrf,
                                    job_id=commit_job_id,
                                )
                                if old_status == 401:
                                    assert old_response["code"] == "SESSION_INVALID", old_response
                                    break
                                assert old_status == 200, old_response
                                old_job = old_response.get("job")
                                assert isinstance(old_job, dict)
                                assert old_job.get("phase") != "FAILED", old_job
                                assert time.monotonic() < invalidation_deadline, "restore did not invalidate the old Owner session"
                                await asyncio.sleep(0.05)

                            fresh_session, fresh_csrf = await _authenticate_owner(
                                http_socket,
                                native_socket,
                                label="D12 post-restore Owner",
                            )
                            commit_job = await _wait_database_job(
                                http_socket,
                                session=fresh_session,
                                csrf=fresh_csrf,
                                job_id=commit_job_id,
                            )
                            if commit_job["phase"] != "SUCCEEDED":
                                journal = backend.full_restore_service.read_journal(operation_id)
                                pytest.fail(f"restore commit did not succeed: job={commit_job!r}; journal={journal!r}")
                            commit_result = commit_job["result"]
                            assert isinstance(commit_result, dict)
                            assert commit_result["generation"] == 2
                            assert commit_result["reconnect_failures"] == {}

                            activated = active_db.stat()
                            final = registry.registrations(database_path=str(active_db), require_live=True)
                            assert len(final) == 3
                            assert {identity.instance_id for identity in final} == set(initial_by_id)
                            for identity in final:
                                original = initial_by_id[identity.instance_id]
                                assert identity.pid == original.pid
                                assert identity.process_start_ticks == original.process_start_ticks
                                assert identity.boot_id == original.boot_id
                                assert identity.owner_uid == original.owner_uid
                                assert identity.control_socket_path == original.control_socket_path
                                assert identity.generation == original.generation + 1 == 2
                                assert (identity.device, identity.inode) == (activated.st_dev, activated.st_ino)
                                assert identity.lifecycle_state == "OPEN"

                            journal = backend.full_restore_service.read_journal(operation_id)
                            assert journal["phase"] == "VERIFIED"
                            assert journal["generation"] == 2
                            assert journal["activation_device"] == activated.st_dev
                            assert journal["activation_inode"] == activated.st_ino
                            assert journal["reconnect_failures"] == {}
                            assert journal["maintenance_participant_failures"] == {}
                            assert set(journal["ready_instance_ids"]) == set(initial_by_id)

                            for session in (session_a, session_b, session_c):
                                restored_after = await _call(session, "memory_get", restored_request)
                                assert restored_after["status"] == "OK"
                                assert restored_after["results"][0]["record_id"] == restored_fixture["record_id"]  # type: ignore[index]
                                retired = await _call(session, "memory_get", active_request)
                                assert retired["status"] == "BLOCKED"
                                assert retired["error_code"] == "NOT_FOUND"

                            # Acceptance evidence only: this write is deliberately after restore verification.
                            candidate = await _call(
                                session_a,
                                "memory_store",
                                _fixture_request("post-restore-acceptance-candidate"),
                            )
                            assert candidate["status"] == "OK"
                            candidate_row = candidate["results"][0]  # type: ignore[index]
                            assert candidate_row["lifecycle_state"] == "CANDIDATE"
                            assert candidate_row["verified_by"] == "UNVERIFIED"
                            assert candidate_row["contribution_origin"] == {
                                "contributor_class": "DELEGATED_AGENT",
                                "contributor_ref": "d12-real-a",
                            }

                            detail_status, _, detail_payload = await asyncio.to_thread(
                                _owner_http_request,
                                http_socket,
                                "POST",
                                "/api/read/detail",
                                body={
                                    "scope": "CROSS_PROJECT_WORKFLOW",
                                    "memory_id": candidate_row["memory_id"],
                                },
                                session=fresh_session,
                                csrf=fresh_csrf,
                            )
                            assert detail_status == 200, detail_payload
                            detail = _json_response(detail_payload)
                            latest = detail["latest"]
                            assert isinstance(latest, dict)
                            assert latest["record_id"] == candidate_row["record_id"]
                            assert latest["lifecycle_state"] == "CANDIDATE"
                            assert latest["verified_by"] == "UNVERIFIED"
                            assert latest["contribution_origin"] == {
                                "contributor_class": "DELEGATED_AGENT",
                                "contributor_ref": "d12-real-a",
                            }

            try:
                asyncio.run(run_acceptance())
            finally:
                backend.begin_drain()
                assert backend.wait_stopped(_TIMEOUT) is True


def test_unregistered_idle_real_stdio_runtime_blocks_exclusion_without_product_force_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "d12-idle-blocker.db"
    db_path.touch(mode=0o600)

    async def run(ollama_url: str) -> None:
        registered_env = _process_env(home, runtime, state)
        missing_runtime = tmp_path / "runtime-unavailable"
        assert not missing_runtime.exists()
        idle_env = _process_env(home, missing_runtime, state)

        async with _real_factlane_session(
            db_path=db_path,
            ollama_url=ollama_url,
            host_id="d12-registered",
            env=registered_env,
        ) as registered_session:
            async with _real_factlane_session(
                db_path=db_path,
                ollama_url=ollama_url,
                host_id="d12-idle-unregistered",
                env=idle_env,
            ) as idle_session:
                runtime_root = _default_runtime_root(str(db_path))
                assert runtime_root is not None
                registry = RuntimeRegistry(runtime_root, owner_uid=os.getuid())
                registrations = registry.registrations(database_path=str(db_path), require_live=True)
                assert len(registrations) == 1
                assert registrations[0].lifecycle_state == "OPEN"

                barrier = MaintenanceBarrierStore(_default_barrier_root(str(db_path)), str(db_path))
                coordinator = MaintenanceCoordinator(
                    barrier=barrier,
                    registry=registry,
                    call_runtime=RuntimeControlClient(registry).call,
                    exclusion_check=_exclusive_exclusion,
                )
                with pytest.raises(MaintenanceCoordinatorError) as blocked:
                    await asyncio.wait_for(
                        coordinator.prepare_close(str(uuid.uuid4()), drain_deadline=time.time() + 10),
                        timeout=_TIMEOUT,
                    )
                assert blocked.value.code == "EXCLUSION_UNPROVEN"
                assert barrier.read().state == "RUNNING"
                assert barrier.read().generation == 1

                # The coordinator reopens the registered runtime after abort. The
                # idle process is still alive too; product code never force-kills it.
                registered_status = await _call(registered_session, "memory_status", {"scope": "GLOBAL_USER"})
                idle_status = await _call(idle_session, "memory_status", {"scope": "GLOBAL_USER"})
                assert registered_status["status"] == "OK"
                assert idle_status["status"] == "OK"
                reopened = registry.registrations(database_path=str(db_path), require_live=True)
                assert len(reopened) == 1
                assert reopened[0].lifecycle_state == "OPEN"

    with _isolated_env(monkeypatch):
        home = Path(os.environ["HOME"])
        runtime = Path(os.environ["XDG_RUNTIME_DIR"])
        state = Path(os.environ["XDG_STATE_HOME"])
        with _fake_ollama() as ollama_url:
            asyncio.run(run(ollama_url))
