from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
import sqlite3
import sys
import threading
import time
from typing import Any, Iterator

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from factlane.adapter import MemoryAdapter, _establish_embedding_compatibility, trusted_write_context_for_profile
from factlane.contract import AdapterError, PUBLIC_TOOL_NAMES
from factlane.embeddings import EmbeddingProfile
from factlane.storage import SQLiteVecEngine


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows qualification only")

_MODEL_DIGEST = "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f"


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="windows-x64-test-256",
        provider_kind="OLLAMA_LOCAL",
        base_model_identity="nomic-embed-text:latest",
        model_digest=_MODEL_DIGEST,
        source_dimension=768,
        output_dimension=256,
        normalization_policy="OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
        distance_metric="cosine",
        projection_version="ollama-dimensions-v1",
        document_prefix="search_document: ",
        query_prefix="search_query: ",
        semantic_family="nomic-bert",
        minimum_context_window=2048,
    )


class _Provider:
    def __init__(self) -> None:
        self.profile = _profile()
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        return [[1.0] + [0.0] * 255 for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        del text
        self.query_calls += 1
        return [1.0] + [0.0] * 255

    def provider_status(self) -> dict[str, object]:
        return {
            "local_only": True,
            "provider_kind": "OLLAMA_LOCAL",
            "model": self.profile.base_model_identity,
            "digest": self.profile.model_digest,
            "ollama_version": "0.34.2",
            "semantic_family": self.profile.semantic_family,
            "native_dimension": self.profile.source_dimension,
            "output_dimension": self.profile.output_dimension,
            "effective_context_window": self.profile.minimum_context_window,
        }


async def _qualified_adapter(db_path: str, write_profile: str) -> tuple[SQLiteVecEngine, MemoryAdapter]:
    engine = SQLiteVecEngine(db_path, _profile())
    await engine.open()
    provider = _Provider()
    fingerprint = await _establish_embedding_compatibility(engine, provider, provider.provider_status())  # type: ignore[arg-type]
    engine.require_embedding_compatibility()
    return engine, MemoryAdapter(
        engine,
        provider,  # type: ignore[arg-type]
        trusted_write_context=trusted_write_context_for_profile(write_profile, contributor_ref="windows-x64"),
        embedding_runtime_fingerprint=fingerprint,
    )


def _spawn_reverify_worker(
    db_path: str,
    memory_id: str,
    record_id: str,
    marker: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    async def run() -> None:
        engine, adapter = await _qualified_adapter(db_path, "automated-verifier")
        try:
            ready.put(marker)
            if not start.wait(10):
                results.put((marker, "ERROR", "START_TIMEOUT"))
                return
            try:
                response = await adapter.dispatch(
                    "memory_update",
                    {
                        "memory_id": memory_id,
                        "expected_record_id": record_id,
                        "scope": "PROJECT",
                        "project_id": "factlane",
                        "expected_revision": 1,
                        "mode": "REVERIFY",
                        "idempotency_key": f"windows-spawn-{marker}",
                        "verification": {
                            "source_provenance": {
                                "source_class": "CURRENT_REPO",
                                "source_ref": f"windows-spawn-{marker}",
                                "source_hash": marker * 64,
                                "review_ref": "windows-spawn-cas",
                                "extraction_method": "AUTOMATED_CHECK",
                            },
                            "source_timestamp": "2026-10-10T00:00:00Z",
                            "verified_by": "AUTOMATED_CHECK",
                        },
                    },
                )
            except AdapterError as exc:
                results.put((marker, "ADAPTER_ERROR", exc.code))
            else:
                results.put((marker, "OK", response["results"][0]["revision"]))
        finally:
            await adapter.close()

    try:
        asyncio.run(run())
    except BaseException as exc:  # pragma: no cover - returned to parent for diagnosis
        results.put((marker, "PROCESS_ERROR", repr(exc)))


def _spawn_inspect_durable_db(db_path: str, memory_id: str, results: Any) -> None:
    try:
        import sqlite_vec

        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        rows = conn.execute(
            "SELECT revision,lifecycle_state FROM adapter_records WHERE memory_id=? ORDER BY revision",
            (memory_id,),
        ).fetchall()
        results.put(
            {
                "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                "quick_check": conn.execute("PRAGMA quick_check").fetchone()[0],
                "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
                "rows": rows,
                "adapter_count": int(conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0]),
                "native_count": int(conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]),
                "vector_count": int(conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]),
            }
        )
        conn.close()
    except BaseException as exc:  # pragma: no cover - returned to parent for diagnosis
        results.put({"error": repr(exc)})


def _spawn_hold_immediate_lock(db_path: str, ready: Any, release: Any, results: Any) -> None:
    try:
        conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        ready.set()
        if not release.wait(10):
            conn.rollback()
            results.put("RELEASE_TIMEOUT")
            return
        conn.rollback()
        conn.close()
        results.put("RELEASED")
    except BaseException as exc:  # pragma: no cover - returned to parent for diagnosis
        results.put(repr(exc))


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


async def _stdio_roundtrip(
    db_path: str,
    ollama_url: str,
    *,
    write_profile: str | None,
    tool: str,
    request: dict[str, object],
) -> tuple[list[str], dict[str, object]]:
    args = [
        "-m",
        "factlane.server",
        "--db",
        db_path,
        "--host-id",
        "windows-stdio",
        "--profile",
        "nomic-256",
        "--ollama-url",
        ollama_url,
    ]
    if write_profile is not None:
        args.extend(["--write-profile", write_profile])
    params = StdioServerParameters(command=sys.executable, args=args)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool(tool, {"request": request})
            assert len(result.content) == 1
            payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
            return [item.name for item in tools.tools], payload


def test_sensitive_recovery_module_imports_but_posix_maintenance_is_unsupported(tmp_path) -> None:
    from factlane.recovery import MaintenanceLease, RecoveryHold

    target = tmp_path / "must-not-be-created.db"
    with pytest.raises(RecoveryHold) as exc_info:
        with MaintenanceLease(str(target), "windows-platform-boundary"):
            raise AssertionError("unsupported Windows recovery lease unexpectedly opened")

    assert exc_info.value.code == "HOLD_UNSUPPORTED_PLATFORM_NO_MUTATION"
    assert not target.exists()


def test_standard_storage_opens_with_wal_busy_timeout_and_sqlite_vec_on_windows(tmp_path) -> None:
    import sqlite_vec

    db_path = tmp_path / "windows standard runtime 日本 memory.db"
    engine = SQLiteVecEngine(str(db_path), _profile())
    asyncio.run(engine.open())
    try:
        assert engine.conn is not None
        assert str(engine.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert int(engine.conn.execute("PRAGMA busy_timeout").fetchone()[0]) >= 5000
        assert sqlite3.sqlite_version_info >= (3, 42, 0)
        assert str(engine.conn.execute("SELECT vec_version()").fetchone()[0]).lstrip("v") == sqlite_vec.__version__
    finally:
        asyncio.run(engine.close())

    assert db_path.is_file()


def test_spawn_process_cas_has_one_winner_and_fresh_process_reopen_is_durable(tmp_path) -> None:
    db_path = str(tmp_path / "spawn-cas.db")

    async def seed() -> tuple[str, str]:
        engine, adapter = await _qualified_adapter(db_path, "automated-verifier")
        try:
            result = await adapter.dispatch(
                "memory_store",
                {
                    "fact": "Native Windows process CAS has one durable current lineage.",
                    "scope": "PROJECT",
                    "project_id": "factlane",
                    "memory_type": "PROJECT_LEARNED_FACT",
                    "source_provenance": {
                        "source_class": "CURRENT_REPO",
                        "source_ref": "windows-spawn-seed",
                        "source_hash": "a" * 64,
                        "review_ref": "windows-spawn-cas",
                        "extraction_method": "AUTOMATED_CHECK",
                    },
                    "freshness_policy": {"kind": "manual"},
                    "idempotency_key": "windows-spawn-seed",
                    "requested_lifecycle_state": "VALIDATED_CURRENT",
                    "source_timestamp": "2026-10-10T00:00:00Z",
                    "last_verified_at": "2026-10-10T00:00:00Z",
                    "verified_by": "AUTOMATED_CHECK",
                },
            )
            row = result["results"][0]
            return str(row["memory_id"]), str(row["record_id"])
        finally:
            await adapter.close()

    memory_id, record_id = asyncio.run(seed())
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    start = ctx.Event()
    results = ctx.Queue()
    workers = [
        ctx.Process(
            target=_spawn_reverify_worker,
            args=(db_path, memory_id, record_id, marker, ready, start, results),
        )
        for marker in ("b", "c")
    ]
    for worker in workers:
        worker.start()
    assert {ready.get(timeout=20), ready.get(timeout=20)} == {"b", "c"}
    start.set()
    outcomes = [results.get(timeout=30), results.get(timeout=30)]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert sorted((status, value) for _, status, value in outcomes) == [
        ("ADAPTER_ERROR", "VERSION_CONFLICT"),
        ("OK", 2),
    ]

    inspect_results = ctx.Queue()
    inspector = ctx.Process(target=_spawn_inspect_durable_db, args=(db_path, memory_id, inspect_results))
    inspector.start()
    durable = inspect_results.get(timeout=20)
    inspector.join(timeout=20)
    assert inspector.exitcode == 0
    assert "error" not in durable
    assert durable["journal_mode"] == "wal"
    assert durable["quick_check"] == "ok"
    assert durable["integrity_check"] == "ok"
    assert durable["rows"] == [(1, "SUPERSEDED"), (2, "VALIDATED_CURRENT")]
    assert durable["adapter_count"] == 2
    assert durable["native_count"] >= 1
    assert durable["vector_count"] >= 1


def test_spawn_process_busy_lock_waits_then_standard_runtime_recovers(tmp_path) -> None:
    db_path = str(tmp_path / "spawn-busy.db")
    engine = SQLiteVecEngine(db_path, _profile())
    asyncio.run(engine.open())
    try:
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        release = ctx.Event()
        results = ctx.Queue()
        holder = ctx.Process(target=_spawn_hold_immediate_lock, args=(db_path, ready, release, results))
        holder.start()
        assert ready.wait(20)
        timer = threading.Timer(0.35, release.set)
        timer.start()

        def take_write_lock() -> str:
            assert engine.conn is not None
            engine.conn.execute("BEGIN IMMEDIATE")
            engine.conn.rollback()
            return "OK"

        started = time.monotonic()
        assert asyncio.run(engine._run(take_write_lock)) == "OK"
        elapsed = time.monotonic() - started
        timer.join(timeout=2)
        assert elapsed >= 0.2
        assert results.get(timeout=20) == "RELEASED"
        holder.join(timeout=20)
        assert holder.exitcode == 0
        assert engine.conn is not None
        assert engine.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        asyncio.run(engine.close())


def test_stdio_default_read_only_candidate_and_verifier_flow_on_native_windows(tmp_path) -> None:
    db_path = str(tmp_path / "windows stdio memory.db")
    candidate_request: dict[str, object] = {
        "fact": "Native Windows stdio preserves Candidate then verifier promotion authority.",
        "scope": "CROSS_PROJECT_WORKFLOW",
        "memory_type": "WORKFLOW_RULE",
        "source_provenance": {
            "source_class": "OWNER_INPUT",
            "source_ref": "windows-stdio",
            "source_hash": "d" * 64,
            "review_ref": "windows-stdio",
            "extraction_method": "direct-input",
        },
        "freshness_policy": {"kind": "manual"},
        "idempotency_key": "windows-stdio-candidate",
    }

    with _fake_ollama() as ollama_url:
        tools, blocked = asyncio.run(
            _stdio_roundtrip(
                db_path,
                ollama_url,
                write_profile=None,
                tool="memory_store",
                request=candidate_request,
            )
        )
        assert tools == list(PUBLIC_TOOL_NAMES)
        assert blocked["status"] == "BLOCKED"
        assert blocked["error_code"] == "WRITE_AUTHORIZATION_DENIED"

        tools, candidate = asyncio.run(
            _stdio_roundtrip(
                db_path,
                ollama_url,
                write_profile="delegated-candidate",
                tool="memory_store",
                request=candidate_request,
            )
        )
        assert tools == list(PUBLIC_TOOL_NAMES)
        candidate_row = candidate["results"][0]  # type: ignore[index]
        assert candidate_row["lifecycle_state"] == "CANDIDATE"

        update_request: dict[str, object] = {
            "memory_id": candidate_row["memory_id"],
            "expected_record_id": candidate_row["record_id"],
            "scope": "CROSS_PROJECT_WORKFLOW",
            "expected_revision": 1,
            "mode": "REVERIFY",
            "idempotency_key": "windows-stdio-promote",
            "verification": {
                "source_timestamp": "2026-10-10T00:00:00Z",
                "last_verified_at": "2026-10-10T00:00:00Z",
                "verified_by": "AUTOMATED_CHECK",
            },
        }
        tools, promoted = asyncio.run(
            _stdio_roundtrip(
                db_path,
                ollama_url,
                write_profile="automated-verifier",
                tool="memory_update",
                request=update_request,
            )
        )
        assert tools == list(PUBLIC_TOOL_NAMES)
        promoted_row = promoted["results"][0]  # type: ignore[index]
        assert promoted_row["memory_id"] == candidate_row["memory_id"]
        assert promoted_row["revision"] == 2
        assert promoted_row["lifecycle_state"] == "VALIDATED_CURRENT"
        assert promoted_row["verified_by"] == "AUTOMATED_CHECK"
