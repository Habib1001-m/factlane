from __future__ import annotations

import asyncio
import base64
import json
import math
import uuid
from typing import Any
from urllib.parse import quote, urlencode

from .adapter import MemoryAdapter
from .contract import (
    LIFECYCLE_STATES,
    AdapterError,
    ScopeContext,
    canonical_json,
    contains_sensitive,
    digest,
    iso_now,
    parse_iso,
    validate_identifier,
    validate_scope,
)
from .owner_contract import validate_owner_page_size
from .owner_control import OwnerAdapterMaintenanceGate, OwnerControlService, owner_adapter_request
from .storage import _RECORD_COLUMNS


_SCOPE_FIELDS = ("scope", "project_id", "worktree_id", "workflow_id", "agent_id")
_GOVERNANCE_ACTIVE_STATES = ("CANDIDATE", "QUARANTINED", "STALE", "VALIDATED_CURRENT")
_SEARCH_MODES = frozenset({"TEXT", "SEMANTIC"})
_CHRONOLOGICAL_SORT_ORDERS = frozenset({"NEWEST", "OLDEST"})
_SEMANTIC_SORT_ORDER = "RELEVANCE"
_SEMANTIC_DEGRADATION_CODES = frozenset({"EMBEDDING_UNAVAILABLE", "PROFILE_MISMATCH", "SCHEMA_MISMATCH"})
_SEMANTIC_CANDIDATE_LIMIT = 400
_MAX_CURSOR_BYTES = 2048


def _metric(value: int | None, completeness: str, reason: str | None = None) -> dict[str, Any]:
    return {"value": value, "completeness": completeness, "reason": reason}


class OwnerOverviewService:
    """Owner-only aggregate reads over the governed FactLane database.

    This service is deliberately separate from the five-tool MCP surface. Aggregate
    database reads may cross exact scopes for the human Owner, while every returned
    record retains its complete five-field scope identity.
    """

    def __init__(self, owner_control: OwnerControlService) -> None:
        if not isinstance(owner_control, OwnerControlService):
            raise AdapterError(
                "WRITE_AUTHORIZATION_DENIED",
                "OwnerOverviewService requires a governed OwnerControlService",
            )
        owner_context = getattr(owner_control, "_owner_context", None)
        if (
            owner_context is None
            or owner_context.authorization_class != "DIRECT_OWNER_OPERATOR"
            or owner_context.contributor_class != "OWNER"
        ):
            raise AdapterError(
                "WRITE_AUTHORIZATION_DENIED",
                "OwnerOverviewService requires direct Owner operator authority",
            )
        self._owner = owner_control
        self._engine = owner_control._engine
        self._adapter = owner_control._adapter
        self._maintenance_gate = owner_control._maintenance_gate

    def _rebind_owner_adapter(self) -> None:
        if self._maintenance_gate.state != OwnerAdapterMaintenanceGate.REOPENING:
            raise AdapterError(
                "BACKEND_UNAVAILABLE",
                "Owner overview rebinding is allowed only during governed reopen",
            )
        self._engine = self._owner._engine
        self._adapter = self._owner._adapter

    @staticmethod
    def _request_id(value: str | None) -> str:
        if value is None:
            return str(uuid.uuid4())
        validated = validate_identifier(value, "request_id", required=True)
        assert validated is not None
        return validated

    @staticmethod
    def _exact_scope(
        scope: str | None,
        project_id: str | None,
        worktree_id: str | None,
        workflow_id: str | None,
        agent_id: str | None,
    ) -> ScopeContext | None:
        if scope is None:
            if any(value is not None for value in (project_id, worktree_id, workflow_id, agent_id)):
                raise AdapterError(
                    "INVALID_ENVELOPE",
                    "scope identity filters require one exact scope",
                )
            return None
        return validate_scope(scope, project_id, worktree_id, workflow_id, agent_id)

    @staticmethod
    def _validate_query(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 512:
            raise AdapterError("INVALID_ENVELOPE", "query must be a bounded non-empty string")
        return value.strip()

    @staticmethod
    def _validate_source_ref(value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.encode("utf-8")) > 256
            or "\n" in value
            or "\r" in value
            or contains_sensitive(value)
        ):
            raise AdapterError("INVALID_ENVELOPE", "source_ref filter is invalid")
        return value.strip()

    @staticmethod
    def _validate_date(value: str | None, field: str) -> str | None:
        if value is None:
            return None
        parsed = parse_iso(value, required=True)
        assert parsed is not None
        return value

    @staticmethod
    def _validate_chronological_sort(value: str | None) -> str:
        if value is None:
            return "NEWEST"
        if not isinstance(value, str) or value not in _CHRONOLOGICAL_SORT_ORDERS:
            raise AdapterError("INVALID_ENUM", "sort_order must be NEWEST or OLDEST")
        return value

    @staticmethod
    def _chronological_page(
        sort_order: str,
        after: tuple[str, str] | None,
    ) -> tuple[str, list[str], str]:
        if sort_order == "NEWEST":
            page_where = "WHERE created_at < ? OR (created_at = ? AND record_id > ?) " if after else ""
            order_by = "created_at DESC,record_id ASC"
        else:
            page_where = "WHERE created_at > ? OR (created_at = ? AND record_id > ?) " if after else ""
            order_by = "created_at ASC,record_id ASC"
        page_values = [after[0], after[0], after[1]] if after else []
        return page_where, page_values, order_by

    @staticmethod
    def _scope_dict(values: tuple[Any, ...] | list[Any]) -> dict[str, Any]:
        return dict(zip(_SCOPE_FIELDS, values, strict=True))

    @staticmethod
    def _scope_key(scope: dict[str, Any]) -> tuple[str, str, str, str, str]:
        return tuple(str(scope.get(field) or "") for field in _SCOPE_FIELDS)  # type: ignore[return-value]

    @staticmethod
    def _cursor_binding(filters: dict[str, Any]) -> str:
        return digest(filters)

    @classmethod
    def _encode_cursor(cls, filters: dict[str, Any], position: list[Any]) -> str:
        payload = {
            "v": 1,
            "binding": cls._cursor_binding(filters),
            "position": position,
        }
        encoded = base64.urlsafe_b64encode(canonical_json(payload).encode("utf-8")).decode("ascii").rstrip("=")
        if len(encoded.encode("ascii")) > _MAX_CURSOR_BYTES:
            raise AdapterError("INVALID_CURSOR", "Owner overview pagination cursor is oversized")
        return encoded

    @classmethod
    def _decode_cursor(cls, cursor: str | None, filters: dict[str, Any]) -> list[Any] | None:
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor or len(cursor.encode("utf-8")) > _MAX_CURSOR_BYTES:
            raise AdapterError("INVALID_CURSOR", "Owner overview pagination cursor is invalid")
        try:
            padding = "=" * (-len(cursor) % 4)
            raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError("INVALID_CURSOR", "Owner overview pagination cursor is invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {"v", "binding", "position"}:
            raise AdapterError("INVALID_CURSOR", "Owner overview pagination cursor is invalid")
        if payload.get("v") != 1 or payload.get("binding") != cls._cursor_binding(filters):
            raise AdapterError(
                "INVALID_CURSOR",
                "Owner overview pagination cursor does not match exact scope and filters",
            )
        position = payload.get("position")
        if not isinstance(position, list):
            raise AdapterError("INVALID_CURSOR", "Owner overview pagination cursor is invalid")
        return position

    @staticmethod
    def _detail_link(record: dict[str, Any]) -> str:
        scope = {
            "scope": record["scope"],
            "project_id": record.get("project_id"),
            "worktree_id": record.get("worktree_id"),
            "workflow_id": record.get("workflow_id"),
            "agent_id": record.get("agent_id"),
        }
        params: list[tuple[str, str]] = [("scope", str(scope["scope"]))]
        for field in _SCOPE_FIELDS[1:]:
            value = scope[field]
            if value is not None:
                params.append((field, str(value)))
        return f"/owner/memories/{quote(str(record['memory_id']), safe='')}?{urlencode(params)}"

    @staticmethod
    def _conflict_count_sql(*, project_only: bool = False) -> str:
        group_fields = ",".join(f"a.{field}" for field in _SCOPE_FIELDS)
        state_marks = ",".join("?" for _ in _GOVERNANCE_ACTIVE_STATES)
        grouped = (
            f"SELECT {group_fields},a.contradiction_key FROM adapter_records a "
            f"WHERE a.lifecycle_state IN ({state_marks}) "
            + ("AND a.project_id IS NOT NULL " if project_only else "")
            + f"GROUP BY {group_fields},a.contradiction_key "
            "HAVING SUM(CASE WHEN a.contradiction_state IN ('UNRESOLVED','QUARANTINED') "
            "OR a.lifecycle_state IN ('STALE','QUARANTINED') THEN 1 ELSE 0 END) > 0"
        )
        return grouped

    def _overview_snapshot_sync(self) -> dict[str, Any]:
        conn = self._engine.conn
        if conn is None:
            raise AdapterError("BACKEND_UNAVAILABLE", "Owner overview database is not open")
        conn.execute("BEGIN")
        try:
            total_row = conn.execute(
                "SELECT COUNT(DISTINCT memory_id),COUNT(*),"
                "COUNT(DISTINCT CASE WHEN lifecycle_state='CANDIDATE' THEN memory_id END) "
                "FROM adapter_records"
            ).fetchone()
            lifecycle_rows = conn.execute(
                "WITH ranked AS ("
                "SELECT lifecycle_state,ROW_NUMBER() OVER (PARTITION BY memory_id "
                "ORDER BY revision DESC,created_at DESC,record_id DESC) AS owner_row_rank "
                "FROM adapter_records) "
                "SELECT lifecycle_state,COUNT(*) FROM ranked WHERE owner_row_rank=1 GROUP BY lifecycle_state"
            ).fetchall()
            lifecycle_counts = {state: 0 for state in sorted(LIFECYCLE_STATES)}
            for lifecycle_state, count in lifecycle_rows:
                lifecycle_counts[str(lifecycle_state)] = int(count)
            conflict_total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM ({self._conflict_count_sql()})",
                    _GOVERNANCE_ACTIVE_STATES,
                ).fetchone()[0]
            )

            scope_rows = conn.execute(
                "SELECT scope,project_id,worktree_id,workflow_id,agent_id,"
                "COUNT(DISTINCT memory_id),COUNT(*),"
                "COUNT(DISTINCT CASE WHEN lifecycle_state='CANDIDATE' THEN memory_id END) "
                "FROM adapter_records GROUP BY scope,project_id,worktree_id,workflow_id,agent_id "
                "ORDER BY scope,COALESCE(project_id,''),COALESCE(worktree_id,''),"
                "COALESCE(workflow_id,''),COALESCE(agent_id,'')"
            ).fetchall()
            scope_conflict_rows = conn.execute(
                "SELECT scope,project_id,worktree_id,workflow_id,agent_id,COUNT(*) FROM ("
                + self._conflict_count_sql()
                + ") GROUP BY scope,project_id,worktree_id,workflow_id,agent_id",
                _GOVERNANCE_ACTIVE_STATES,
            ).fetchall()
            scope_conflicts = {
                tuple(row[:5]): int(row[5])
                for row in scope_conflict_rows
            }

            project_rows = conn.execute(
                "SELECT project_id,COUNT(DISTINCT memory_id),COUNT(*),"
                "COUNT(DISTINCT CASE WHEN lifecycle_state='CANDIDATE' THEN memory_id END) "
                "FROM adapter_records WHERE project_id IS NOT NULL GROUP BY project_id ORDER BY project_id"
            ).fetchall()
            project_conflict_rows = conn.execute(
                "SELECT project_id,COUNT(*) FROM ("
                + self._conflict_count_sql(project_only=True)
                + ") GROUP BY project_id ORDER BY project_id",
                _GOVERNANCE_ACTIVE_STATES,
            ).fetchall()
            project_conflicts = {str(row[0]): int(row[1]) for row in project_conflict_rows}

            scopes = []
            for row in scope_rows:
                scope_values = tuple(row[:5])
                scopes.append(
                    {
                        "scope": self._scope_dict(scope_values),
                        "logical_memories": int(row[5]),
                        "revisions": int(row[6]),
                        "candidates": int(row[7]),
                        "conflicts": scope_conflicts.get(scope_values, 0),
                    }
                )
            projects = [
                {
                    "project_id": str(row[0]),
                    "logical_memories": int(row[1]),
                    "revisions": int(row[2]),
                    "candidates": int(row[3]),
                    "conflicts": project_conflicts.get(str(row[0]), 0),
                }
                for row in project_rows
            ]
            return {
                "logical_memories": int(total_row[0]),
                "revisions": int(total_row[1]),
                "candidates": int(total_row[2]),
                "conflicts": conflict_total,
                "lifecycle": lifecycle_counts,
                "scopes": scopes,
                "projects": projects,
            }
        finally:
            conn.rollback()

    @owner_adapter_request
    async def overview(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        page_size = validate_owner_page_size(limit)
        filters = {"kind": "overview_scope_index", "sort": "exact_scope_asc"}
        position = self._decode_cursor(cursor, filters)
        after_key: tuple[str, str, str, str, str] | None = None
        if position is not None:
            if len(position) != 5 or not all(isinstance(value, str) for value in position):
                raise AdapterError("INVALID_CURSOR", "Owner overview pagination cursor is invalid")
            after_key = tuple(position)  # type: ignore[assignment]

        snapshot = await self._engine._run(self._overview_snapshot_sync)
        scopes = snapshot["scopes"]
        if after_key is not None:
            scopes = [item for item in scopes if self._scope_key(item["scope"]) > after_key]
        visible = scopes[:page_size]
        has_more = len(scopes) > page_size
        next_cursor = None
        if has_more and visible:
            next_cursor = self._encode_cursor(filters, list(self._scope_key(visible[-1]["scope"])))

        return {
            "status": "OK",
            "request_id": self._request_id(request_id),
            "snapshot_at": iso_now(),
            "totals": {
                "logical_memories": _metric(snapshot["logical_memories"], "EXACT"),
                "revisions": _metric(snapshot["revisions"], "EXACT"),
                "candidates": _metric(snapshot["candidates"], "EXACT"),
                "conflicts": _metric(snapshot["conflicts"], "EXACT"),
            },
            "lifecycle_breakdown": {
                state: _metric(count, "EXACT") for state, count in snapshot["lifecycle"].items()
            },
            "scope_index": {
                "total": _metric(len(snapshot["scopes"]), "EXACT"),
                "page_count": len(visible),
                "items": visible,
                "next_cursor": next_cursor,
            },
            "project_breakdown": snapshot["projects"],
        }

    @staticmethod
    def _filter_sql(
        *,
        lifecycle_state: str | None,
        source_ref: str | None,
        created_after: str | None,
        created_before: str | None,
        alias: str,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if lifecycle_state is not None:
            clauses.append(f"{alias}.lifecycle_state=?")
            values.append(lifecycle_state)
        if source_ref is not None:
            clauses.append(f"json_extract({alias}.source_provenance,'$.source_ref')=?")
            values.append(source_ref)
        if created_after is not None:
            clauses.append(f"julianday({alias}.created_at)>=julianday(?)")
            values.append(created_after)
        if created_before is not None:
            clauses.append(f"julianday({alias}.created_at)<=julianday(?)")
            values.append(created_before)
        return clauses, values

    def _discovery_sync(
        self,
        *,
        scope: ScopeContext | None,
        lifecycle_state: str | None,
        source_ref: str | None,
        created_after: str | None,
        created_before: str | None,
        sort_order: str,
        page_size: int,
        after: tuple[str, str] | None,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        conn = self._engine.conn
        if conn is None:
            raise AdapterError("BACKEND_UNAVAILABLE", "Owner overview database is not open")
        if scope is None:
            scope_where, scope_values = "1=1", []
        else:
            scope_where, scope_values = self._engine._scope_where(scope)
        filter_clauses, filter_values = self._filter_sql(
            lifecycle_state=lifecycle_state,
            source_ref=source_ref,
            created_after=created_after,
            created_before=created_before,
            alias="r",
        )
        filtered_where = "r.owner_row_rank=1"
        if filter_clauses:
            filtered_where += " AND " + " AND ".join(filter_clauses)
        ranked = (
            "WITH ranked AS ("
            "SELECT a.*,ROW_NUMBER() OVER (PARTITION BY a.memory_id "
            "ORDER BY a.revision DESC,a.created_at DESC,a.record_id DESC) AS owner_row_rank "
            f"FROM adapter_records a WHERE {scope_where}),"
            f"filtered AS (SELECT * FROM ranked r WHERE {filtered_where}) "
        )
        base_values = [*scope_values, *filter_values]

        conn.execute("BEGIN")
        try:
            count_row = conn.execute(
                ranked
                + "SELECT (SELECT COUNT(*) FROM filtered),"
                "(SELECT COUNT(*) FROM ranked all_rows "
                "WHERE all_rows.memory_id IN (SELECT memory_id FROM filtered))",
                base_values,
            ).fetchone()
            page_where, page_values, order_by = self._chronological_page(sort_order, after)
            rows = conn.execute(
                ranked
                + f"SELECT * FROM filtered {page_where}"
                + f"ORDER BY {order_by} LIMIT ?",
                [*base_values, *page_values, page_size + 1],
            ).fetchall()
            records = [self._engine._row_to_dict(row[:-1]) for row in rows]
            return int(count_row[0]), int(count_row[1]), records
        finally:
            conn.rollback()

    @owner_adapter_request
    async def discover(
        self,
        *,
        scope: str | None = None,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        lifecycle_state: str | None = None,
        source_ref: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        sort_order: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        scope_context = self._exact_scope(scope, project_id, worktree_id, workflow_id, agent_id)
        if lifecycle_state is not None and lifecycle_state not in LIFECYCLE_STATES:
            raise AdapterError("INVALID_ENUM", "lifecycle_state filter is invalid")
        source_ref_value = self._validate_source_ref(source_ref)
        after_value = self._validate_date(created_after, "created_after")
        before_value = self._validate_date(created_before, "created_before")
        sort_value = self._validate_chronological_sort(sort_order)
        if after_value is not None and before_value is not None:
            after_dt = parse_iso(after_value, required=True)
            before_dt = parse_iso(before_value, required=True)
            assert after_dt is not None and before_dt is not None
            if after_dt > before_dt:
                raise AdapterError("INVALID_ENVELOPE", "created_after must not be later than created_before")
        page_size = validate_owner_page_size(limit)
        filters = {
            "kind": "overview_discovery",
            "scope": scope_context.to_dict() if scope_context else None,
            "lifecycle_state": lifecycle_state,
            "source_ref": source_ref_value,
            "created_after": after_value,
            "created_before": before_value,
            "sort_order": sort_value,
        }
        position = self._decode_cursor(cursor, filters)
        after: tuple[str, str] | None = None
        if position is not None:
            if len(position) != 2 or not all(isinstance(value, str) and value for value in position):
                raise AdapterError("INVALID_CURSOR", "Owner overview pagination cursor is invalid")
            after = (position[0], position[1])

        filtered_total, revision_total, rows = await self._engine._run(
            lambda: self._discovery_sync(
                scope=scope_context,
                lifecycle_state=lifecycle_state,
                source_ref=source_ref_value,
                created_after=after_value,
                created_before=before_value,
                sort_order=sort_value,
                page_size=page_size,
                after=after,
            )
        )
        visible = rows[:page_size]
        has_more = len(rows) > page_size
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = self._encode_cursor(filters, [last["created_at"], last["record_id"]])
        items = [self._public_item(row, rank=index) for index, row in enumerate(visible, start=1)]
        return {
            "status": "OK",
            "request_id": self._request_id(request_id),
            "scope": scope_context.to_dict() if scope_context else None,
            "filters": {
                "lifecycle_state": lifecycle_state,
                "source_ref": source_ref_value,
                "created_after": after_value,
                "created_before": before_value,
                "sort_order": sort_value,
            },
            "counts": {
                "filtered_logical": _metric(filtered_total, "EXACT"),
                "filtered_revisions": _metric(revision_total, "EXACT"),
                "page_rows": _metric(len(items), "EXACT"),
            },
            "items": items,
            "next_cursor": next_cursor,
        }

    def _text_search_sync(
        self,
        *,
        query: str,
        scope: ScopeContext | None,
        lifecycle_state: str | None,
        source_ref: str | None,
        created_after: str | None,
        created_before: str | None,
        sort_order: str,
        page_size: int,
        after: tuple[str, str] | None,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        conn = self._engine.conn
        if conn is None:
            raise AdapterError("BACKEND_UNAVAILABLE", "Owner overview database is not open")
        if scope is None:
            scope_where, scope_values = "1=1", []
        else:
            scope_where, scope_values = self._engine._scope_where(scope)
        filter_clauses, filter_values = self._filter_sql(
            lifecycle_state=lifecycle_state,
            source_ref=source_ref,
            created_after=created_after,
            created_before=created_before,
            alias="r",
        )
        filter_clauses.insert(0, "instr(lower(r.fact),lower(?))>0")
        filter_values.insert(0, query)
        ranked = (
            "WITH ranked AS ("
            "SELECT a.*,ROW_NUMBER() OVER (PARTITION BY a.memory_id "
            "ORDER BY a.revision DESC,a.created_at DESC,a.record_id DESC) AS owner_row_rank "
            f"FROM adapter_records a WHERE {scope_where}),"
            "filtered AS (SELECT * FROM ranked r WHERE r.owner_row_rank=1 AND "
            + " AND ".join(filter_clauses)
            + ") "
        )
        base_values = [*scope_values, *filter_values]

        conn.execute("BEGIN")
        try:
            count_row = conn.execute(
                ranked
                + "SELECT (SELECT COUNT(*) FROM filtered),"
                "(SELECT COUNT(*) FROM ranked all_rows "
                "WHERE all_rows.memory_id IN (SELECT memory_id FROM filtered))",
                base_values,
            ).fetchone()
            page_where, page_values, order_by = self._chronological_page(sort_order, after)
            rows = conn.execute(
                ranked
                + f"SELECT * FROM filtered {page_where}"
                + f"ORDER BY {order_by} LIMIT ?",
                [*base_values, *page_values, page_size + 1],
            ).fetchall()
            records = [self._engine._row_to_dict(row[:-1]) for row in rows]
            return int(count_row[0]), int(count_row[1]), records
        finally:
            conn.rollback()

    def _semantic_search_sync(
        self,
        *,
        vector: list[float],
        scope: ScopeContext | None,
        lifecycle_state: str | None,
        source_ref: str | None,
        created_after: str | None,
        created_before: str | None,
    ) -> list[tuple[dict[str, Any], float]]:
        conn = self._engine.conn
        if conn is None:
            raise AdapterError("BACKEND_UNAVAILABLE", "Owner overview database is not open")
        self._engine.require_embedding_compatibility()
        if scope is None:
            scope_where, scope_values = "1=1", []
            eligible_scope_where, eligible_scope_values = "1=1", []
        else:
            scope_where, scope_values = self._engine._scope_where(scope)
            eligible_scope_where, eligible_scope_values = self._engine._scope_where(scope, alias="eligible")
        filter_clauses, filter_values = self._filter_sql(
            lifecycle_state=lifecycle_state,
            source_ref=source_ref,
            created_after=created_after,
            created_before=created_before,
            alias="a",
        )
        extra = ""
        if filter_clauses:
            extra = " AND " + " AND ".join(filter_clauses)
        eligible_filter_clauses, eligible_filter_values = self._filter_sql(
            lifecycle_state=lifecycle_state,
            source_ref=source_ref,
            created_after=created_after,
            created_before=created_before,
            alias="eligible",
        )
        eligible_extra = ""
        if eligible_filter_clauses:
            eligible_extra = " AND " + " AND ".join(eligible_filter_clauses)

        from sqlite_vec import serialize_float32

        columns = ", ".join(f"a.{column}" for column in _RECORD_COLUMNS)
        rows = conn.execute(
            f"SELECT {columns},e.distance FROM adapter_records a "
            "JOIN memories m ON m.content_hash=a.native_content_hash AND m.deleted_at IS NULL "
            "JOIN (SELECT rowid,distance FROM memory_embeddings "
            "WHERE content_embedding MATCH ? AND rowid IN ("
            "SELECT native.id FROM memories native "
            "JOIN adapter_records eligible ON eligible.native_content_hash=native.content_hash "
            f"WHERE native.deleted_at IS NULL AND {eligible_scope_where}{eligible_extra} "
            "AND NOT EXISTS (SELECT 1 FROM adapter_records newer "
            "WHERE newer.memory_id=eligible.memory_id AND newer.revision>eligible.revision)) "
            "AND k = ? AND store = ?) e ON e.rowid=m.id "
            f"WHERE {scope_where}{extra} "
            "AND NOT EXISTS (SELECT 1 FROM adapter_records newer "
            "WHERE newer.memory_id=a.memory_id AND newer.revision>a.revision) "
            "ORDER BY e.distance ASC,a.record_id ASC LIMIT ?",
            [
                serialize_float32(vector),
                *eligible_scope_values,
                *eligible_filter_values,
                _SEMANTIC_CANDIDATE_LIMIT,
                self._engine.embedding_store_key,
                *scope_values,
                *filter_values,
                _SEMANTIC_CANDIDATE_LIMIT,
            ],
        ).fetchall()
        return [(self._engine._row_to_dict(row[:-1]), float(row[-1])) for row in rows]

    @staticmethod
    def _validate_semantic_vector(vector: Any, expected_dimension: int) -> list[float]:
        if not isinstance(vector, list) or len(vector) != expected_dimension:
            raise AdapterError("PROFILE_MISMATCH", "embedding provider returned an incompatible query vector")
        normalized: list[float] = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise AdapterError("PROFILE_MISMATCH", "embedding provider returned an invalid query vector")
            normalized.append(float(value))
        return normalized

    @staticmethod
    def _public_item(record: dict[str, Any], *, relevance_score: float | None = None, rank: int | None = None) -> dict[str, Any]:
        item = MemoryAdapter._public(record, relevance_score=relevance_score, retrieval_rank=rank)
        item["detail_link"] = OwnerOverviewService._detail_link(record)
        return item

    async def _text_response(
        self,
        *,
        query: str,
        requested_mode: str,
        degraded_reason: str | None,
        scope: ScopeContext | None,
        lifecycle_state: str | None,
        source_ref: str | None,
        created_after: str | None,
        created_before: str | None,
        sort_order: str,
        page_size: int,
        cursor: str | None,
        request_id: str | None,
    ) -> dict[str, Any]:
        filters = {
            "kind": "overview_search",
            "requested_mode": requested_mode,
            "effective_mode": "TEXT",
            "query": query,
            "scope": scope.to_dict() if scope else None,
            "lifecycle_state": lifecycle_state,
            "source_ref": source_ref,
            "created_after": created_after,
            "created_before": created_before,
            "sort_order": sort_order,
        }
        position = self._decode_cursor(cursor, filters)
        after: tuple[str, str] | None = None
        if position is not None:
            if len(position) != 2 or not all(isinstance(value, str) and value for value in position):
                raise AdapterError("INVALID_CURSOR", "Owner overview pagination cursor is invalid")
            after = (position[0], position[1])
        filtered_total, revision_total, rows = await self._engine._run(
            lambda: self._text_search_sync(
                query=query,
                scope=scope,
                lifecycle_state=lifecycle_state,
                source_ref=source_ref,
                created_after=created_after,
                created_before=created_before,
                sort_order=sort_order,
                page_size=page_size,
                after=after,
            )
        )
        visible = rows[:page_size]
        has_more = len(rows) > page_size
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = self._encode_cursor(filters, [last["created_at"], last["record_id"]])
        items = [self._public_item(row, rank=index) for index, row in enumerate(visible, start=1)]
        degraded = degraded_reason is not None
        return {
            "status": "DEGRADED" if degraded else "OK",
            "request_id": self._request_id(request_id),
            "scope": scope.to_dict() if scope else None,
            "filters": {
                "lifecycle_state": lifecycle_state,
                "source_ref": source_ref,
                "created_after": created_after,
                "created_before": created_before,
                "sort_order": sort_order,
            },
            "search": {
                "requested_mode": requested_mode,
                "effective_mode": "TEXT",
                "degraded": degraded,
                "degradation": degraded_reason,
            },
            "counts": {
                "filtered_logical": _metric(filtered_total, "EXACT"),
                "filtered_revisions": _metric(revision_total, "EXACT"),
                "page_rows": _metric(len(items), "EXACT"),
            },
            "items": items,
            "next_cursor": next_cursor,
        }

    async def _semantic_response(
        self,
        *,
        query: str,
        scope: ScopeContext | None,
        lifecycle_state: str | None,
        source_ref: str | None,
        created_after: str | None,
        created_before: str | None,
        page_size: int,
        cursor: str | None,
        request_id: str | None,
    ) -> dict[str, Any]:
        try:
            self._engine.require_embedding_compatibility()
            raw_vector = await asyncio.to_thread(self._adapter.provider.embed_query, query)
            vector = self._validate_semantic_vector(raw_vector, int(self._engine.profile.output_dimension))
            scored = await self._engine._run(
                lambda: self._semantic_search_sync(
                    vector=vector,
                    scope=scope,
                    lifecycle_state=lifecycle_state,
                    source_ref=source_ref,
                    created_after=created_after,
                    created_before=created_before,
                )
            )
        except AdapterError as exc:
            if exc.code not in _SEMANTIC_DEGRADATION_CODES:
                raise
            return await self._text_response(
                query=query,
                requested_mode="SEMANTIC",
                degraded_reason=exc.code,
                scope=scope,
                lifecycle_state=lifecycle_state,
                source_ref=source_ref,
                created_after=created_after,
                created_before=created_before,
                sort_order="NEWEST",
                page_size=page_size,
                cursor=cursor,
                request_id=request_id,
            )

        filters = {
            "kind": "overview_search",
            "requested_mode": "SEMANTIC",
            "effective_mode": "SEMANTIC",
            "query": query,
            "scope": scope.to_dict() if scope else None,
            "lifecycle_state": lifecycle_state,
            "source_ref": source_ref,
            "created_after": created_after,
            "created_before": created_before,
            "sort_order": _SEMANTIC_SORT_ORDER,
        }
        position = self._decode_cursor(cursor, filters)
        after: tuple[float, str] | None = None
        if position is not None:
            if (
                len(position) != 2
                or isinstance(position[0], bool)
                or not isinstance(position[0], (int, float))
                or not math.isfinite(float(position[0]))
                or not isinstance(position[1], str)
                or not position[1]
            ):
                raise AdapterError("INVALID_CURSOR", "Owner overview pagination cursor is invalid")
            after = (float(position[0]), position[1])
        bounded_match_count = len(scored)
        if after is not None:
            scored = [item for item in scored if (item[1], str(item[0]["record_id"])) > after]
        visible = scored[:page_size]
        has_more = len(scored) > page_size
        next_cursor = None
        if has_more and visible:
            last_record, last_distance = visible[-1]
            next_cursor = self._encode_cursor(filters, [last_distance, last_record["record_id"]])
        items = [
            self._public_item(
                record,
                relevance_score=round(max(0.0, 1.0 - distance / 2.0), 6),
                rank=index,
            )
            for index, (record, distance) in enumerate(visible, start=1)
        ]
        return {
            "status": "OK",
            "request_id": self._request_id(request_id),
            "scope": scope.to_dict() if scope else None,
            "filters": {
                "lifecycle_state": lifecycle_state,
                "source_ref": source_ref,
                "created_after": created_after,
                "created_before": created_before,
                "sort_order": _SEMANTIC_SORT_ORDER,
            },
            "search": {
                "requested_mode": "SEMANTIC",
                "effective_mode": "SEMANTIC",
                "degraded": False,
                "degradation": None,
            },
            "counts": {
                "filtered_logical": _metric(bounded_match_count, "PARTIAL", "SEMANTIC_TOP_K_BOUNDED"),
                "filtered_revisions": _metric(None, "UNKNOWN", "SEMANTIC_TOP_K_BOUNDED"),
                "page_rows": _metric(len(items), "EXACT"),
            },
            "items": items,
            "next_cursor": next_cursor,
        }

    @owner_adapter_request
    async def search(
        self,
        *,
        query: str,
        search_mode: str = "TEXT",
        scope: str | None = None,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        lifecycle_state: str | None = None,
        source_ref: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        sort_order: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        query_value = self._validate_query(query)
        if not isinstance(search_mode, str) or search_mode.upper() not in _SEARCH_MODES:
            raise AdapterError("INVALID_ENUM", "search_mode must be TEXT or SEMANTIC")
        mode = search_mode.upper()
        if mode == "SEMANTIC":
            if sort_order is not None and sort_order != _SEMANTIC_SORT_ORDER:
                raise AdapterError("INVALID_ENUM", "semantic sort_order must be RELEVANCE")
            sort_value = _SEMANTIC_SORT_ORDER
        else:
            sort_value = self._validate_chronological_sort(sort_order)
        scope_context = self._exact_scope(scope, project_id, worktree_id, workflow_id, agent_id)
        if lifecycle_state is not None and lifecycle_state not in LIFECYCLE_STATES:
            raise AdapterError("INVALID_ENUM", "lifecycle_state filter is invalid")
        source_ref_value = self._validate_source_ref(source_ref)
        after_value = self._validate_date(created_after, "created_after")
        before_value = self._validate_date(created_before, "created_before")
        if after_value is not None and before_value is not None:
            after_dt = parse_iso(after_value, required=True)
            before_dt = parse_iso(before_value, required=True)
            assert after_dt is not None and before_dt is not None
            if after_dt > before_dt:
                raise AdapterError("INVALID_ENVELOPE", "created_after must not be later than created_before")
        page_size = validate_owner_page_size(limit)

        if mode == "SEMANTIC":
            return await self._semantic_response(
                query=query_value,
                scope=scope_context,
                lifecycle_state=lifecycle_state,
                source_ref=source_ref_value,
                created_after=after_value,
                created_before=before_value,
                page_size=page_size,
                cursor=cursor,
                request_id=request_id,
            )
        return await self._text_response(
            query=query_value,
            requested_mode="TEXT",
            degraded_reason=None,
            scope=scope_context,
            lifecycle_state=lifecycle_state,
            source_ref=source_ref_value,
            created_after=after_value,
            created_before=before_value,
            sort_order=sort_value,
            page_size=page_size,
            cursor=cursor,
            request_id=request_id,
        )
