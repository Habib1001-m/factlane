from __future__ import annotations

import asyncio
import errno
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import factlane.storage as storage_module
from factlane import backend_compat
from factlane.backend_compat import (
    PINNED_BACKEND_COMMIT,
    PINNED_BACKEND_URL,
    PINNED_BACKEND_VERSION,
    assert_backend_class_contract,
    assert_pinned_backend_identity,
    execute_with_backend_retry,
)
from factlane.contract import AdapterError
from factlane.embeddings import EmbeddingProfile
from factlane.storage import MIN_SQLITE_VERSION, SQLiteVecEngine, assert_supported_sqlite_runtime


_FACTLANE_BACKEND_ENV_KEYS = (
    "MCP_MEMORY_STORAGE_BACKEND",
    "MCP_MEMORY_USE_ONNX",
    "MCP_EXTERNAL_EMBEDDING_URL",
    "MCP_SEMANTIC_DEDUP_ENABLED",
    "MCP_MEMORY_ALLOW_HASH_EMBEDDINGS",
    "MCP_HTTP_ENABLED",
    "MCP_SSE_MODE",
    "MCP_STREAMABLE_HTTP_MODE",
    "MCP_MDNS_ENABLED",
    "MCP_BACKUP_ENABLED",
    "MCP_CONSOLIDATION_ENABLED",
    "MCP_AUTO_EXTRACT_DEFAULT",
    "MCP_QUALITY_SYSTEM_ENABLED",
    "MCP_QUALITY_BOOST_ENABLED",
    "MCP_INSIGHT_CARDS_ENABLED",
)

_CLOUDFLARE_CREDENTIAL_KEYS = (
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_VECTORIZE_INDEX",
    "CLOUDFLARE_D1_DATABASE_ID",
)


def profile(dimension: int = 256) -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id=f"test-{dimension}",
        provider_kind="OLLAMA_LOCAL",
        base_model_identity="nomic-embed-text:latest",
        model_digest="0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
        source_dimension=768,
        output_dimension=dimension,
        normalization_policy="OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
        distance_metric="cosine",
        projection_version="ollama-dimensions-v1",
        document_prefix="search_document: ",
        query_prefix="search_query: ",
    )


def test_pinned_backend_exposes_required_sqlite_primitives(tmp_path) -> None:
    from mcp_memory_service.storage.sqlite_vec import SqliteVecMemoryStorage

    assert_pinned_backend_identity()
    assert_backend_class_contract(SqliteVecMemoryStorage)
    storage = SqliteVecMemoryStorage(str(tmp_path / "memory.db"))
    assert hasattr(storage, "_conn_lock")
    assert callable(storage._execute_with_retry)


def test_declared_backend_pin_matches_runtime_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    lock = (root / "uv.lock").read_text(encoding="utf-8")

    assert PINNED_BACKEND_VERSION == "11.10.0"
    assert f"mcp-memory-service.git@{PINNED_BACKEND_COMMIT}" in pyproject
    assert f"rev={PINNED_BACKEND_COMMIT}" in lock
    assert f"#{PINNED_BACKEND_COMMIT}" in lock


class FakeDistribution:
    def __init__(self, *, version: str = PINNED_BACKEND_VERSION, direct_url: dict | None = None) -> None:
        self.version = version
        self.direct_url = direct_url or {
            "url": PINNED_BACKEND_URL,
            "vcs_info": {
                "vcs": "git",
                "commit_id": PINNED_BACKEND_COMMIT,
                "requested_revision": PINNED_BACKEND_COMMIT,
            },
        }

    def read_text(self, name: str) -> str | None:
        if name != "direct_url.json":
            return None
        return json.dumps(self.direct_url)


@pytest.mark.parametrize(
    ("version", "direct_url"),
    [
        ("0.0.0", None),
        (
            PINNED_BACKEND_VERSION,
            {
                "url": "https://example.invalid/backend.git",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": PINNED_BACKEND_COMMIT,
                    "requested_revision": PINNED_BACKEND_COMMIT,
                },
            },
        ),
        (
            PINNED_BACKEND_VERSION,
            {
                "url": PINNED_BACKEND_URL,
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": "0" * 40,
                    "requested_revision": "0" * 40,
                },
            },
        ),
    ],
)
def test_backend_identity_drift_fails_closed(monkeypatch, version: str, direct_url: dict | None) -> None:
    monkeypatch.setattr(
        backend_compat,
        "distribution",
        lambda _: FakeDistribution(version=version, direct_url=direct_url),
    )
    with pytest.raises(AdapterError) as exc_info:
        assert_pinned_backend_identity()
    assert exc_info.value.code == "BACKEND_COMPATIBILITY_MISMATCH"


def test_backend_malformed_vcs_provenance_fails_closed(monkeypatch) -> None:
    backend = FakeDistribution()
    backend.read_text = lambda _: "[]"
    monkeypatch.setattr(backend_compat, "distribution", lambda _: backend)
    with pytest.raises(AdapterError) as exc_info:
        assert_pinned_backend_identity()
    assert exc_info.value.code == "BACKEND_COMPATIBILITY_MISMATCH"


@pytest.mark.parametrize("backend", ["cloudflare", "hybrid"])
def test_fresh_backend_import_wraps_cloudflare_configuration_exit(backend: str) -> None:
    env = os.environ.copy()
    env["MCP_MEMORY_STORAGE_BACKEND"] = backend
    for key in _CLOUDFLARE_CREDENTIAL_KEYS:
        env.pop(key, None)

    script = """
from factlane.backend_compat import load_pinned_sqlite_vec_storage
from factlane.contract import AdapterError

try:
    load_pinned_sqlite_vec_storage()
except AdapterError as exc:
    print(f"ADAPTER_ERROR:{exc.code}")
else:
    raise SystemExit("expected governed backend configuration failure")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "ADAPTER_ERROR:BACKEND_COMPATIBILITY_MISMATCH" in result.stdout


def test_embedding_initializer_signature_drift_fails_closed() -> None:
    class DriftedStorage:
        async def _initialize_embedding_model(self, unexpected):
            return None

        async def _execute_with_retry(self, operation, max_retries=5, initial_delay=0.2):
            return operation()

    with pytest.raises(AdapterError) as exc_info:
        assert_backend_class_contract(DriftedStorage)
    assert exc_info.value.code == "BACKEND_COMPATIBILITY_MISMATCH"


def test_retry_parameter_kind_drift_fails_closed() -> None:
    class DriftedStorage:
        async def _initialize_embedding_model(self):
            return None

        async def _execute_with_retry(self, *, operation, max_retries=5, initial_delay=0.2):
            return operation()

    with pytest.raises(AdapterError) as exc_info:
        assert_backend_class_contract(DriftedStorage)
    assert exc_info.value.code == "BACKEND_COMPATIBILITY_MISMATCH"


def test_bound_retry_parameter_kind_drift_fails_closed() -> None:
    class DriftedStorage:
        async def _execute_with_retry(self, *, operation, max_retries=5, initial_delay=0.2):
            return operation()

    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(execute_with_backend_retry(DriftedStorage(), lambda: 42))
    assert exc_info.value.code == "BACKEND_COMPATIBILITY_MISMATCH"


class FakeStorage:
    def __init__(self) -> None:
        self.calls = 0

    async def _execute_with_retry(self, operation, max_retries=5, initial_delay=0.2):
        self.calls += 1
        return operation()


def test_engine_delegates_db_execution_to_backend_retry() -> None:
    engine = SQLiteVecEngine("/tmp/factlane-delegation-test.sqlite", profile())
    engine.conn = sqlite3.connect(":memory:")
    engine.storage = FakeStorage()
    try:
        result = asyncio.run(engine._run(lambda value: value + 1, 41))
    finally:
        engine.conn.close()
    assert result == 42
    assert engine.storage.calls == 1


def test_sqlite_runtime_contract_accepts_declared_floor() -> None:
    assert MIN_SQLITE_VERSION == (3, 42, 0)
    assert_supported_sqlite_runtime(MIN_SQLITE_VERSION)


def test_sqlite_runtime_contract_rejects_below_floor() -> None:
    with pytest.raises(AdapterError) as exc_info:
        assert_supported_sqlite_runtime((3, 41, 9))
    assert exc_info.value.code == "BACKEND_COMPATIBILITY_MISMATCH"
    assert "SQLite >= 3.42.0" in exc_info.value.safe_message


def test_open_rejects_unsupported_sqlite_before_database_creation(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "unsupported-sqlite.db"
    monkeypatch.delenv("MCP_EXTERNAL_EMBEDDING_URL", raising=False)
    monkeypatch.delenv("MCP_MEMORY_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("MCP_HTTP_ENABLED", raising=False)
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 37, 2))
    engine = SQLiteVecEngine(str(db_path), profile())

    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(engine.open())

    assert exc_info.value.code == "BACKEND_COMPATIBILITY_MISMATCH"
    assert "linked runtime is 3.37.2" in exc_info.value.safe_message
    assert not db_path.exists()
    assert "MCP_MEMORY_STORAGE_BACKEND" not in os.environ
    assert "MCP_HTTP_ENABLED" not in os.environ


def test_external_embedding_denial_precedes_sqlite_runtime_gate(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MCP_EXTERNAL_EMBEDDING_URL", "https://example.invalid/embed")
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 37, 2))
    engine = SQLiteVecEngine(str(tmp_path / "forbidden-external.db"), profile())

    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(engine.open())

    assert exc_info.value.code == "ADMIN_OPERATION_DENIED"
    assert not (tmp_path / "forbidden-external.db").exists()


def test_open_close_preserves_absent_backend_environment(tmp_path, monkeypatch) -> None:
    for key in _FACTLANE_BACKEND_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "preserve-absent.db"), profile())
        await engine.open()
        try:
            assert engine.storage is not None
            assert engine.storage.semantic_dedup_enabled is False
        finally:
            await engine.close()

    asyncio.run(run())
    assert all(key not in os.environ for key in _FACTLANE_BACKEND_ENV_KEYS)


def test_open_close_preserves_preexisting_backend_environment(tmp_path, monkeypatch) -> None:
    hostile = {
        "MCP_MEMORY_STORAGE_BACKEND": "cloudflare",
        "MCP_MEMORY_USE_ONNX": "1",
        "MCP_EXTERNAL_EMBEDDING_URL": "",
        "MCP_SEMANTIC_DEDUP_ENABLED": "true",
        "MCP_MEMORY_ALLOW_HASH_EMBEDDINGS": "1",
        "MCP_HTTP_ENABLED": "true",
        "MCP_SSE_MODE": "1",
        "MCP_STREAMABLE_HTTP_MODE": "1",
        "MCP_MDNS_ENABLED": "true",
        "MCP_BACKUP_ENABLED": "true",
        "MCP_CONSOLIDATION_ENABLED": "true",
        "MCP_AUTO_EXTRACT_DEFAULT": "true",
        "MCP_QUALITY_SYSTEM_ENABLED": "true",
        "MCP_QUALITY_BOOST_ENABLED": "true",
        "MCP_INSIGHT_CARDS_ENABLED": "true",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)

    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "preserve-existing.db"), profile())
        await engine.open()
        try:
            assert engine.storage is not None
            assert engine.storage.semantic_dedup_enabled is False
        finally:
            await engine.close()

    asyncio.run(run())
    assert {key: os.environ.get(key) for key in hostile} == hostile


def test_concurrent_engines_preserve_backend_environment(tmp_path, monkeypatch) -> None:
    hostile = {
        "MCP_MEMORY_STORAGE_BACKEND": "cloudflare",
        "MCP_MEMORY_USE_ONNX": "1",
        "MCP_EXTERNAL_EMBEDDING_URL": "",
        "MCP_SEMANTIC_DEDUP_ENABLED": "true",
        "MCP_HTTP_ENABLED": "true",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)

    async def run_one(name: str) -> None:
        engine = SQLiteVecEngine(str(tmp_path / f"{name}.db"), profile())
        await engine.open()
        try:
            assert engine.storage is not None
            assert engine.storage.semantic_dedup_enabled is False
        finally:
            await engine.close()

    async def run() -> None:
        await asyncio.gather(run_one("concurrent-a"), run_one("concurrent-b"))

    asyncio.run(run())
    assert {key: os.environ.get(key) for key in hostile} == hostile


def test_failed_open_preserves_backend_environment(tmp_path, monkeypatch) -> None:
    for key in _FACTLANE_BACKEND_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MCP_MEMORY_STORAGE_BACKEND", "sentinel-backend")
    monkeypatch.setenv("MCP_HTTP_ENABLED", "sentinel-http")
    before = {key: os.environ.get(key) for key in _FACTLANE_BACKEND_ENV_KEYS}

    def fail_backend_load():
        raise RuntimeError("forced backend load failure")

    monkeypatch.setattr(storage_module, "load_pinned_sqlite_vec_storage", fail_backend_load)
    engine = SQLiteVecEngine(str(tmp_path / "failed-open.db"), profile())
    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(engine.open())

    assert exc_info.value.code == "BACKEND_UNAVAILABLE"
    assert {key: os.environ.get(key) for key in _FACTLANE_BACKEND_ENV_KEYS} == before


def test_runtime_sqlite_full_is_governed_as_backend_unavailable(tmp_path, monkeypatch) -> None:
    async def raise_sqlite_full(storage, operation):
        del storage, operation
        exc = sqlite3.OperationalError("database or disk is full")
        exc.sqlite_errorcode = sqlite3.SQLITE_FULL
        exc.sqlite_errorname = "SQLITE_FULL"
        raise exc

    monkeypatch.setattr(storage_module, "execute_with_backend_retry", raise_sqlite_full)
    engine = SQLiteVecEngine(str(tmp_path / "sqlite-full.db"), profile())
    engine.conn = object()  # type: ignore[assignment]
    engine.storage = object()

    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(engine._run(lambda: None))

    assert exc_info.value.code == "BACKEND_UNAVAILABLE"
    assert exc_info.value.safe_message == "backend storage is temporarily unavailable"
    assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)
    assert "database or disk is full" not in exc_info.value.safe_message


def test_open_reuses_backend_wal_and_busy_timeout(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "memory.db"), profile())
        await engine.open()
        try:
            assert engine.conn is not None
            assert engine.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert engine.conn.execute("PRAGMA synchronous").fetchone()[0] == 1
            assert engine.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
            assert engine._read_dimension() == 256
            tables = {
                row[0]
                for row in engine.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "adapter_records" in tables
            assert "adapter_meta" in tables
        finally:
            await engine.close()

    asyncio.run(run())


def test_open_rejects_hostile_required_sqlite_pragma_overrides(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "hostile-pragmas.db"
    monkeypatch.setenv(
        "MCP_MEMORY_SQLITE_PRAGMAS",
        "journal_mode=DELETE,synchronous=OFF,busy_timeout=1",
    )
    engine = SQLiteVecEngine(str(db_path), profile())

    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(engine.open())

    assert exc_info.value.code == "BACKEND_COMPATIBILITY_MISMATCH"
    assert "SQLite pragma" in exc_info.value.safe_message
    assert not db_path.exists()


def test_open_rejects_schema_qualified_hostile_pragma_before_db_creation(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "schema-qualified-hostile-pragmas.db"
    monkeypatch.setenv("MCP_MEMORY_SQLITE_PRAGMAS", "main.synchronous=OFF")
    engine = SQLiteVecEngine(str(db_path), profile())

    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(engine.open())

    assert exc_info.value.code == "BACKEND_COMPATIBILITY_MISMATCH"
    assert not db_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fcntl/flock maintenance exclusion test")
def test_maintenance_lock_noncontention_os_error_is_backend_unavailable(tmp_path, monkeypatch) -> None:
    import fcntl

    def fail_flock(*args, **kwargs) -> None:
        del args, kwargs
        raise OSError(errno.EIO, "synthetic lock I/O failure")

    monkeypatch.setattr(fcntl, "flock", fail_flock)
    engine = SQLiteVecEngine(str(tmp_path / "lock-io.db"), profile())
    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(engine.open())
    assert exc_info.value.code == "BACKEND_UNAVAILABLE"
    assert "maintenance coordination" in exc_info.value.safe_message
    assert not (tmp_path / "lock-io.db").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX maintenance-lease setup error injection")
def test_maintenance_lock_setup_permission_error_is_not_mislabeled_as_contention(
    tmp_path, monkeypatch
) -> None:
    import factlane.storage as storage_module

    original_open = storage_module.os.open
    expected_db = storage_module.os.path.realpath(str(tmp_path / "permission.db"))

    def fail_lock_open(path, *args, **kwargs):
        if storage_module.os.path.realpath(path) == expected_db:
            raise PermissionError(errno.EACCES, "synthetic lockfile permission failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "open", fail_lock_open)
    engine = SQLiteVecEngine(str(tmp_path / "permission.db"), profile())
    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(engine.open())
    assert exc_info.value.code == "BACKEND_UNAVAILABLE"
    assert exc_info.value.envelope()["audit"]["retryable"] is True
    assert not (tmp_path / "permission.db").exists()


def test_engine_lifecycle_rejects_double_open_and_reopen_after_close(tmp_path) -> None:
    async def run() -> None:
        engine = SQLiteVecEngine(str(tmp_path / "single-use.db"), profile())
        await engine.open()
        with pytest.raises(AdapterError) as already_open:
            await engine.open()
        assert already_open.value.code == "BACKEND_UNAVAILABLE"
        assert engine.conn is not None

        await engine.close()
        with pytest.raises(AdapterError) as already_closed:
            await engine.open()
        assert already_closed.value.code == "BACKEND_UNAVAILABLE"

    asyncio.run(run())
