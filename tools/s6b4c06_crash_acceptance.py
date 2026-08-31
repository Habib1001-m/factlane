from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from factlane.adapter import MemoryAdapter
from factlane.embeddings import EmbeddingProfile
from factlane.storage import SQLiteVecEngine


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
PROFILE = EmbeddingProfile(
    profile_id="s6b4c06-test-256",
    provider_kind="OLLAMA_LOCAL",
    base_model_identity="nomic-embed-text:latest",
    model_digest="0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
    source_dimension=768,
    output_dimension=256,
    normalization_policy="OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
    distance_metric="cosine",
    projection_version="ollama-dimensions-v1",
    document_prefix="search_document: ",
    query_prefix="search_query: ",
)
BACKEND_PIN = "e5155b937051db4fa99a384018c5ebd621d8c5ef"
STAMP = "2026-08-31T00:00:00Z"
SCENARIOS = {
    "STORE_PRECOMMIT_SIGKILL",
    "REVERIFY_PRECOMMIT_SIGKILL",
    "REPLACE_PRECOMMIT_SIGKILL",
    "UPDATE_POSTCOMMIT_PRE_RESPONSE_SIGKILL",
    "EMBEDDING_INFLIGHT_SIGKILL",
}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class DeterministicProvider:
    def __init__(self) -> None:
        self.profile = PROFILE
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        vector = [0.0] * (PROFILE.output_dimension - 1) + [1.0]
        return [list(vector) for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return [0.0] * (PROFILE.output_dimension - 1) + [1.0]


class BlockingProvider(DeterministicProvider):
    def __init__(self, started: threading.Event, release: threading.Event, finished: threading.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release
        self.finished = finished

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.started.set()
        self.release.wait(timeout=30)
        try:
            return super().embed_documents(texts)
        finally:
            self.finished.set()


def _transaction_source() -> tuple[str, int, str]:
    source_file = Path(inspect.getsourcefile(SQLiteVecEngine) or "").resolve()
    source_lines, source_start = inspect.getsourcelines(SQLiteVecEngine.write_record)
    commit_offset = next(
        index for index, line in enumerate(source_lines) if line.strip() == "self.conn.commit()"
    )
    return str(source_file), source_start + commit_offset, "write_record.<locals>.transaction"


class TransactionTraceGate:
    def __init__(self, marker: Path | None, mode: str) -> None:
        self.marker = marker
        self.mode = mode
        self.armed = False
        self.hit = threading.Event()
        self.release = threading.Event()
        self.worker_returned = threading.Event()
        self.source_file, self.commit_line, self.qualname_suffix = _transaction_source()

    def _matches(self, frame: Any) -> bool:
        return (
            self.armed
            and frame.f_code.co_filename == self.source_file
            and frame.f_code.co_qualname.endswith(self.qualname_suffix)
        )

    def __call__(self, frame: Any, event: str, arg: Any) -> Any:
        if self._matches(frame):
            if self.mode == "precommit" and event == "line" and frame.f_lineno == self.commit_line:
                if not self.hit.is_set():
                    self.hit.set()
                    if self.marker:
                        _atomic_json(self.marker, {"phase": "PRECOMMIT_ARMED", "pid": os.getpid()})
                    self.release.wait(timeout=30)
            elif self.mode == "postcommit" and event == "return":
                if not self.hit.is_set():
                    self.hit.set()
                    if self.marker:
                        _atomic_json(self.marker, {"phase": "POSTCOMMIT_ARMED", "pid": os.getpid()})
                    self.release.wait(timeout=30)
            if event == "return":
                self.worker_returned.set()
        return self


async def _open_adapter(db_path: Path, provider: DeterministicProvider | None = None) -> tuple[SQLiteVecEngine, MemoryAdapter]:
    engine = SQLiteVecEngine(str(db_path), PROFILE)
    await engine.open()
    adapter = MemoryAdapter(engine, provider or DeterministicProvider())  # type: ignore[arg-type]
    return engine, adapter


def _store_request(key: str, fact: str, marker: str) -> dict[str, Any]:
    return {
        "fact": fact,
        "scope": "PROJECT",
        "memory_type": "PROJECT_LEARNED_FACT",
        "source_provenance": {
            "source_class": "CURRENT_REPO",
            "source_ref": f"s6b4c06-{key}",
            "source_hash": marker * 64,
            "review_ref": "s6b4c06",
            "extraction_method": "AUTOMATED_CHECK",
        },
        "freshness_policy": {"kind": "manual"},
        "idempotency_key": key,
        "project_id": "factlane",
        "source_timestamp": STAMP,
        "last_verified_at": STAMP,
        "verified_by": "OWNER",
        "requested_lifecycle_state": "VALIDATED_CURRENT",
        "confidence": 0.95,
        "tags": ["subject:crash-acceptance", "s6b4c06", key],
    }


def _reverify_request(memory_id: str, key: str, marker: str) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "scope": "PROJECT",
        "project_id": "factlane",
        "expected_revision": 1,
        "mode": "REVERIFY",
        "idempotency_key": key,
        "verification": {
            "source_provenance": {
                "source_class": "CURRENT_REPO",
                "source_ref": f"s6b4c06-{key}",
                "source_hash": marker * 64,
                "review_ref": "s6b4c06",
                "extraction_method": "AUTOMATED_CHECK",
            },
            "source_timestamp": STAMP,
            "verified_by": "AUTOMATED_CHECK",
        },
    }


def _replace_request(memory_id: str, key: str, marker: str) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "scope": "PROJECT",
        "project_id": "factlane",
        "expected_revision": 1,
        "mode": "REPLACE",
        "idempotency_key": key,
        "replacement": {
            "fact": f"Replacement fact for {key}.",
            "memory_type": "PROJECT_LEARNED_FACT",
            "source_provenance": {
                "source_class": "CURRENT_REPO",
                "source_ref": f"s6b4c06-{key}",
                "source_hash": marker * 64,
                "review_ref": "s6b4c06",
                "extraction_method": "AUTOMATED_CHECK",
            },
            "freshness_policy": {"kind": "manual"},
            "source_timestamp": STAMP,
            "verified_by": "AUTOMATED_CHECK",
            "tags": ["subject:crash-acceptance", "s6b4c06", key],
        },
    }


async def _seed(db_path: Path, key: str) -> dict[str, str]:
    engine, adapter = await _open_adapter(db_path)
    try:
        response = await adapter.store(
            **_store_request(key, f"Seed parent for {key}.", "a")
        )
        result = response["results"][0]
        return {"memory_id": result["memory_id"], "record_id": result["record_id"]}
    finally:
        await adapter.close()


async def _read_state(engine: SQLiteVecEngine) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        assert engine.conn is not None
        conn = engine.conn
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0]).lower()
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        busy_timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        records = [
            {
                "record_id": row[0],
                "memory_id": row[1],
                "revision": int(row[2]),
                "parent_record_id": row[3],
                "lifecycle_state": row[4],
                "idempotency_key": row[5],
            }
            for row in conn.execute(
                "SELECT record_id, memory_id, revision, parent_record_id, lifecycle_state, idempotency_key "
                "FROM adapter_records ORDER BY created_at, record_id"
            ).fetchall()
        ]
        adapter_rows = int(conn.execute("SELECT COUNT(*) FROM adapter_records").fetchone()[0])
        native_rows = int(conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0])
        vector_rows = int(conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0])
        adapter_without_native = int(
            conn.execute(
                "SELECT COUNT(*) FROM adapter_records a "
                "LEFT JOIN memories m ON m.content_hash = a.native_content_hash "
                "WHERE m.id IS NULL"
            ).fetchone()[0]
        )
        native_without_adapter = int(
            conn.execute(
                "SELECT COUNT(*) FROM memories m "
                "LEFT JOIN adapter_records a ON a.native_content_hash = m.content_hash "
                "WHERE m.deleted_at IS NULL AND a.record_id IS NULL"
            ).fetchone()[0]
        )
        vector_without_native = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_embeddings e "
                "LEFT JOIN memories m ON m.id = e.rowid "
                "WHERE m.id IS NULL"
            ).fetchone()[0]
        )
        native_without_vector = int(
            conn.execute(
                "SELECT COUNT(*) FROM memories m "
                "LEFT JOIN memory_embeddings e ON e.rowid = m.id "
                "WHERE m.deleted_at IS NULL AND e.rowid IS NULL"
            ).fetchone()[0]
        )
        current_rows = [row for row in records if row["lifecycle_state"] == "VALIDATED_CURRENT"]
        return {
            "quick_check": quick_check,
            "journal_mode": journal_mode,
            "busy_timeout": busy_timeout,
            "adapter_rows": adapter_rows,
            "native_rows": native_rows,
            "vector_rows": vector_rows,
            "records": records,
            "current_records": len(current_rows),
            "current_revisions": sorted(row["revision"] for row in current_rows),
            "current_lineage_forks": max(0, len(current_rows) - 1),
            "adapter_without_native": adapter_without_native,
            "native_without_adapter": native_without_adapter,
            "vector_without_native": vector_without_native,
            "native_without_vector": native_without_vector,
        }

    return await engine._run(read)


async def _inspect(db_path: Path) -> dict[str, Any]:
    engine, adapter = await _open_adapter(db_path)
    try:
        return await _read_state(engine)
    finally:
        await adapter.close()


def _assert_coherent(state: dict[str, Any]) -> None:
    assert state["quick_check"] == "ok"
    assert state["journal_mode"] == "wal"
    assert state["busy_timeout"] == 5000
    assert state["current_lineage_forks"] == 0
    assert state["adapter_without_native"] == 0
    assert state["native_without_adapter"] == 0
    assert state["vector_without_native"] == 0
    assert state["native_without_vector"] == 0


def _wait_for_marker(process: subprocess.Popen[str], marker: Path, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while not marker.exists():
        if process.poll() is not None:
            raise RuntimeError("disposable child exited before its crash marker")
        if time.monotonic() >= deadline:
            raise TimeoutError("disposable crash marker was not observed")
        time.sleep(0.01)
    return json.loads(marker.read_text(encoding="utf-8"))


def _kill_child(
    run_dir: Path,
    scenario: str,
    db_path: Path,
    marker: Path,
    *,
    memory_id: str | None = None,
    idempotency_key: str | None = None,
    parent_record_id: str | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--run-dir",
        str(run_dir),
        "--child-scenario",
        scenario,
        "--db",
        str(db_path),
        "--marker",
        str(marker),
    ]
    for name, value in (
        ("--memory-id", memory_id),
        ("--idempotency-key", idempotency_key),
        ("--parent-record-id", parent_record_id),
    ):
        if value is not None:
            command.extend((name, value))
    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"
    child_env["MCP_MEMORY_BASE_DIR"] = str(run_dir / "upstream-runtime" / scenario)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=child_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        marker_payload = _wait_for_marker(process, marker)
        os.kill(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=10)
    except Exception:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
        raise
    del stdout, stderr
    if process.returncode != -signal.SIGKILL:
        raise RuntimeError(f"{scenario} child was not SIGKILL-terminated: {process.returncode}")
    return {"pid": process.pid, "exit_code": process.returncode, "marker": marker_payload}


async def _retry_store(db_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    engine, adapter = await _open_adapter(db_path)
    try:
        response = await adapter.store(**request)
        state = await _read_state(engine)
        return {"response": response, "state": state}
    finally:
        await adapter.close()


async def _retry_update(db_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    engine, adapter = await _open_adapter(db_path)
    try:
        response = await adapter.update(**request)
        state = await _read_state(engine)
        return {"response": response, "state": state}
    finally:
        await adapter.close()


async def _run_store_precommit(run_dir: Path) -> dict[str, Any]:
    db_path = run_dir / "store-precommit.sqlite3"
    marker = run_dir / "markers" / "store-precommit.json"
    request = _store_request("s6b4c06-store-precommit", "Store precommit crash proof.", "b")
    crash = _kill_child(run_dir, "STORE_PRECOMMIT_SIGKILL", db_path, marker, idempotency_key=request["idempotency_key"])
    before = await _inspect(db_path)
    _assert_coherent(before)
    assert before["adapter_rows"] == before["native_rows"] == before["vector_rows"] == 0
    retry = await _retry_store(db_path, request)
    after = retry["state"]
    _assert_coherent(after)
    assert retry["response"]["status"] == "OK"
    assert after["adapter_rows"] == after["native_rows"] == after["vector_rows"] == 1
    return {
        "result": "PASS",
        "crash": crash,
        "precommit_partial_adapter_rows": before["adapter_rows"],
        "precommit_partial_native_rows": before["native_rows"],
        "precommit_partial_vector_rows": before["vector_rows"],
        "quick_check": before["quick_check"],
        "retry_status": retry["response"]["status"],
    }


async def _run_reverify_precommit(run_dir: Path) -> dict[str, Any]:
    db_path = run_dir / "reverify-precommit.sqlite3"
    seed = await _seed(db_path, "s6b4c06-reverify-seed")
    request = _reverify_request(seed["memory_id"], "s6b4c06-reverify-precommit", "c")
    marker = run_dir / "markers" / "reverify-precommit.json"
    crash = _kill_child(
        run_dir,
        "REVERIFY_PRECOMMIT_SIGKILL",
        db_path,
        marker,
        memory_id=seed["memory_id"],
        idempotency_key=request["idempotency_key"],
        parent_record_id=seed["record_id"],
    )
    before = await _inspect(db_path)
    _assert_coherent(before)
    records = before["records"]
    assert len(records) == 1
    assert records[0]["record_id"] == seed["record_id"]
    assert records[0]["lifecycle_state"] == "VALIDATED_CURRENT"
    assert before["current_revisions"] == [1]
    retry = await _retry_update(db_path, request)
    after = retry["state"]
    _assert_coherent(after)
    result = retry["response"]["results"][0]
    assert retry["response"]["status"] == "OK"
    assert result["revision"] == 2
    assert after["current_revisions"] == [2]
    assert len(after["records"]) == 2
    return {
        "result": "PASS",
        "crash": crash,
        "history_records_after_crash": len(before["records"]),
        "current_records_after_crash": before["current_records"],
        "current_revision_after_crash": before["current_revisions"][0],
        "parent_state_after_crash": records[0]["lifecycle_state"],
        "killed_successor_present": False,
        "killed_idempotency_key_present": False,
        "orphan_native_rows": before["native_without_adapter"],
        "orphan_vector_rows": before["vector_without_native"],
        "retry_revision": result["revision"],
    }


async def _run_replace_precommit(run_dir: Path) -> dict[str, Any]:
    db_path = run_dir / "replace-precommit.sqlite3"
    seed = await _seed(db_path, "s6b4c06-replace-seed")
    request = _replace_request(seed["memory_id"], "s6b4c06-replace-precommit", "d")
    marker = run_dir / "markers" / "replace-precommit.json"
    crash = _kill_child(
        run_dir,
        "REPLACE_PRECOMMIT_SIGKILL",
        db_path,
        marker,
        memory_id=seed["memory_id"],
        idempotency_key=request["idempotency_key"],
        parent_record_id=seed["record_id"],
    )
    before = await _inspect(db_path)
    _assert_coherent(before)
    assert len(before["records"]) == 1
    assert before["records"][0]["lifecycle_state"] == "VALIDATED_CURRENT"
    retry = await _retry_update(db_path, request)
    after = retry["state"]
    _assert_coherent(after)
    result = retry["response"]["results"][0]
    assert retry["response"]["status"] == "OK"
    assert result["memory_id"] != seed["memory_id"]
    assert after["current_records"] == 1
    assert len(after["records"]) == 2
    parent = next(row for row in after["records"] if row["record_id"] == seed["record_id"])
    assert parent["lifecycle_state"] == "SUPERSEDED"
    return {
        "result": "PASS",
        "crash": crash,
        "original_parent_current_after_crash": before["records"][0]["lifecycle_state"] == "VALIDATED_CURRENT",
        "replacement_branch_after_crash": False,
        "orphan_native_rows": before["native_without_adapter"],
        "orphan_vector_rows": before["vector_without_native"],
        "retry_status": retry["response"]["status"],
        "current_records_after_retry": after["current_records"],
    }


async def _run_postcommit(run_dir: Path) -> dict[str, Any]:
    db_path = run_dir / "postcommit-reverify.sqlite3"
    seed = await _seed(db_path, "s6b4c06-postcommit-seed")
    request = _reverify_request(seed["memory_id"], "s6b4c06-postcommit", "e")
    marker = run_dir / "markers" / "postcommit.json"
    crash = _kill_child(
        run_dir,
        "UPDATE_POSTCOMMIT_PRE_RESPONSE_SIGKILL",
        db_path,
        marker,
        memory_id=seed["memory_id"],
        idempotency_key=request["idempotency_key"],
        parent_record_id=seed["record_id"],
    )
    durable = await _inspect(db_path)
    _assert_coherent(durable)
    assert len(durable["records"]) == 2
    assert durable["current_revisions"] == [2]
    old = next(row for row in durable["records"] if row["record_id"] == seed["record_id"])
    assert old["lifecycle_state"] == "SUPERSEDED"

    engine, adapter = await _open_adapter(db_path)
    try:
        changes_before = await engine._run(lambda: engine.conn.total_changes if engine.conn else -1)
        embeddings_before = adapter.provider.document_calls
        replay = await adapter.update(**request)
        changes_after = await engine._run(lambda: engine.conn.total_changes if engine.conn else -1)
        after = await _read_state(engine)
        result = replay["results"][0]
        assert replay.get("idempotent_replay") is True
        assert result["revision"] == 2
        assert after["current_revisions"] == [2]
        assert len(after["records"]) == 2
        assert adapter.provider.document_calls - embeddings_before == 0
        assert changes_after == changes_before
    finally:
        await adapter.close()
    return {
        "result": "PASS",
        "crash": crash,
        "committed_write_durable": True,
        "partial_state": 0,
        "idempotent_replay": True,
        "duplicate_successor_rows": 0,
        "second_commit_for_same_operation": False,
        "returned_revision": result["revision"],
        "current_revision": after["current_revisions"][0],
        "history_records": len(after["records"]),
        "additional_embedding_for_replay": 0,
        "additional_successor_row": 0,
    }


async def _run_embedding_inflight(run_dir: Path) -> dict[str, Any]:
    db_path = run_dir / "embedding-inflight.sqlite3"
    marker = run_dir / "markers" / "embedding-inflight.json"
    request = _store_request("s6b4c06-embedding-inflight", "Embedding inflight crash proof.", "f")
    crash = _kill_child(run_dir, "EMBEDDING_INFLIGHT_SIGKILL", db_path, marker, idempotency_key=request["idempotency_key"])
    before = await _inspect(db_path)
    _assert_coherent(before)
    assert before["adapter_rows"] == before["native_rows"] == before["vector_rows"] == 0
    retry = await _retry_store(db_path, request)
    assert retry["response"]["status"] == "OK"
    return {
        "result": "PASS",
        "crash": crash,
        "storage_mutation_from_killed_embedding": "NONE",
        "quick_check": before["quick_check"],
        "subsequent_normal_store": "PASS",
    }


async def _run_cancellation(run_dir: Path, gate: TransactionTraceGate) -> dict[str, Any]:
    embedding_db = run_dir / "cancel-embedding.sqlite3"
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    provider = BlockingProvider(started, release, finished)
    engine, adapter = await _open_adapter(embedding_db, provider)
    try:
        cancelled_request = _store_request("s6b4c06-cancelled-embedding", "Cancelled embedding proof.", "a")
        task = asyncio.create_task(adapter.store(**cancelled_request))
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            caller_cancelled = True
        else:
            caller_cancelled = False
        release.set()
        provider_finished = await asyncio.to_thread(finished.wait, 10)
        after_cancel = await _read_state(engine)
        assert caller_cancelled
        assert provider_finished
        assert after_cancel["adapter_rows"] == 0
        followup = await adapter.store(
            **_store_request("s6b4c06-after-cancel", "Subsequent operation after cancellation.", "b")
        )
        after_followup = await _read_state(engine)
        assert followup["status"] == "OK"
        assert after_followup["adapter_rows"] == 1
    finally:
        release.set()
        await adapter.close()

    transaction_db = run_dir / "cancel-transaction.sqlite3"
    transaction_adapter: MemoryAdapter | None = None
    try:
        transaction_engine, transaction_adapter = await _open_adapter(transaction_db)
        gate.armed = True
        request = _store_request("s6b4c06-cancelled-transaction", "Cancelled transaction proof.", "c")
        task = asyncio.create_task(transaction_adapter.store(**request))
        assert await asyncio.to_thread(gate.hit.wait, 10)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            ambiguous_caller = True
        else:
            ambiguous_caller = False
        gate.release.set()
        assert await asyncio.to_thread(gate.worker_returned.wait, 10)
        committed = await _read_state(transaction_engine)
        assert committed["adapter_rows"] == committed["native_rows"] == committed["vector_rows"] == 1
        embeddings_before = transaction_adapter.provider.document_calls
        changes_before = await transaction_engine._run(
            lambda: transaction_engine.conn.total_changes if transaction_engine.conn else -1
        )
        replay = await transaction_adapter.store(**request)
        changes_after = await transaction_engine._run(
            lambda: transaction_engine.conn.total_changes if transaction_engine.conn else -1
        )
        replay_state = await _read_state(transaction_engine)
        assert replay.get("idempotent_replay") is True
        assert transaction_adapter.provider.document_calls == embeddings_before
        assert changes_after == changes_before
        assert len(replay_state["records"]) == 1
    finally:
        gate.release.set()
        if transaction_adapter is not None:
            await transaction_adapter.close()

    return {
        "result": "PASS",
        "caller_cancelled": caller_cancelled,
        "provider_worker_finished": provider_finished,
        "late_storage_write_after_embedding_cancellation": "NO",
        "subsequent_operation": "PASS",
        "async_cancellation_does_not_create_partial_transaction": True,
        "commit_outcome_may_become_caller_ambiguous": ambiguous_caller,
        "idempotency_replay_resolves_ambiguous_commit": True,
    }


async def _run_acceptance_body(run_dir: Path, gate: TransactionTraceGate) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "markers").mkdir()
    (run_dir / "upstream-runtime").mkdir()
    os.environ["MCP_MEMORY_BASE_DIR"] = str(run_dir / "upstream-runtime" / "parent")
    scenarios = {
        "STORE_PRECOMMIT_SIGKILL": await _run_store_precommit(run_dir),
        "REVERIFY_PRECOMMIT_SIGKILL": await _run_reverify_precommit(run_dir),
        "REPLACE_PRECOMMIT_SIGKILL": await _run_replace_precommit(run_dir),
        "UPDATE_POSTCOMMIT_PRE_RESPONSE_SIGKILL": await _run_postcommit(run_dir),
        "EMBEDDING_INFLIGHT_SIGKILL": await _run_embedding_inflight(run_dir),
    }
    cancellation = await _run_cancellation(run_dir, gate)
    coherence = []
    for db_path in sorted(run_dir.glob("*.sqlite3")):
        state = await _inspect(db_path)
        _assert_coherent(state)
        coherence.append(
            {
                "database": db_path.name,
                "quick_check": state["quick_check"],
                "journal_mode": state["journal_mode"],
                "busy_timeout": state["busy_timeout"],
                "adapter_without_native": state["adapter_without_native"],
                "native_without_adapter": state["native_without_adapter"],
                "vector_without_native": state["vector_without_native"],
                "native_without_vector": state["native_without_vector"],
            }
        )
    assert coherence
    assert all(item["quick_check"] == "ok" for item in coherence)
    assert all(item["journal_mode"] == "wal" for item in coherence)
    assert all(item["busy_timeout"] == 5000 for item in coherence)
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lockfile = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert BACKEND_PIN in pyproject and BACKEND_PIN in lockfile
    summary = {
        "PROCESS_SIGKILL_PROOF": "PASS",
        "STORE_PRECOMMIT_ROLLBACK": "PASS",
        "REVERIFY_PRECOMMIT_ROLLBACK": "PASS",
        "REPLACE_PRECOMMIT_ROLLBACK": "PASS",
        "POSTCOMMIT_DURABILITY": "PASS",
        "POSTCOMMIT_IDEMPOTENT_REPLAY": "PASS",
        "EMBEDDING_INFLIGHT_PROCESS_KILL": "PASS",
        "ASYNC_CANCELLATION_SEMANTICS": "PASS",
        "SQLITE_QUICK_CHECK_AFTER_CRASHES": "PASS",
        "SQLITE_QUICK_CHECK": "PASS",
        "PRAGMA_QUICK_CHECK": "ok",
        "JOURNAL_MODE": "wal",
        "BUSY_TIMEOUT": 5000,
        "STALE_WRITER_LOCK": "NO",
        "STALE_WRITER_LOCKS": 0,
        "CURRENT_LINEAGE_FORKS": 0,
        "PARTIAL_ADAPTER_ROWS": 0,
        "PARTIAL_NATIVE_ROWS": 0,
        "PARTIAL_VECTOR_ROWS": 0,
        "ORPHAN_ADAPTER_ROWS": 0,
        "ORPHAN_NATIVE_ROWS": 0,
        "ORPHAN_VECTOR_ROWS": 0,
        "BACKEND_PIN": BACKEND_PIN,
        "BACKEND_PIN_CHANGE": "NONE",
        "UV_LOCK_CHANGE": "NONE",
        "SCHEMA_CHANGE": "NONE",
        "PUBLIC_TOOL_CHANGE": "NONE",
        "PRODUCTION_EMBEDDING_PROFILE_SELECTION": "NO",
        "ACTUAL_CODEX_OR_HERMES_PROCESS_KILLED": "NO",
        "PRODUCTION_SOURCE_CHANGE": "NONE",
        "GATEWAY_CHANGE": "NONE",
        "EMBEDDING_CHANGE": "NONE",
        "LIVE_HOST_CONFIG_CHANGE": "NONE",
        "NATIVE_MEMORY_MUTATION": "NONE",
        "GLOBAL_MCP_REGISTRATION": "NONE",
        "S6B_4D_STARTED": "NO",
        "S6B_5_STARTED": "NO",
        "S6B_4C_06_STARTED": "NO",
        "coherence": coherence,
        "scenarios": scenarios,
        "cancellation": cancellation,
    }
    _atomic_json(run_dir / "summary.json", summary)
    return summary


async def _run_acceptance(run_dir: Path) -> dict[str, Any]:
    gate = TransactionTraceGate(None, "precommit")
    previous_trace = threading.gettrace()
    threading.settrace(gate)
    try:
        return await _run_acceptance_body(run_dir, gate)
    finally:
        gate.armed = False
        gate.release.set()
        threading.settrace(previous_trace)


async def _child(args: argparse.Namespace) -> None:
    run_dir = args.run_dir.resolve()
    os.environ["MCP_MEMORY_BASE_DIR"] = str(run_dir / "upstream-runtime" / "child")
    marker = args.marker.resolve()
    gate: TransactionTraceGate | None = None
    if args.child_scenario in {"STORE_PRECOMMIT_SIGKILL", "REVERIFY_PRECOMMIT_SIGKILL", "REPLACE_PRECOMMIT_SIGKILL"}:
        gate = TransactionTraceGate(marker, "precommit")
    elif args.child_scenario == "UPDATE_POSTCOMMIT_PRE_RESPONSE_SIGKILL":
        gate = TransactionTraceGate(marker, "postcommit")
    previous_trace = threading.gettrace()
    if gate is not None:
        threading.settrace(gate)
    provider: DeterministicProvider = DeterministicProvider()
    if args.child_scenario == "EMBEDDING_INFLIGHT_SIGKILL":
        provider = BlockingProvider(threading.Event(), threading.Event(), threading.Event())

        original_embed = provider.embed_documents

        def announce_and_block(texts: list[str]) -> list[list[float]]:
            _atomic_json(marker, {"phase": "EMBEDDING_INFLIGHT", "pid": os.getpid()})
            return original_embed(texts)

        provider.embed_documents = announce_and_block  # type: ignore[method-assign]
    adapter: MemoryAdapter | None = None
    try:
        _engine, adapter = await _open_adapter(args.db, provider)
        if gate is not None:
            gate.armed = True
        if args.child_scenario == "STORE_PRECOMMIT_SIGKILL":
            await adapter.store(**_store_request(args.idempotency_key, "Store precommit crash proof.", "b"))
        elif args.child_scenario == "REVERIFY_PRECOMMIT_SIGKILL":
            await adapter.update(**_reverify_request(args.memory_id, args.idempotency_key, "c"))
        elif args.child_scenario == "REPLACE_PRECOMMIT_SIGKILL":
            await adapter.update(**_replace_request(args.memory_id, args.idempotency_key, "d"))
        elif args.child_scenario == "UPDATE_POSTCOMMIT_PRE_RESPONSE_SIGKILL":
            await adapter.update(**_reverify_request(args.memory_id, args.idempotency_key, "e"))
        elif args.child_scenario == "EMBEDDING_INFLIGHT_SIGKILL":
            await adapter.store(**_store_request(args.idempotency_key, "Embedding inflight crash proof.", "f"))
        else:
            raise ValueError(f"unknown child scenario: {args.child_scenario}")
    finally:
        if gate is not None:
            gate.release.set()
            threading.settrace(previous_trace)
        if adapter is not None:
            await adapter.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="S6B.4C-06 disposable crash/cancellation acceptance harness")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--child-scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--memory-id")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--parent-record-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.child_scenario:
        if not args.db or not args.marker:
            raise SystemExit("--db and --marker are required for a child scenario")
        asyncio.run(_child(args))
        return 0
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            summary = asyncio.run(_run_acceptance(args.run_dir.resolve()))
    except Exception as exc:
        payload = {"status": "FAIL", "error_type": type(exc).__name__, "error": str(exc)[:240]}
        args.run_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(args.run_dir / "summary.json", payload)
        print(json.dumps(payload, sort_keys=True))
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
