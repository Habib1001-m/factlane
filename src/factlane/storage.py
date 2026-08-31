from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from .contract import AdapterError, ScopeContext, canonical_json, parse_iso
from .embeddings import EmbeddingProfile


_RECORD_COLUMNS = (
    "record_id",
    "memory_id",
    "revision",
    "parent_record_id",
    "scope",
    "project_id",
    "worktree_id",
    "workflow_id",
    "agent_id",
    "memory_type",
    "fact",
    "source_provenance",
    "source_timestamp",
    "created_at",
    "last_verified_at",
    "verified_by",
    "authority_role",
    "freshness_policy",
    "supersedes",
    "contradiction_key",
    "contradiction_state",
    "confidence",
    "tags",
    "lifecycle_state",
    "native_content_hash",
    "payload_fingerprint",
    "idempotency_key",
    "embedding_profile_id",
    "embedding_model_digest",
    "embedding_output_dimension",
)


class SQLiteVecEngine:
    """Small adapter-owned repository over the pinned SQLite-vec schema."""

    def __init__(self, db_path: str, profile: EmbeddingProfile) -> None:
        self.db_path = os.path.abspath(db_path)
        self.profile = profile
        self.storage: Any = None
        self.conn: sqlite3.Connection | None = None
        self.native_columns: set[str] = set()
        self._closed = False

    async def open(self) -> None:
        if os.environ.get("MCP_EXTERNAL_EMBEDDING_URL", "").strip():
            raise AdapterError("ADMIN_OPERATION_DENIED", "external embedding providers are disabled")
        os.environ["MCP_MEMORY_STORAGE_BACKEND"] = "sqlite_vec"
        os.environ["MCP_MEMORY_USE_ONNX"] = "0"
        os.environ["MCP_EXTERNAL_EMBEDDING_URL"] = ""
        os.environ["MCP_SEMANTIC_DEDUP_ENABLED"] = "false"
        os.environ["MCP_MEMORY_ALLOW_HASH_EMBEDDINGS"] = "0"
        os.environ["MCP_HTTP_ENABLED"] = "false"
        os.environ["MCP_SSE_MODE"] = "0"
        os.environ["MCP_STREAMABLE_HTTP_MODE"] = "0"
        os.environ["MCP_MDNS_ENABLED"] = "false"
        os.environ["MCP_BACKUP_ENABLED"] = "false"
        os.environ["MCP_CONSOLIDATION_ENABLED"] = "false"
        os.environ["MCP_AUTO_EXTRACT_DEFAULT"] = "false"
        os.environ["MCP_QUALITY_SYSTEM_ENABLED"] = "false"
        os.environ["MCP_QUALITY_BOOST_ENABLED"] = "false"
        os.environ["MCP_INSIGHT_CARDS_ENABLED"] = "false"
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        try:
            from mcp_memory_service.storage.sqlite_vec import SqliteVecMemoryStorage

            storage = SqliteVecMemoryStorage(
                self.db_path,
                embedding_model=self.profile.base_model_identity,
            )
            # Native startup receives the profile dimension; adapter code binds
            # the real provider before any memory operation.
            storage.embedding_dimension = self.profile.output_dimension
            storage.semantic_dedup_enabled = False

            async def defer_native_embedding() -> None:
                # The adapter owns embedding calls; native startup must not create
                # hash vectors or attempt a model download before provider binding.
                storage.embedding_model = None
                storage.embedding_dimension = self.profile.output_dimension
                storage.embedding_backend_degraded = False

            storage._initialize_embedding_model = defer_native_embedding
            await storage.initialize(strict_dimension_check=True)
            self.storage = storage
            self.conn = storage.conn
            actual_dimension = await self._run(self._read_dimension)
            if actual_dimension != self.profile.output_dimension:
                await self.close()
                raise AdapterError("SCHEMA_MISMATCH", "pilot database dimension does not match the embedding profile")
            self.native_columns = await self._run(self._read_native_columns)
            required = {"content_hash", "content", "metadata", "created_at", "updated_at"}
            if not required.issubset(self.native_columns):
                await self.close()
                raise AdapterError("SCHEMA_MISMATCH", "pinned backend schema is missing required columns")
            await self._run(self._create_adapter_schema)
            await self._run(self._check_profile)
        except AdapterError:
            raise
        except sqlite3.Error as exc:
            await self.close()
            raise AdapterError("BACKEND_CORRUPT", "SQLite-vec database could not be opened") from exc
        except Exception as exc:
            await self.close()
            raise AdapterError("BACKEND_UNAVAILABLE", "SQLite-vec backend could not be initialized") from exc

    async def _run(self, operation: Callable[..., Any], *args: Any) -> Any:
        if self.conn is None or self.storage is None:
            raise AdapterError("BACKEND_UNAVAILABLE", "backend is not open")

        execute_with_retry = getattr(self.storage, "_execute_with_retry", None)
        if not callable(execute_with_retry):
            raise AdapterError(
                "SCHEMA_MISMATCH",
                "pinned backend no longer exposes the required SQLite retry boundary",
            )

        try:
            return await execute_with_retry(lambda: operation(*args))
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise AdapterError("BACKEND_BUSY", "backend remained busy after bounded retry") from exc
            raise

    def _read_dimension(self) -> int | None:
        assert self.conn is not None
        row = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='memory_embeddings'").fetchone()
        if not row or not row[0]:
            return None
        import re

        match = re.search(r"FLOAT\[(\d+)\]", row[0])
        return int(match.group(1)) if match else None

    def _read_native_columns(self) -> set[str]:
        assert self.conn is not None
        return {row[1] for row in self.conn.execute("PRAGMA table_info(memories)").fetchall()}

    def _create_adapter_schema(self) -> None:
        assert self.conn is not None
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS adapter_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS adapter_records (
                record_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                parent_record_id TEXT,
                scope TEXT NOT NULL,
                project_id TEXT,
                worktree_id TEXT,
                workflow_id TEXT,
                agent_id TEXT,
                memory_type TEXT NOT NULL,
                fact TEXT NOT NULL,
                source_provenance TEXT NOT NULL,
                source_timestamp TEXT,
                created_at TEXT NOT NULL,
                last_verified_at TEXT,
                verified_by TEXT NOT NULL,
                authority_role TEXT NOT NULL,
                freshness_policy TEXT NOT NULL,
                supersedes TEXT NOT NULL,
                contradiction_key TEXT NOT NULL,
                contradiction_state TEXT NOT NULL,
                confidence REAL NOT NULL,
                tags TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                native_content_hash TEXT NOT NULL UNIQUE,
                payload_fingerprint TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                embedding_profile_id TEXT NOT NULL,
                embedding_model_digest TEXT NOT NULL,
                embedding_output_dimension INTEGER NOT NULL,
                CHECK (confidence >= 0.0 AND confidence <= 1.0)
            );
            CREATE INDEX IF NOT EXISTS idx_adapter_scope_current
                ON adapter_records(scope, project_id, worktree_id, workflow_id, agent_id, lifecycle_state);
            CREATE INDEX IF NOT EXISTS idx_adapter_memory_id
                ON adapter_records(memory_id, revision);
            CREATE INDEX IF NOT EXISTS idx_adapter_contradiction
                ON adapter_records(contradiction_key, lifecycle_state);
            """
        )
        self.conn.commit()

    def _check_profile(self) -> None:
        assert self.conn is not None
        expected = {
            "profile_id": self.profile.profile_id,
            "provider_kind": self.profile.provider_kind,
            "base_model_identity": self.profile.base_model_identity,
            "model_digest": self.profile.model_digest,
            "source_dimension": self.profile.source_dimension,
            "output_dimension": self.profile.output_dimension,
            "normalization_policy": self.profile.normalization_policy,
            "distance_metric": self.profile.distance_metric,
            "projection_version": self.profile.projection_version,
        }
        row = self.conn.execute("SELECT value FROM adapter_meta WHERE key='embedding_profile'").fetchone()
        if row:
            try:
                actual = json.loads(row[0])
            except json.JSONDecodeError as exc:
                raise AdapterError("SCHEMA_MISMATCH", "adapter profile metadata is invalid") from exc
            if actual != expected:
                raise AdapterError("PROFILE_MISMATCH", "pilot database contains a different embedding profile")
        else:
            self.conn.execute("INSERT INTO adapter_meta(key, value) VALUES (?, ?)", ("embedding_profile", canonical_json(expected)))
            self.conn.execute("INSERT INTO adapter_meta(key, value) VALUES (?, ?)", ("contract_version", "1"))
            self.conn.commit()

    @staticmethod
    def _scope_where(scope: ScopeContext, alias: str = "a") -> tuple[str, list[Any]]:
        fields = {
            "scope": scope.scope,
            "project_id": scope.project_id,
            "worktree_id": scope.worktree_id,
            "workflow_id": scope.workflow_id,
            "agent_id": scope.agent_id,
        }
        clauses: list[str] = []
        params: list[Any] = []
        for field, value in fields.items():
            if value is None:
                clauses.append(f"{alias}.{field} IS NULL")
            else:
                clauses.append(f"{alias}.{field} = ?")
                params.append(value)
        return " AND ".join(clauses), params

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        return dict(zip(_RECORD_COLUMNS, row, strict=True))

    def _select_sql(self, extra: str = "") -> str:
        columns = ", ".join(f"a.{column}" for column in _RECORD_COLUMNS)
        return f"SELECT {columns} FROM adapter_records a {extra}"

    async def get_record(self, memory_id: str, scope: ScopeContext, history: bool = False) -> list[dict[str, Any]]:
        where, params = self._scope_where(scope)
        lifecycle = "" if history else " AND a.lifecycle_state = 'VALIDATED_CURRENT'"

        def query() -> list[dict[str, Any]]:
            assert self.conn is not None
            rows = self.conn.execute(
                self._select_sql(f"WHERE a.memory_id = ? AND {where}{lifecycle} ORDER BY a.revision DESC"),
                [memory_id, *params],
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

        return await self._run(query)

    async def memory_exists_outside_scope(self, memory_id: str, scope: ScopeContext) -> bool:
        where, params = self._scope_where(scope)

        def query() -> bool:
            assert self.conn is not None
            row = self.conn.execute(
                f"SELECT 1 FROM adapter_records a WHERE a.memory_id = ? AND NOT ({where}) LIMIT 1",
                [memory_id, *params],
            ).fetchone()
            return row is not None

        return await self._run(query)

    async def find_idempotency(self, key: str) -> dict[str, Any] | None:
        def query() -> dict[str, Any] | None:
            assert self.conn is not None
            row = self.conn.execute(self._select_sql("WHERE a.idempotency_key = ?"), (key,)).fetchone()
            return self._row_to_dict(row) if row else None

        return await self._run(query)

    async def find_exact(self, native_content_hash_value: str) -> dict[str, Any] | None:
        def query() -> dict[str, Any] | None:
            assert self.conn is not None
            row = self.conn.execute(
                self._select_sql("WHERE a.native_content_hash = ? ORDER BY a.revision DESC LIMIT 1"),
                (native_content_hash_value,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

        return await self._run(query)

    async def find_contradictions(self, key: str, scope: ScopeContext) -> list[dict[str, Any]]:
        where, params = self._scope_where(scope)

        def query() -> list[dict[str, Any]]:
            assert self.conn is not None
            rows = self.conn.execute(
                self._select_sql(
                    f"WHERE a.contradiction_key = ? AND {where} "
                    "AND a.lifecycle_state = 'VALIDATED_CURRENT' "
                    "ORDER BY a.created_at, a.record_id"
                ),
                [key, *params],
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

        return await self._run(query)

    async def write_record(
        self,
        record: dict[str, Any],
        embedding: list[float],
        supersede_record_id: str | None = None,
    ) -> None:
        def transaction() -> None:
            assert self.conn is not None
            now = time.time()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                superseded_native_hash: str | None = None
                if supersede_record_id:
                    old = self.conn.execute(
                        "SELECT native_content_hash, lifecycle_state FROM adapter_records WHERE record_id = ?",
                        (supersede_record_id,),
                    ).fetchone()
                    if not old or old[1] != "VALIDATED_CURRENT":
                        raise AdapterError(
                            "VERSION_CONFLICT",
                            "expected revision is no longer the current lineage parent",
                        )
                    if record.get("parent_record_id") != supersede_record_id:
                        raise AdapterError("VERSION_CONFLICT", "update lineage parent does not match expected revision")
                    superseded_native_hash = str(old[0])

                values = [
                    record["record_id"],
                    record["memory_id"],
                    record["revision"],
                    record.get("parent_record_id"),
                    record["scope"],
                    record.get("project_id"),
                    record.get("worktree_id"),
                    record.get("workflow_id"),
                    record.get("agent_id"),
                    record["memory_type"],
                    record["fact"],
                    canonical_json(record["source_provenance"]),
                    record.get("source_timestamp"),
                    record["created_at"],
                    record.get("last_verified_at"),
                    record["verified_by"],
                    record["authority_role"],
                    canonical_json(record["freshness_policy"]),
                    canonical_json(record.get("supersedes", [])),
                    record["contradiction_key"],
                    record["contradiction_state"],
                    record["confidence"],
                    canonical_json(record.get("tags", [])),
                    record["lifecycle_state"],
                    record["native_content_hash"],
                    record["payload_fingerprint"],
                    record["idempotency_key"],
                    record["embedding_profile_id"],
                    record["embedding_model_digest"],
                    record["embedding_output_dimension"],
                ]
                placeholders = ",".join("?" for _ in _RECORD_COLUMNS)
                self.conn.execute(
                    f"INSERT INTO adapter_records ({','.join(_RECORD_COLUMNS)}) VALUES ({placeholders})",
                    values,
                )
                native_metadata = canonical_json(
                    {
                        "adapter_record_id": record["record_id"],
                        "adapter_memory_id": record["memory_id"],
                        "embedding_profile_id": record["embedding_profile_id"],
                        "lifecycle_state": record["lifecycle_state"],
                    }
                )
                native_values: dict[str, Any] = {
                    "content_hash": record["native_content_hash"],
                    "content": record["fact"],
                    "tags": ",".join(record.get("tags", [])),
                    "memory_type": record["memory_type"],
                    "metadata": native_metadata,
                    "created_at": parse_iso(record["created_at"], required=True).timestamp(),
                    "updated_at": parse_iso(record["created_at"], required=True).timestamp(),
                    "created_at_iso": record["created_at"],
                    "updated_at_iso": record["created_at"],
                    "store": self.profile.profile_id,
                }
                if "confidence" in self.native_columns:
                    native_values["confidence"] = record["confidence"]
                if "last_accessed" in self.native_columns:
                    native_values["last_accessed"] = None
                native_fields = [field for field in native_values if field in self.native_columns]
                native_sql = (
                    f"INSERT INTO memories ({','.join(native_fields)}) VALUES "
                    f"({','.join('?' for _ in native_fields)})"
                )
                cursor = self.conn.execute(native_sql, [native_values[field] for field in native_fields])
                rowid = cursor.lastrowid
                if rowid is None:
                    raise sqlite3.IntegrityError("native memory rowid was not returned")
                from sqlite_vec import serialize_float32

                self.conn.execute(
                    "INSERT INTO memory_embeddings (rowid, content_embedding, store) VALUES (?, ?, ?)",
                    (rowid, serialize_float32(embedding), self.profile.profile_id),
                )
                if supersede_record_id:
                    self.conn.execute(
                        "UPDATE adapter_records SET lifecycle_state='SUPERSEDED' WHERE record_id = ?",
                        (supersede_record_id,),
                    )
                    if "superseded_by" in self.native_columns:
                        assert superseded_native_hash is not None
                        self.conn.execute(
                            "UPDATE memories SET superseded_by = ? WHERE content_hash = ?",
                            (record["native_content_hash"], superseded_native_hash),
                        )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

        await self._run(transaction)

    async def compact_superseded_record(self, record_id: str) -> bool:
        """Compact one fully materialized superseded record into history."""

        def transaction() -> bool:
            assert self.conn is not None
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT lifecycle_state, native_content_hash FROM adapter_records WHERE record_id = ?",
                    (record_id,),
                ).fetchone()
                if not row:
                    raise AdapterError("NOT_FOUND", "record_id was not found")

                lifecycle_state, content_hash = row
                native = self.conn.execute(
                    "SELECT id, deleted_at FROM memories WHERE content_hash = ?",
                    (content_hash,),
                ).fetchone()
                native_id = native[0] if native else None
                vector = (
                    self.conn.execute(
                        "SELECT 1 FROM memory_embeddings WHERE rowid = ? AND store = ?",
                        (native_id, self.profile.profile_id),
                    ).fetchone()
                    if native_id is not None
                    else None
                )
                graph = self.conn.execute(
                    "SELECT 1 FROM memory_graph WHERE source_hash = ? OR target_hash = ? LIMIT 1",
                    (content_hash, content_hash),
                ).fetchone()

                if lifecycle_state == "HISTORICAL":
                    if native is None and graph is None:
                        self.conn.commit()
                        return False
                    raise AdapterError(
                        "COMPACTION_STATE_INVALID",
                        "historical record still has materialized state",
                    )
                if lifecycle_state != "SUPERSEDED":
                    raise AdapterError(
                        "COMPACTION_NOT_ELIGIBLE",
                        "only superseded records can be compacted",
                    )
                if native is None or native[1] is not None or vector is None:
                    raise AdapterError(
                        "COMPACTION_STATE_INVALID",
                        "superseded record has partial materialization",
                    )

                updated = self.conn.execute(
                    "UPDATE adapter_records SET lifecycle_state = 'HISTORICAL' "
                    "WHERE record_id = ? AND lifecycle_state = 'SUPERSEDED'",
                    (record_id,),
                )
                if updated.rowcount != 1:
                    raise AdapterError("COMPACTION_NOT_ELIGIBLE", "record is no longer superseded")
                self.conn.execute(
                    "DELETE FROM memory_embeddings WHERE rowid = ? AND store = ?",
                    (native_id, self.profile.profile_id),
                )
                self.conn.execute(
                    "DELETE FROM memory_graph WHERE source_hash = ? OR target_hash = ?",
                    (content_hash, content_hash),
                )
                deleted = self.conn.execute(
                    "DELETE FROM memories WHERE id = ? AND content_hash = ? AND deleted_at IS NULL",
                    (native_id, content_hash),
                )
                if deleted.rowcount != 1:
                    raise AdapterError("COMPACTION_STATE_INVALID", "native materialization changed during compaction")
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

        return await self._run(transaction)

    async def vector_candidates(
        self,
        vector: list[float],
        scope: ScopeContext,
        *,
        limit: int,
        history: bool,
    ) -> list[tuple[dict[str, Any], float]]:
        where, params = self._scope_where(scope)
        lifecycle = "" if history else " AND a.lifecycle_state IN ('VALIDATED_CURRENT', 'CANDIDATE')"

        def query() -> list[tuple[dict[str, Any], float]]:
            assert self.conn is not None
            from sqlite_vec import serialize_float32

            columns = ", ".join(f"a.{column}" for column in _RECORD_COLUMNS)
            rows = self.conn.execute(
                f"SELECT {columns}, e.distance FROM adapter_records a "
                "JOIN memories m ON m.content_hash = a.native_content_hash "
                "JOIN (SELECT rowid, distance FROM memory_embeddings "
                "WHERE content_embedding MATCH ? AND k = ? AND store = ?) e ON e.rowid = m.id "
                f"WHERE m.deleted_at IS NULL AND {where}{lifecycle} "
                "ORDER BY e.distance ASC LIMIT ?",
                [serialize_float32(vector), max(limit * 4, limit), self.profile.profile_id, *params, limit],
            ).fetchall()
            return [(self._row_to_dict(row[:-1]), float(row[-1])) for row in rows]

        return await self._run(query)

    async def keyword_candidates(
        self,
        query_text: str,
        scope: ScopeContext,
        *,
        limit: int,
        history: bool,
        exact: bool = False,
    ) -> list[dict[str, Any]]:
        where, params = self._scope_where(scope)
        lifecycle = "" if history else " AND a.lifecycle_state IN ('VALIDATED_CURRENT', 'CANDIDATE')"
        operator = "=" if exact else "LIKE"
        value = query_text.casefold() if exact else f"%{query_text.casefold()}%"

        def query() -> list[dict[str, Any]]:
            assert self.conn is not None
            rows = self.conn.execute(
                self._select_sql(
                    f"WHERE lower(a.fact) {operator} ? AND {where}{lifecycle} "
                    "ORDER BY a.created_at DESC, a.record_id LIMIT ?"
                ),
                [value, *params, limit],
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

        return await self._run(query)

    async def contradiction_summary(self, scope: ScopeContext) -> list[dict[str, Any]]:
        where, params = self._scope_where(scope)

        def query() -> list[dict[str, Any]]:
            assert self.conn is not None
            rows = self.conn.execute(
                "SELECT a.record_id, a.memory_id, a.contradiction_key, a.contradiction_state, "
                "a.lifecycle_state, a.authority_role, a.source_provenance "
                f"FROM adapter_records a WHERE {where} AND a.contradiction_state IN ('UNRESOLVED','QUARANTINED') "
                "ORDER BY a.created_at, a.record_id LIMIT 16",
                params,
            ).fetchall()
            return [
                {
                    "record_id": row[0],
                    "memory_id": row[1],
                    "contradiction_key": row[2],
                    "contradiction_state": row[3],
                    "lifecycle_state": row[4],
                    "authority_role": row[5],
                    "source_class": json.loads(row[6]).get("source_class"),
                }
                for row in rows
            ]

        return await self._run(query)

    async def status(self, scope: ScopeContext | None) -> dict[str, Any]:
        if scope:
            where, params = self._scope_where(scope)
            suffix = "WHERE " + where
        else:
            suffix, params = "", []

        def query() -> dict[str, Any]:
            assert self.conn is not None
            counts = {state: 0 for state in ("CANDIDATE", "VALIDATED_CURRENT", "SUPERSEDED", "STALE", "QUARANTINED", "HISTORICAL")}
            rows = self.conn.execute(
                f"SELECT lifecycle_state, COUNT(*) FROM adapter_records a {suffix} GROUP BY lifecycle_state",
                params,
            ).fetchall()
            for state, count in rows:
                if state in counts:
                    counts[state] = int(count)
            return {
                "available": True,
                "backend": "sqlite_vec",
                "profile": self.profile.to_dict(),
                "counts": counts,
                "native_columns": sorted(self.native_columns),
            }

        return await self._run(query)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.storage is not None:
            try:
                await self.storage.close()
            except Exception:
                pass
        self.conn = None
        self.storage = None
