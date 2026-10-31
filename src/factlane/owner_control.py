from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import timedelta
from typing import Any

from .adapter import MemoryAdapter
from .contract import (
    CONTRADICTION_STATES,
    LIFECYCLE_STATES,
    MEMORY_TYPES,
    AdapterError,
    ScopeContext,
    canonical_json,
    contains_sensitive,
    digest,
    is_fresh,
    iso_now,
    native_hash,
    parse_iso,
    validate_freshness,
    validate_identifier,
    validate_provenance,
    validate_scope,
)
from .owner_contract import (
    MAX_STATUS_EVENT_ROWS,
    MAX_STATUS_RESPONSE_BYTES,
    MAX_STATUS_SCOPE_ROWS,
    decode_page_cursor,
    encode_page_cursor,
    validate_group_token,
    validate_owner_page_size,
    validate_owner_reason,
)

_GOVERNANCE_ACTIVE_STATES = frozenset({"VALIDATED_CURRENT", "CANDIDATE", "STALE", "QUARANTINED"})
_RESOLVE_VERIFICATION_FIELDS = frozenset(
    {
        "source_provenance",
        "freshness_policy",
        "source_timestamp",
        "last_verified_at",
        "verified_by",
        "memory_type",
        "confidence",
        "tags",
        "subject",
    }
)
_MANUAL_RECHECK_METHODS = frozenset(
    {"OWNER_HASHED_SOURCE", "OWNER_REVIEWED_SOURCE", "TRUSTED_LOCAL_RESULT_REFERENCE"}
)
_MANUAL_RECHECK_CLAIMS = frozenset({"OBSERVED_HASH", "UNAVAILABLE", "INDETERMINATE"})
_STATUS_LIFECYCLE_STATES = (
    "CANDIDATE",
    "VALIDATED_CURRENT",
    "SUPERSEDED",
    "STALE",
    "QUARANTINED",
    "HISTORICAL",
    "REVOKED",
    "REJECTED",
)
_FRESHNESS_REVIEW_KEYS = (
    "ttl_stale",
    "manual_review",
    "on_change_source_change_required",
    "on_change_unavailable",
    "on_change_indeterminate",
)
_READMIT_VERIFICATION_FIELDS = frozenset(
    {
        "source_provenance",
        "freshness_policy",
        "source_timestamp",
        "last_verified_at",
        "verified_by",
        "confidence",
        "tags",
        "subject",
    }
)
_EVENT_SELECT = (
    "e.event_id,e.operation_id,e.action,e.occurred_at,e.prior_lifecycle_state,e.result_lifecycle_state,"
    "e.prior_contradiction_state,e.result_contradiction_state,e.reason_code,e.expected_revision,e.result_revision,"
    "e.related_record_id,e.related_memory_id,e.freshness_outcome,e.observation_origin,e.observed_at,e.accepted_at,"
    "e.freshness_generation,e.clock_anomaly"
)


class OwnerControlService:
    """Owner-local semantic surface over the governed FactLane core."""

    def __init__(self, adapter: MemoryAdapter) -> None:
        if not isinstance(adapter, MemoryAdapter):
            raise AdapterError("WRITE_AUTHORIZATION_DENIED", "OwnerControlService requires a governed MemoryAdapter")
        context = getattr(adapter, "_trusted_write_context", None)
        if (
            context is None
            or context.authorization_class != "DIRECT_OWNER_OPERATOR"
            or context.contributor_class != "OWNER"
        ):
            raise AdapterError(
                "WRITE_AUTHORIZATION_DENIED",
                "OwnerControlService requires direct Owner operator authority from the trusted launcher",
            )
        self._adapter = adapter
        self._engine = adapter.engine
        self._owner_context = context

    @staticmethod
    def _scope(
        scope: str,
        project_id: str | None,
        worktree_id: str | None,
        workflow_id: str | None,
        agent_id: str | None,
    ) -> ScopeContext:
        return validate_scope(scope, project_id, worktree_id, workflow_id, agent_id)

    @staticmethod
    def _request_id(value: str | None) -> str:
        if value is None:
            return str(uuid.uuid4())
        validated = validate_identifier(value, "request_id", required=True)
        assert validated is not None
        return validated

    @staticmethod
    def _uuid(value: str, field: str) -> str:
        if not isinstance(value, str):
            raise AdapterError("INVALID_ENVELOPE", f"{field} must be a UUID")
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError) as exc:
            raise AdapterError("INVALID_ENVELOPE", f"{field} must be a UUID") from exc
        return str(parsed)

    @staticmethod
    def _positive_revision(value: int, field: str = "expected_revision") -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise AdapterError("INVALID_ENVELOPE", f"{field} must be a positive integer")
        return value

    @staticmethod
    def _validate_filter(value: str | None, allowed: set[str] | frozenset[str], field: str) -> str | None:
        if value is None:
            return None
        if value not in allowed:
            raise AdapterError("INVALID_ENUM", f"{field} filter is invalid")
        return value

    @staticmethod
    def _decode_json_field(value: Any, fallback: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return fallback
        return value if value is not None else fallback

    async def _owner_record(self, record: dict[str, Any]) -> dict[str, Any]:
        freshness = await self._engine.freshness_state(record["record_id"], int(record["revision"]))
        projected = dict(record)
        projected["freshness_gate_state"] = freshness["freshness_gate_state"]
        return MemoryAdapter._public(projected)

    async def _group_for_key(self, scope: ScopeContext, contradiction_key: str) -> dict[str, Any]:
        observation = await self._engine.group_observation(scope, contradiction_key)
        return observation["public"]

    async def inventory(
        self,
        *,
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        lifecycle_state: str | None = None,
        contradiction_state: str | None = None,
        memory_type: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        scope_context = self._scope(scope, project_id, worktree_id, workflow_id, agent_id)
        page_size = validate_owner_page_size(limit)
        lifecycle_state = self._validate_filter(lifecycle_state, LIFECYCLE_STATES, "lifecycle_state")
        contradiction_state = self._validate_filter(
            contradiction_state, CONTRADICTION_STATES, "contradiction_state"
        )
        memory_type = self._validate_filter(memory_type, MEMORY_TYPES, "memory_type")
        filters = {
            "kind": "inventory",
            "lifecycle_state": lifecycle_state,
            "contradiction_state": contradiction_state,
            "memory_type": memory_type,
        }
        after = decode_page_cursor(cursor, scope_context, filters)

        def query() -> list[dict[str, Any]]:
            assert self._engine.conn is not None
            where, params = self._engine._scope_where(scope_context)
            clauses = [where]
            values: list[Any] = list(params)
            for field, value in (
                ("lifecycle_state", lifecycle_state),
                ("contradiction_state", contradiction_state),
                ("memory_type", memory_type),
            ):
                if value is not None:
                    clauses.append(f"a.{field}=?")
                    values.append(value)
            if after is not None:
                clauses.append("(a.created_at > ? OR (a.created_at = ? AND a.record_id > ?))")
                values.extend([after[0], after[0], after[1]])
            rows = self._engine.conn.execute(
                self._engine._select_sql(
                    f"WHERE {' AND '.join(clauses)} ORDER BY a.created_at,a.record_id LIMIT ?"
                ),
                [*values, page_size + 1],
            ).fetchall()
            return [self._engine._row_to_dict(row) for row in rows]

        rows = await self._engine._run(query)
        has_more = len(rows) > page_size
        visible_rows = rows[:page_size]
        groups: dict[str, dict[str, Any]] = {}
        items: list[dict[str, Any]] = []
        for record in visible_rows:
            key = record["contradiction_key"]
            if key not in groups:
                groups[key] = await self._group_for_key(scope_context, key)
            item = await self._owner_record(record)
            item["group_cas"] = groups[key]
            items.append(item)
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = encode_page_cursor(
                scope_context,
                filters,
                created_at=last["created_at"],
                record_id=last["record_id"],
            )
        return {
            "status": "OK",
            "request_id": self._request_id(request_id),
            "scope": scope_context.to_dict(),
            "filters": {key: value for key, value in filters.items() if key != "kind" and value is not None},
            "items": items,
            "next_cursor": next_cursor,
        }

    async def detail(
        self,
        *,
        memory_id: str,
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        memory_id = self._uuid(memory_id, "memory_id")
        scope_context = self._scope(scope, project_id, worktree_id, workflow_id, agent_id)
        page_size = validate_owner_page_size(limit)
        filters = {"kind": "detail", "memory_id": memory_id}
        after = decode_page_cursor(cursor, scope_context, filters)

        def query() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
            assert self._engine.conn is not None
            where, params = self._engine._scope_where(scope_context)
            clauses = ["a.memory_id=?", where]
            values: list[Any] = [memory_id, *params]
            if after is not None:
                clauses.append("(a.created_at > ? OR (a.created_at = ? AND a.record_id > ?))")
                values.extend([after[0], after[0], after[1]])
            rows = self._engine.conn.execute(
                self._engine._select_sql(
                    f"WHERE {' AND '.join(clauses)} ORDER BY a.created_at,a.record_id LIMIT ?"
                ),
                [*values, page_size + 1],
            ).fetchall()
            records = [self._engine._row_to_dict(row) for row in rows]
            latest_row = self._engine.conn.execute(
                self._engine._select_sql(
                    f"WHERE a.memory_id=? AND {where} ORDER BY a.revision DESC,a.record_id DESC LIMIT 1"
                ),
                [memory_id, *params],
            ).fetchone()
            latest = self._engine._row_to_dict(latest_row) if latest_row is not None else None
            event_rows = self._engine.conn.execute(
                f"SELECT {_EVENT_SELECT} FROM lifecycle_events e JOIN adapter_records a ON a.record_id=e.record_id "
                f"WHERE e.memory_id=? AND {where} ORDER BY e.occurred_at,e.event_id LIMIT 100",
                [memory_id, *params],
            ).fetchall()
            event_names = (
                "event_id",
                "operation_id",
                "action",
                "occurred_at",
                "prior_lifecycle_state",
                "result_lifecycle_state",
                "prior_contradiction_state",
                "result_contradiction_state",
                "reason_code",
                "expected_revision",
                "result_revision",
                "related_record_id",
                "related_memory_id",
                "freshness_outcome",
                "observation_origin",
                "observed_at",
                "accepted_at",
                "freshness_generation",
                "clock_anomaly",
            )
            events = [dict(zip(event_names, row, strict=True)) for row in event_rows]
            return records, events, latest

        records, events, latest = await self._engine._run(query)
        if latest is None:
            if await self._engine.memory_exists_outside_scope(memory_id, scope_context):
                raise AdapterError("CROSS_SCOPE_DENIED", "memory_id exists outside the requested scope")
            raise AdapterError("NOT_FOUND", "memory_id was not found in the requested scope")
        has_more = len(records) > page_size
        visible_rows = records[:page_size]
        public_records = [await self._owner_record(record) for record in visible_rows]
        group_cas = await self._group_for_key(scope_context, latest["contradiction_key"])
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = encode_page_cursor(
                scope_context,
                filters,
                created_at=last["created_at"],
                record_id=last["record_id"],
            )
        return {
            "status": "OK",
            "request_id": self._request_id(request_id),
            "scope": scope_context.to_dict(),
            "memory_id": memory_id,
            "records": public_records,
            "events": events,
            "group_cas": group_cas,
            "next_cursor": next_cursor,
        }

    async def conflicts(
        self,
        *,
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        scope_context = self._scope(scope, project_id, worktree_id, workflow_id, agent_id)
        page_size = validate_owner_page_size(limit)
        filters = {"kind": "conflicts"}
        after = decode_page_cursor(cursor, scope_context, filters)

        def query() -> list[tuple[str, str]]:
            assert self._engine.conn is not None
            where, params = self._engine._scope_where(scope_context)
            state_marks = ",".join("?" for _ in _GOVERNANCE_ACTIVE_STATES)
            active_states = sorted(_GOVERNANCE_ACTIVE_STATES)
            having = (
                "SUM(CASE WHEN a.contradiction_state IN ('UNRESOLVED','QUARANTINED') "
                "OR a.lifecycle_state IN ('STALE','QUARANTINED') THEN 1 ELSE 0 END) > 0"
            )
            base = (
                "SELECT MIN(a.created_at) AS first_created_at,a.contradiction_key FROM adapter_records a "
                f"WHERE {where} AND a.lifecycle_state IN ({state_marks}) "
                f"GROUP BY a.contradiction_key HAVING {having}"
            )
            values: list[Any] = [*params, *active_states]
            if after is None:
                sql = f"SELECT first_created_at,contradiction_key FROM ({base}) ORDER BY first_created_at,contradiction_key LIMIT ?"
            else:
                sql = (
                    f"SELECT first_created_at,contradiction_key FROM ({base}) "
                    "WHERE first_created_at > ? OR (first_created_at = ? AND contradiction_key > ?) "
                    "ORDER BY first_created_at,contradiction_key LIMIT ?"
                )
                values.extend([after[0], after[0], after[1]])
            rows = self._engine.conn.execute(sql, [*values, page_size + 1]).fetchall()
            return [(str(row[0]), str(row[1])) for row in rows]

        rows = await self._engine._run(query)
        has_more = len(rows) > page_size
        visible = rows[:page_size]
        items = [
            {
                "contradiction_key": contradiction_key,
                "group_cas": await self._group_for_key(scope_context, contradiction_key),
            }
            for _, contradiction_key in visible
        ]
        next_cursor = None
        if has_more and visible:
            last_created_at, last_key = visible[-1]
            next_cursor = encode_page_cursor(
                scope_context,
                filters,
                created_at=last_created_at,
                record_id=last_key,
            )
        return {
            "status": "OK",
            "request_id": self._request_id(request_id),
            "scope": scope_context.to_dict(),
            "items": items,
            "next_cursor": next_cursor,
        }

    async def health(
        self,
        *,
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        scope_context = self._scope(scope, project_id, worktree_id, workflow_id, agent_id)
        return {
            "status": "OK",
            "request_id": self._request_id(request_id),
            "scope": scope_context.to_dict(),
            "backend_status": await self._engine.status(scope_context),
        }

    async def revoke(
        self,
        *,
        memory_id: str,
        expected_record_id: str,
        expected_revision: int,
        observed_group_token: str,
        idempotency_key: str,
        reason_code: str,
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._terminal_action(
            "REVOKE",
            memory_id=memory_id,
            expected_record_id=expected_record_id,
            expected_revision=expected_revision,
            observed_group_token=observed_group_token,
            idempotency_key=idempotency_key,
            reason_code=reason_code,
            scope=scope,
            project_id=project_id,
            worktree_id=worktree_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            request_id=request_id,
        )

    async def reject(
        self,
        *,
        memory_id: str,
        expected_record_id: str,
        expected_revision: int,
        observed_group_token: str,
        idempotency_key: str,
        reason_code: str,
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._terminal_action(
            "REJECT",
            memory_id=memory_id,
            expected_record_id=expected_record_id,
            expected_revision=expected_revision,
            observed_group_token=observed_group_token,
            idempotency_key=idempotency_key,
            reason_code=reason_code,
            scope=scope,
            project_id=project_id,
            worktree_id=worktree_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            request_id=request_id,
        )

    async def _governed_update_action(
        self,
        action: str,
        *,
        memory_id: str,
        expected_record_id: str,
        expected_revision: int,
        observed_group_token: str,
        idempotency_key: str,
        payload: dict[str, Any],
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"REVERIFY", "REPLACE"}:
            raise AdapterError("INVALID_ENUM", "Owner update action is invalid")
        memory_id = self._uuid(memory_id, "memory_id")
        expected_record_id = self._uuid(expected_record_id, "expected_record_id")
        expected_revision = self._positive_revision(expected_revision)
        observed_group_token = validate_group_token(observed_group_token)
        idempotency_key_value = validate_identifier(idempotency_key, "idempotency_key", required=True)
        assert idempotency_key_value is not None
        if not isinstance(payload, dict):
            raise AdapterError("INVALID_ENVELOPE", f"{action.lower()} payload must be an object")
        request_id_value = self._request_id(request_id)
        scope_context = self._scope(scope, project_id, worktree_id, workflow_id, agent_id)

        history = await self._engine.get_record(memory_id, scope_context, history=True)
        if not history:
            if await self._engine.memory_exists_outside_scope(memory_id, scope_context):
                raise AdapterError("CROSS_SCOPE_DENIED", "memory_id exists outside the requested scope")
            raise AdapterError("NOT_FOUND", "memory_id was not found in the requested scope")
        target = next((row for row in history if row["record_id"] == expected_record_id), None)
        if target is None or int(target["revision"]) != expected_revision:
            raise AdapterError("VERSION_CONFLICT", "expected record/revision no longer matches the inspected target")
        observation = await self._engine.group_observation(scope_context, target["contradiction_key"])
        current_token = observation["public"]["observed_group_token"]
        if current_token != observed_group_token:
            raise AdapterError(
                "GROUP_VERSION_CONFLICT",
                "contradiction group changed; refetch and review before retrying",
                refetch_required=True,
                current_group_token=current_token,
                current_group_count=observation["public"]["member_count"],
            )

        update_kwargs: dict[str, Any] = {
            "memory_id": memory_id,
            "expected_record_id": expected_record_id,
            "expected_revision": expected_revision,
            "mode": action,
            "idempotency_key": idempotency_key_value,
            "observed_group_token": observed_group_token,
            "request_id": request_id_value,
            **scope_context.to_dict(),
        }
        if action == "REVERIFY":
            update_kwargs["verification"] = payload
        else:
            update_kwargs["replacement"] = payload
        return await self._adapter.update(**update_kwargs)

    async def reverify(
        self,
        *,
        memory_id: str,
        expected_record_id: str,
        expected_revision: int,
        observed_group_token: str,
        idempotency_key: str,
        verification: dict[str, Any],
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._governed_update_action(
            "REVERIFY",
            memory_id=memory_id,
            expected_record_id=expected_record_id,
            expected_revision=expected_revision,
            observed_group_token=observed_group_token,
            idempotency_key=idempotency_key,
            payload=verification,
            scope=scope,
            project_id=project_id,
            worktree_id=worktree_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            request_id=request_id,
        )

    async def replace(
        self,
        *,
        memory_id: str,
        expected_record_id: str,
        expected_revision: int,
        observed_group_token: str,
        idempotency_key: str,
        replacement: dict[str, Any],
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._governed_update_action(
            "REPLACE",
            memory_id=memory_id,
            expected_record_id=expected_record_id,
            expected_revision=expected_revision,
            observed_group_token=observed_group_token,
            idempotency_key=idempotency_key,
            payload=replacement,
            scope=scope,
            project_id=project_id,
            worktree_id=worktree_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            request_id=request_id,
        )

    async def _terminal_action(
        self,
        action: str,
        *,
        memory_id: str,
        expected_record_id: str,
        expected_revision: int,
        observed_group_token: str,
        idempotency_key: str,
        reason_code: str,
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        memory_id = self._uuid(memory_id, "memory_id")
        expected_record_id = self._uuid(expected_record_id, "expected_record_id")
        expected_revision = self._positive_revision(expected_revision)
        observed_group_token = validate_group_token(observed_group_token)
        validate_owner_reason(action, reason_code)
        idempotency_key = validate_identifier(idempotency_key, "idempotency_key", required=True)
        assert idempotency_key is not None
        request_id_value = self._request_id(request_id)
        scope_context = self._scope(scope, project_id, worktree_id, workflow_id, agent_id)
        return await self._adapter.update(
            memory_id=memory_id,
            expected_record_id=expected_record_id,
            expected_revision=expected_revision,
            mode=action,
            idempotency_key=idempotency_key,
            observed_group_token=observed_group_token,
            reason_code=reason_code,
            request_id=request_id_value,
            **scope_context.to_dict(),
        )

    async def _winner_record(
        self,
        scope: ScopeContext,
        *,
        contradiction_key: str,
        winner_memory_id: str,
        winner_record_id: str,
    ) -> dict[str, Any]:
        history = await self._engine.get_record(winner_memory_id, scope, history=True)
        winner = next(
            (
                record
                for record in history
                if record["record_id"] == winner_record_id and record["contradiction_key"] == contradiction_key
            ),
            None,
        )
        if winner is not None:
            return winner
        if await self._engine.memory_exists_outside_scope(winner_memory_id, scope):
            raise AdapterError("CROSS_SCOPE_DENIED", "winner memory exists outside the requested scope")
        raise AdapterError("INVALID_WINNER", "winner is not a member of the requested contradiction group")

    def _candidate_successor(
        self,
        scope: ScopeContext,
        winner: dict[str, Any],
        verification: dict[str, Any],
        *,
        request_fingerprint: str,
        committed_at: str,
    ) -> dict[str, Any]:
        if not isinstance(verification, dict):
            raise AdapterError("WINNER_PROOF_INVALID", "Candidate winner verification must be an object")
        unknown = set(verification) - _RESOLVE_VERIFICATION_FIELDS
        if unknown:
            raise AdapterError(
                "WINNER_PROOF_INVALID",
                "Candidate winner verification contains unsupported fields: " + ", ".join(sorted(unknown)),
            )
        required = {"source_provenance", "freshness_policy", "source_timestamp", "verified_by"}
        if required - set(verification):
            raise AdapterError("WINNER_PROOF_REQUIRED", "Candidate winner requires complete fresh verification")
        if verification.get("verified_by") != "OWNER":
            raise AdapterError("WINNER_PROOF_INVALID", "Candidate winner verification must be verified_by OWNER")
        provenance = validate_provenance(verification["source_provenance"])
        freshness = validate_freshness(verification["freshness_policy"])
        source_timestamp = self._adapter._normalize_timestamp(verification.get("source_timestamp"), required=True)
        last_verified_at = self._adapter._normalize_timestamp(
            verification.get("last_verified_at", committed_at), required=True
        )
        asserted_memory_type = verification.get("memory_type")
        if asserted_memory_type is not None and asserted_memory_type != winner["memory_type"]:
            raise AdapterError("WINNER_PROOF_INVALID", "Candidate winner proof cannot change memory_type")
        self._adapter._validate_on_change_freshness(provenance, freshness)
        if not is_fresh(freshness, last_verified_at, provenance, now=parse_iso(committed_at, required=True)):
            raise AdapterError("WINNER_PROOF_INVALID", "Candidate winner verification is not fresh enough for Current admission")
        if scope.scope == "CROSS_PROJECT_WORKFLOW":
            if winner["memory_type"] not in {"WORKFLOW_RULE", "DECISION_RATIONALE"}:
                raise AdapterError("SCOPE_TYPE_MISMATCH", "memory_type is incompatible with CROSS_PROJECT_WORKFLOW")
            self._adapter._validate_cross_project_freshness(provenance, freshness)

        old_tags = self._decode_json_field(winner.get("tags"), [])
        tags = self._adapter._tags(verification.get("tags", old_tags))
        subject_asserted = "subject" in verification
        tags_changed = tags != old_tags
        if subject_asserted or tags_changed:
            subject = self._adapter._subject(verification.get("subject"), tags, winner["fact"])
            if self._adapter._contradiction_identity_key(scope, winner["memory_type"], subject) != winner["contradiction_key"]:
                raise AdapterError("WINNER_PROOF_INVALID", "Candidate winner proof changes contradiction identity")
        confidence_value = verification.get("confidence", winner["confidence"])
        if isinstance(confidence_value, bool) or not isinstance(confidence_value, (int, float)):
            raise AdapterError("WINNER_PROOF_INVALID", "Candidate winner confidence must be numeric")
        confidence = float(confidence_value)
        if not 0.0 <= confidence <= 1.0:
            raise AdapterError("WINNER_PROOF_INVALID", "Candidate winner confidence must be between 0 and 1")

        record_id = str(uuid.uuid4())
        successor = dict(winner)
        successor.update(
            {
                "record_id": record_id,
                "revision": int(winner["revision"]) + 1,
                "parent_record_id": winner["record_id"],
                "source_provenance": provenance,
                "source_timestamp": source_timestamp,
                "created_at": committed_at,
                "last_verified_at": last_verified_at,
                "verified_by": "OWNER",
                "authority_role": self._adapter._authority_for(scope),
                "contribution_origin": self._decode_json_field(
                    winner.get("contribution_origin"),
                    {"contributor_class": "LEGACY_UNKNOWN", "contributor_ref": None},
                ),
                "freshness_policy": freshness,
                "supersedes": [],
                "contradiction_state": "NONE",
                "confidence": confidence,
                "tags": tags,
                "lifecycle_state": "VALIDATED_CURRENT",
                "native_content_hash": native_hash(scope, winner["fact"], revision_key=record_id),
                "payload_fingerprint": request_fingerprint,
                "idempotency_key": f"internal-resolve-{uuid.uuid4()}",
            }
        )
        return successor

    async def resolve(
        self,
        *,
        contradiction_key: str,
        observed_group_token: str,
        winner_memory_id: str,
        winner_record_id: str,
        winner_expected_revision: int,
        idempotency_key: str,
        reason_code: str,
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        verification: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        contradiction_key_value = validate_identifier(contradiction_key, "contradiction_key", required=True)
        assert contradiction_key_value is not None
        observed_group_token = validate_group_token(observed_group_token)
        winner_memory_id = self._uuid(winner_memory_id, "winner_memory_id")
        winner_record_id = self._uuid(winner_record_id, "winner_record_id")
        winner_expected_revision = self._positive_revision(winner_expected_revision, "winner_expected_revision")
        idempotency_key_value = validate_identifier(idempotency_key, "idempotency_key", required=True)
        assert idempotency_key_value is not None
        validate_owner_reason("RESOLVE", reason_code)
        request_id_value = self._request_id(request_id)
        scope_context = self._scope(scope, project_id, worktree_id, workflow_id, agent_id)
        winner = await self._winner_record(
            scope_context,
            contradiction_key=contradiction_key_value,
            winner_memory_id=winner_memory_id,
            winner_record_id=winner_record_id,
        )
        if int(winner["revision"]) != winner_expected_revision:
            raise AdapterError("VERSION_CONFLICT", "winner expected_revision no longer matches")
        if winner["lifecycle_state"] == "CANDIDATE" and verification is None:
            raise AdapterError("WINNER_PROOF_REQUIRED", "Candidate RESOLVE winner requires complete fresh verification")
        if winner["lifecycle_state"] == "VALIDATED_CURRENT" and verification is not None:
            raise AdapterError("WINNER_PROOF_INVALID", "existing Current winner does not accept verification payload")
        if winner["lifecycle_state"] not in {"VALIDATED_CURRENT", "CANDIDATE"}:
            raise AdapterError("INVALID_WINNER", "winner lifecycle is not admissible")

        actor_ref = self._owner_context.contributor_ref
        verification_digest = digest(verification) if verification is not None else None
        request_fingerprint = digest(
            {
                "operation": "RESOLVE",
                "scope": scope_context.to_dict(),
                "contradiction_key": contradiction_key_value,
                "observed_group_token": observed_group_token,
                "winner_memory_id": winner_memory_id,
                "winner_record_id": winner_record_id,
                "winner_expected_revision": winner_expected_revision,
                "reason_code": reason_code,
                "actor_class": self._owner_context.contributor_class,
                "actor_ref_digest": digest(actor_ref) if actor_ref is not None else None,
                "grant_ref_digest": digest(self._owner_context.grant_ref),
                "verification_digest": verification_digest,
            }
        )
        committed_at = iso_now()
        candidate_successor = None
        if winner["lifecycle_state"] == "CANDIDATE":
            assert verification is not None
            candidate_successor = self._candidate_successor(
                scope_context,
                winner,
                verification,
                request_fingerprint=request_fingerprint,
                committed_at=committed_at,
            )
        return await self._engine.resolve_group(
            scope_context,
            contradiction_key=contradiction_key_value,
            observed_group_token=observed_group_token,
            winner_memory_id=winner_memory_id,
            winner_record_id=winner_record_id,
            winner_expected_revision=winner_expected_revision,
            idempotency_key=idempotency_key_value,
            request_fingerprint=request_fingerprint,
            request_id=request_id_value,
            reason_code=reason_code,
            actor_class=self._owner_context.contributor_class,
            actor_ref_digest=digest(actor_ref) if actor_ref is not None else None,
            grant_ref_digest=digest(self._owner_context.grant_ref),
            committed_at=committed_at,
            candidate_successor=candidate_successor,
        )

    @staticmethod
    def _bounded_reference(value: Any, field: str, *, required: bool) -> str | None:
        if value is None:
            if required:
                raise AdapterError("EVIDENCE_INVALID", f"{field} is required")
            return None
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 256:
            raise AdapterError("EVIDENCE_INVALID", f"{field} is invalid or unbounded")
        normalized = value.strip()
        if "\n" in normalized or "\r" in normalized or contains_sensitive(normalized):
            raise AdapterError("RAW_OR_SENSITIVE_CONTENT", f"{field} contains unsafe material")
        return normalized

    async def manual_recheck(
        self,
        *,
        memory_id: str,
        expected_record_id: str,
        expected_revision: int,
        observed_group_token: str,
        idempotency_key: str,
        evidence: dict[str, Any],
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        memory_id = self._uuid(memory_id, "memory_id")
        expected_record_id = self._uuid(expected_record_id, "expected_record_id")
        expected_revision = self._positive_revision(expected_revision)
        observed_group_token = validate_group_token(observed_group_token)
        idempotency_key_value = validate_identifier(idempotency_key, "idempotency_key", required=True)
        assert idempotency_key_value is not None
        request_id_value = self._request_id(request_id)
        scope_context = self._scope(scope, project_id, worktree_id, workflow_id, agent_id)
        if not isinstance(evidence, dict):
            raise AdapterError("EVIDENCE_INVALID", "manual recheck evidence must be an object")
        allowed = {
            "origin",
            "claim",
            "recheck_ref",
            "observed_at",
            "observed_source_hash",
            "evidence_ref",
            "method",
        }
        if set(evidence) - allowed:
            raise AdapterError("EVIDENCE_INVALID", "manual recheck evidence contains unsupported fields")
        if evidence.get("origin") != "OWNER_SUPPLIED":
            raise AdapterError("EVIDENCE_INVALID", "manual recheck evidence origin must be OWNER_SUPPLIED")
        claim = evidence.get("claim")
        if claim not in _MANUAL_RECHECK_CLAIMS:
            raise AdapterError("EVIDENCE_INVALID", "manual recheck claim is invalid")
        method = evidence.get("method")
        if method not in _MANUAL_RECHECK_METHODS:
            raise AdapterError("EVIDENCE_INVALID", "manual recheck method is invalid")
        recheck_ref = self._bounded_reference(evidence.get("recheck_ref"), "recheck_ref", required=True)
        observed_at = self._bounded_reference(evidence.get("observed_at"), "observed_at", required=True)
        assert recheck_ref is not None and observed_at is not None
        parse_iso(observed_at, required=True)
        observed_source_hash = evidence.get("observed_source_hash")
        if claim == "OBSERVED_HASH":
            if (
                not isinstance(observed_source_hash, str)
                or len(observed_source_hash) != 64
                or any(character not in "0123456789abcdefABCDEF" for character in observed_source_hash)
            ):
                raise AdapterError("EVIDENCE_INVALID", "OBSERVED_HASH requires a SHA-256 observed_source_hash")
            observed_source_hash = observed_source_hash.lower()
        elif observed_source_hash is not None:
            raise AdapterError("EVIDENCE_INVALID", f"{claim} must not include observed_source_hash")
        evidence_ref = self._bounded_reference(evidence.get("evidence_ref"), "evidence_ref", required=False)
        canonical_evidence = {
            "origin": "OWNER_SUPPLIED",
            "claim": claim,
            "recheck_ref": recheck_ref,
            "observed_at": observed_at,
            "observed_source_hash": observed_source_hash,
            "evidence_ref": evidence_ref,
            "method": method,
        }
        actor_ref = self._owner_context.contributor_ref
        request_fingerprint = digest(
            {
                "operation": "MANUAL_RECHECK",
                "scope": scope_context.to_dict(),
                "memory_id": memory_id,
                "expected_record_id": expected_record_id,
                "expected_revision": expected_revision,
                "observed_group_token": observed_group_token,
                "actor_class": self._owner_context.contributor_class,
                "actor_ref_digest": digest(actor_ref) if actor_ref is not None else None,
                "grant_ref_digest": digest(self._owner_context.grant_ref),
                "evidence": canonical_evidence,
            }
        )
        return await self._engine.manual_recheck(
            scope_context,
            memory_id=memory_id,
            expected_record_id=expected_record_id,
            expected_revision=expected_revision,
            observed_group_token=observed_group_token,
            idempotency_key=idempotency_key_value,
            request_fingerprint=request_fingerprint,
            request_id=request_id_value,
            claim=claim,
            recheck_ref=recheck_ref,
            observed_at=observed_at,
            observed_source_hash=observed_source_hash,
            evidence_ref_digest=digest(evidence_ref) if evidence_ref is not None else None,
            method=method,
            actor_class=self._owner_context.contributor_class,
            actor_ref_digest=digest(actor_ref) if actor_ref is not None else None,
            grant_ref_digest=digest(self._owner_context.grant_ref),
        )

    def _normalize_readmit_verification(self, verification: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(verification, dict):
            raise AdapterError("VERIFICATION_REQUIRED", "readmit verification must be an object")
        unknown = set(verification) - _READMIT_VERIFICATION_FIELDS
        if unknown:
            raise AdapterError(
                "INVALID_ENVELOPE",
                "readmit verification contains unsupported fields: " + ", ".join(sorted(unknown)),
            )
        missing = {
            "source_provenance",
            "freshness_policy",
            "source_timestamp",
            "verified_by",
        } - set(verification)
        if missing:
            raise AdapterError(
                "VERIFICATION_REQUIRED",
                "readmit requires verification fields: " + ", ".join(sorted(missing)),
            )
        if verification.get("verified_by") != "OWNER":
            raise AdapterError("VERIFICATION_REQUIRED", "readmit verification must be verified_by OWNER")
        provenance = validate_provenance(verification["source_provenance"])
        freshness = validate_freshness(verification["freshness_policy"])
        source_timestamp = self._adapter._normalize_timestamp(
            verification.get("source_timestamp"), required=True
        )
        last_verified_at = None
        if verification.get("last_verified_at") is not None:
            last_verified_at = self._adapter._normalize_timestamp(
                verification.get("last_verified_at"), required=True
            )
        self._adapter._validate_on_change_freshness(provenance, freshness)
        tags = None
        if "tags" in verification:
            tags = self._adapter._tags(verification.get("tags"))
        confidence = None
        if "confidence" in verification:
            raw_confidence = verification.get("confidence")
            if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
                raise AdapterError("VERIFICATION_REQUIRED", "readmit confidence must be numeric")
            confidence = float(raw_confidence)
            if not 0.0 <= confidence <= 1.0:
                raise AdapterError("VERIFICATION_REQUIRED", "readmit confidence must be between 0 and 1")
        subject = None
        if "subject" in verification:
            raw_subject = verification.get("subject")
            if not isinstance(raw_subject, str) or not raw_subject.strip() or len(raw_subject.encode("utf-8")) > 96:
                raise AdapterError("SEMANTIC_IDENTITY_MISMATCH", "readmit subject assertion is invalid")
            subject = " ".join(raw_subject.strip().split()).casefold()
            if contains_sensitive(subject):
                raise AdapterError("RAW_OR_SENSITIVE_CONTENT", "readmit subject assertion contains sensitive material")
        return {
            "source_provenance": provenance,
            "freshness_policy": freshness,
            "source_timestamp": source_timestamp,
            "last_verified_at": last_verified_at,
            "verified_by": "OWNER",
            "confidence": confidence,
            "tags": tags,
            "subject": subject,
        }

    async def _exact_readmit_target(
        self,
        scope: ScopeContext,
        *,
        memory_id: str,
        revoked_record_id: str,
        expected_revoked_revision: int,
    ) -> dict[str, Any]:
        rows = await self._engine.get_record(memory_id, scope, history=True)
        target = next((row for row in rows if row["record_id"] == revoked_record_id), None)
        if target is None:
            if await self._engine.memory_exists_outside_scope(memory_id, scope):
                raise AdapterError("CROSS_SCOPE_DENIED", "readmit target exists outside the requested scope")
            raise AdapterError("NOT_REVOKED", "readmit target was not found")
        if target["lifecycle_state"] == "QUARANTINED":
            raise AdapterError("READMIT_RECOVERY_PURGED_DENIED", "Recovery-quarantined target cannot be readmitted")
        if target["lifecycle_state"] != "REVOKED":
            raise AdapterError("NOT_REVOKED", "readmit target is not REVOKED")
        if int(target["revision"]) != expected_revoked_revision:
            raise AdapterError("VERSION_CONFLICT", "readmit expected_revoked_revision no longer matches")
        return target

    def _readmit_successor(
        self,
        scope: ScopeContext,
        target: dict[str, Any],
        verification: dict[str, Any],
        *,
        request_fingerprint: str,
        committed_at: str,
    ) -> dict[str, Any]:
        if scope.scope == "CROSS_PROJECT_WORKFLOW":
            if target["memory_type"] not in {"WORKFLOW_RULE", "DECISION_RATIONALE"}:
                raise AdapterError("SCOPE_TYPE_MISMATCH", "memory_type is incompatible with CROSS_PROJECT_WORKFLOW")
            self._adapter._validate_cross_project_freshness(
                verification["source_provenance"], verification["freshness_policy"]
            )
        old_tags = self._decode_json_field(target.get("tags"), [])
        tags = verification["tags"] if verification["tags"] is not None else old_tags
        asserted_subject = verification["subject"]
        if asserted_subject is not None:
            asserted_key = self._adapter._contradiction_identity_key(
                scope,
                target["memory_type"],
                asserted_subject,
            )
            if asserted_key != target["contradiction_key"]:
                raise AdapterError(
                    "SEMANTIC_IDENTITY_MISMATCH",
                    "readmit subject assertion changes contradiction identity",
                )
        if verification["tags"] is not None:
            old_subject_tags = self._adapter._subject_tags(old_tags)
            refreshed_subject_tags = self._adapter._subject_tags(tags)

            def subject_tag_is_identity(value: str) -> bool:
                return (
                    self._adapter._contradiction_identity_key(
                        scope,
                        target["memory_type"],
                        value,
                    )
                    == target["contradiction_key"]
                )

            old_identity_tags = {value for value in old_subject_tags if subject_tag_is_identity(value)}
            old_legacy_tags = set(old_subject_tags) - old_identity_tags
            refreshed_identity_tags = {
                value for value in refreshed_subject_tags if subject_tag_is_identity(value)
            }
            refreshed_legacy_tags = set(refreshed_subject_tags) - refreshed_identity_tags
            if refreshed_legacy_tags - old_legacy_tags:
                raise AdapterError(
                    "SEMANTIC_IDENTITY_MISMATCH",
                    "readmit tags introduce a different subject identity",
                )
            if old_identity_tags and not refreshed_identity_tags and asserted_subject is None:
                fallback_subject = self._adapter._subject(None, tags, target["fact"])
                if (
                    self._adapter._contradiction_identity_key(
                        scope,
                        target["memory_type"],
                        fallback_subject,
                    )
                    != target["contradiction_key"]
                ):
                    raise AdapterError(
                        "SEMANTIC_IDENTITY_MISMATCH",
                        "readmit tags remove the subject identity of the revoked target",
                    )
        record_id = str(uuid.uuid4())
        confidence = (
            verification["confidence"]
            if verification["confidence"] is not None
            else float(target["confidence"])
        )
        successor = dict(target)
        successor.update(
            {
                "record_id": record_id,
                "memory_id": target["memory_id"],
                "revision": int(target["revision"]) + 1,
                "parent_record_id": target["record_id"],
                "source_provenance": verification["source_provenance"],
                "source_timestamp": verification["source_timestamp"],
                "created_at": committed_at,
                "last_verified_at": verification["last_verified_at"] or committed_at,
                "verified_by": "OWNER",
                "authority_role": self._adapter._authority_for(scope),
                "contribution_origin": {
                    "contributor_class": self._owner_context.contributor_class,
                    "contributor_ref": self._owner_context.contributor_ref,
                },
                "freshness_policy": verification["freshness_policy"],
                "supersedes": [],
                "contradiction_state": "NONE",
                "confidence": confidence,
                "tags": tags,
                "lifecycle_state": "VALIDATED_CURRENT",
                "native_content_hash": native_hash(scope, target["fact"], revision_key=record_id),
                "payload_fingerprint": request_fingerprint,
                "idempotency_key": f"internal-readmit-{uuid.uuid4()}",
            }
        )
        if not is_fresh(
            successor["freshness_policy"],
            successor["last_verified_at"],
            successor["source_provenance"],
            now=parse_iso(committed_at, required=True),
        ):
            raise AdapterError("VERIFICATION_REQUIRED", "readmit verification is not fresh enough for Current admission")
        return successor

    async def readmit(
        self,
        *,
        memory_id: str,
        revoked_record_id: str,
        expected_revoked_revision: int,
        observed_group_token: str,
        idempotency_key: str,
        verification: dict[str, Any],
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        memory_id = self._uuid(memory_id, "memory_id")
        revoked_record_id = self._uuid(revoked_record_id, "revoked_record_id")
        expected_revoked_revision = self._positive_revision(
            expected_revoked_revision, "expected_revoked_revision"
        )
        observed_group_token = validate_group_token(observed_group_token)
        idempotency_key_value = validate_identifier(idempotency_key, "idempotency_key", required=True)
        assert idempotency_key_value is not None
        request_id_value = self._request_id(request_id)
        scope_context = self._scope(scope, project_id, worktree_id, workflow_id, agent_id)
        normalized_verification = self._normalize_readmit_verification(verification)
        actor_ref = self._owner_context.contributor_ref
        request_fingerprint = digest(
            {
                "operation": "READMIT",
                "scope": scope_context.to_dict(),
                "memory_id": memory_id,
                "revoked_record_id": revoked_record_id,
                "expected_revoked_revision": expected_revoked_revision,
                "observed_group_token": observed_group_token,
                "actor_class": self._owner_context.contributor_class,
                "actor_ref_digest": digest(actor_ref) if actor_ref is not None else None,
                "grant_ref_digest": digest(self._owner_context.grant_ref),
                "verification": normalized_verification,
            }
        )
        existing_operation = await self._engine.operation_by_idempotency(idempotency_key_value)
        if existing_operation is not None:
            return await self._engine.readmit(
                scope_context,
                memory_id=memory_id,
                revoked_record_id=revoked_record_id,
                expected_revoked_revision=expected_revoked_revision,
                observed_group_token=observed_group_token,
                idempotency_key=idempotency_key_value,
                request_fingerprint=request_fingerprint,
                request_id=request_id_value,
                verified_successor={},
                actor_class=self._owner_context.contributor_class,
                actor_ref_digest=digest(actor_ref) if actor_ref is not None else None,
                grant_ref_digest=digest(self._owner_context.grant_ref),
                committed_at=iso_now(),
            )
        target = await self._exact_readmit_target(
            scope_context,
            memory_id=memory_id,
            revoked_record_id=revoked_record_id,
            expected_revoked_revision=expected_revoked_revision,
        )
        committed_at = iso_now()
        successor = self._readmit_successor(
            scope_context,
            target,
            normalized_verification,
            request_fingerprint=request_fingerprint,
            committed_at=committed_at,
        )
        return await self._engine.readmit(
            scope_context,
            memory_id=memory_id,
            revoked_record_id=revoked_record_id,
            expected_revoked_revision=expected_revoked_revision,
            observed_group_token=observed_group_token,
            idempotency_key=idempotency_key_value,
            request_fingerprint=request_fingerprint,
            request_id=request_id_value,
            verified_successor=successor,
            actor_class=self._owner_context.contributor_class,
            actor_ref_digest=digest(actor_ref) if actor_ref is not None else None,
            grant_ref_digest=digest(self._owner_context.grant_ref),
            committed_at=committed_at,
        )

    @staticmethod
    def _db_status_section(
        db_snapshot_at: str,
        *,
        observation: str,
        value: Any,
        reason: str | None = None,
    ) -> dict[str, Any]:
        section = {
            "db_snapshot_at": db_snapshot_at,
            "consistency": "READ_TRANSACTION",
            "observation": observation,
            "value": value,
        }
        if reason is not None:
            section["reason"] = reason
        return section

    @staticmethod
    def _fixed_unknown_counts(keys: tuple[str, ...]) -> dict[str, None]:
        return {key: None for key in keys}

    def _retention_snapshot_sync(self, scope: ScopeContext) -> dict[str, Any]:
        conn = self._engine.conn
        if conn is None:
            raise AdapterError("STATUS_UNAVAILABLE", "retention status requires an open backend")
        if conn.in_transaction:
            raise AdapterError("STATUS_UNAVAILABLE", "retention status could not start an isolated read transaction")
        conn.execute("BEGIN")
        try:
            db_snapshot_at = iso_now()
            where, params = self._engine._scope_where(scope)
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                ).fetchall()
            }

            def indexes(table: str) -> set[str]:
                return {str(row[1]) for row in conn.execute(f"PRAGMA index_list({table})").fetchall()}

            scope_index_ok = (
                "adapter_records" in tables
                and "idx_adapter_scope_current" in indexes("adapter_records")
            )
            materialization_index_ok = (
                {"memories", "memory_embeddings", "memory_graph"}.issubset(tables)
                and "idx_content_hash" in indexes("memories")
                and {"idx_graph_source", "idx_graph_target"}.issubset(indexes("memory_graph"))
            )
            event_index_ok = (
                "lifecycle_events" in tables
                and "idx_lifecycle_event_target_generation" in indexes("lifecycle_events")
            )

            page_size_row = conn.execute("PRAGMA page_size").fetchone()
            page_count_row = conn.execute("PRAGMA page_count").fetchone()
            freelist_row = conn.execute("PRAGMA freelist_count").fetchone()
            db_capacity = {
                "database_allocated_bytes": (
                    int(page_size_row[0]) * int(page_count_row[0])
                    if page_size_row is not None and page_count_row is not None
                    else None
                ),
                "freelist_bytes": (
                    int(page_size_row[0]) * int(freelist_row[0])
                    if page_size_row is not None and freelist_row is not None
                    else None
                ),
            }

            unknown_lifecycle = self._fixed_unknown_counts(_STATUS_LIFECYCLE_STATES)
            unknown_freshness = self._fixed_unknown_counts(_FRESHNESS_REVIEW_KEYS)
            if not scope_index_ok:
                reason = "STATUS_INDEX_UNAVAILABLE"
                return {
                    "db_snapshot_at": db_snapshot_at,
                    "logical_counts": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=unknown_lifecycle, reason=reason
                    ),
                    "freshness_review_counts": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=unknown_freshness, reason=reason
                    ),
                    "native_vector_materialized_by_state": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=unknown_lifecycle, reason=reason
                    ),
                    "history_only_total": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=None, reason=reason
                    ),
                    "compaction_ready": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=None, reason=reason
                    ),
                    "compaction_blocked_partial": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=None, reason=reason
                    ),
                    "terminal_native_vector_materialized": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=None, reason=reason
                    ),
                    "terminal_graph_linked": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=None, reason=reason
                    ),
                    "terminal_partial_materialization": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=None, reason=reason
                    ),
                    "db_capacity": db_capacity,
                }

            preflight_rows = conn.execute(
                f"SELECT a.record_id FROM adapter_records a INDEXED BY idx_adapter_scope_current "
                f"WHERE {where} LIMIT ?",
                [*params, MAX_STATUS_SCOPE_ROWS + 1],
            ).fetchall()
            if len(preflight_rows) > MAX_STATUS_SCOPE_ROWS:
                reason = "STATUS_SCAN_LIMIT_EXCEEDED"
                return {
                    "db_snapshot_at": db_snapshot_at,
                    "logical_counts": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=unknown_lifecycle, reason=reason
                    ),
                    "freshness_review_counts": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=unknown_freshness, reason=reason
                    ),
                    "native_vector_materialized_by_state": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=unknown_lifecycle, reason=reason
                    ),
                    "history_only_total": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=None, reason=reason
                    ),
                    "compaction_ready": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=None, reason=reason
                    ),
                    "compaction_blocked_partial": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=None, reason=reason
                    ),
                    "terminal_native_vector_materialized": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=None, reason=reason
                    ),
                    "terminal_graph_linked": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=None, reason=reason
                    ),
                    "terminal_partial_materialization": self._db_status_section(
                        db_snapshot_at, observation="UNKNOWN", value=None, reason=reason
                    ),
                    "db_capacity": db_capacity,
                }

            scoped_rows = conn.execute(
                "SELECT a.record_id,a.revision,a.lifecycle_state,a.freshness_policy,a.last_verified_at,"
                "a.native_content_hash FROM adapter_records a INDEXED BY idx_adapter_scope_current "
                f"WHERE {where} ORDER BY a.record_id",
                params,
            ).fetchall()
            logical_counts = {state: 0 for state in _STATUS_LIFECYCLE_STATES}
            logical_rows = conn.execute(
                "SELECT a.lifecycle_state,COUNT(*) FROM adapter_records a INDEXED BY idx_adapter_scope_current "
                f"WHERE {where} GROUP BY a.lifecycle_state",
                params,
            ).fetchall()
            for row in logical_rows:
                state = str(row[0])
                if state not in logical_counts:
                    return {
                        "db_snapshot_at": db_snapshot_at,
                        "logical_counts": self._db_status_section(
                            db_snapshot_at,
                            observation="UNKNOWN",
                            value=unknown_lifecycle,
                            reason="STATUS_UNAVAILABLE",
                        ),
                        "freshness_review_counts": self._db_status_section(
                            db_snapshot_at,
                            observation="UNKNOWN",
                            value=unknown_freshness,
                            reason="STATUS_UNAVAILABLE",
                        ),
                        "native_vector_materialized_by_state": self._db_status_section(
                            db_snapshot_at,
                            observation="UNKNOWN",
                            value=unknown_lifecycle,
                            reason="STATUS_UNAVAILABLE",
                        ),
                        "history_only_total": self._db_status_section(
                            db_snapshot_at, observation="UNKNOWN", value=None, reason="STATUS_UNAVAILABLE"
                        ),
                        "compaction_ready": self._db_status_section(
                            db_snapshot_at, observation="UNKNOWN", value=None, reason="STATUS_UNAVAILABLE"
                        ),
                        "compaction_blocked_partial": self._db_status_section(
                            db_snapshot_at, observation="UNKNOWN", value=None, reason="STATUS_UNAVAILABLE"
                        ),
                        "terminal_native_vector_materialized": self._db_status_section(
                            db_snapshot_at, observation="UNKNOWN", value=None, reason="STATUS_UNAVAILABLE"
                        ),
                        "terminal_graph_linked": self._db_status_section(
                            db_snapshot_at, observation="UNKNOWN", value=None, reason="STATUS_UNAVAILABLE"
                        ),
                        "terminal_partial_materialization": self._db_status_section(
                            db_snapshot_at, observation="UNKNOWN", value=None, reason="STATUS_UNAVAILABLE"
                        ),
                        "db_capacity": db_capacity,
                    }
                logical_counts[state] = int(row[1])

            logical_section = self._db_status_section(
                db_snapshot_at, observation="EXACT", value=logical_counts
            )

            freshness_section: dict[str, Any]
            if not event_index_ok:
                freshness_section = self._db_status_section(
                    db_snapshot_at,
                    observation="UNKNOWN",
                    value=unknown_freshness,
                    reason="STATUS_INDEX_UNAVAILABLE",
                )
            else:
                event_preflight = conn.execute(
                    "SELECT e.event_id FROM adapter_records a INDEXED BY idx_adapter_scope_current "
                    "JOIN lifecycle_events e INDEXED BY idx_lifecycle_event_target_generation "
                    "ON e.target_record_id=a.record_id AND e.target_revision=a.revision "
                    f"WHERE {where} AND e.freshness_generation IS NOT NULL LIMIT ?",
                    [*params, MAX_STATUS_EVENT_ROWS + 1],
                ).fetchall()
                if len(event_preflight) > MAX_STATUS_EVENT_ROWS:
                    freshness_section = self._db_status_section(
                        db_snapshot_at,
                        observation="UNKNOWN",
                        value=unknown_freshness,
                        reason="STATUS_SCAN_LIMIT_EXCEEDED",
                    )
                else:
                    event_rows = conn.execute(
                        "SELECT e.target_record_id,e.target_revision,e.freshness_outcome,e.freshness_generation "
                        "FROM adapter_records a INDEXED BY idx_adapter_scope_current "
                        "JOIN lifecycle_events e INDEXED BY idx_lifecycle_event_target_generation "
                        "ON e.target_record_id=a.record_id AND e.target_revision=a.revision "
                        f"WHERE {where} AND e.freshness_generation IS NOT NULL "
                        "ORDER BY e.target_record_id,e.target_revision,e.freshness_generation",
                        params,
                    ).fetchall()
                    latest_events: dict[tuple[str, int], tuple[str, int]] = {}
                    malformed_event = False
                    for event in event_rows:
                        outcome = event[2]
                        generation = event[3]
                        if (
                            outcome not in {"UNCHANGED", "CHANGED", "UNAVAILABLE", "INDETERMINATE"}
                            or isinstance(generation, bool)
                            or not isinstance(generation, int)
                            or generation < 1
                        ):
                            malformed_event = True
                            break
                        latest_events[(str(event[0]), int(event[1]))] = (str(outcome), int(generation))
                    freshness_counts = {key: 0 for key in _FRESHNESS_REVIEW_KEYS}
                    malformed_policy = False
                    snapshot_dt = parse_iso(db_snapshot_at, required=True)
                    assert snapshot_dt is not None
                    if not malformed_event:
                        for row in scoped_rows:
                            if str(row[2]) != "VALIDATED_CURRENT":
                                continue
                            try:
                                raw_policy = json.loads(str(row[3]))
                                policy = validate_freshness(raw_policy)
                                kind = policy["kind"]
                                if kind == "ttl":
                                    verified_at = parse_iso(row[4], required=True)
                                    assert verified_at is not None
                                    ttl_seconds = policy.get("ttl_seconds")
                                    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
                                        raise AdapterError("INVALID_ENVELOPE", "ttl_seconds is invalid")
                                    if snapshot_dt > verified_at + timedelta(seconds=ttl_seconds):
                                        freshness_counts["ttl_stale"] += 1
                                elif kind == "manual":
                                    freshness_counts["manual_review"] += 1
                                elif kind == "on_change":
                                    latest = latest_events.get((str(row[0]), int(row[1])))
                                    if latest is not None:
                                        if latest[0] == "CHANGED":
                                            freshness_counts["on_change_source_change_required"] += 1
                                        elif latest[0] == "UNAVAILABLE":
                                            freshness_counts["on_change_unavailable"] += 1
                                        elif latest[0] == "INDETERMINATE":
                                            freshness_counts["on_change_indeterminate"] += 1
                            except (AdapterError, json.JSONDecodeError, TypeError, ValueError):
                                malformed_policy = True
                                break
                    if malformed_event or malformed_policy:
                        freshness_section = self._db_status_section(
                            db_snapshot_at,
                            observation="UNKNOWN",
                            value=unknown_freshness,
                            reason="STATUS_UNAVAILABLE",
                        )
                    else:
                        freshness_section = self._db_status_section(
                            db_snapshot_at, observation="EXACT", value=freshness_counts
                        )

            materialized_unknown = self._db_status_section(
                db_snapshot_at,
                observation="UNKNOWN",
                value=unknown_lifecycle,
                reason="STATUS_INDEX_UNAVAILABLE" if not materialization_index_ok else "STATUS_UNAVAILABLE",
            )
            scalar_unknown_reason = "STATUS_INDEX_UNAVAILABLE" if not materialization_index_ok else "STATUS_UNAVAILABLE"
            history_section = self._db_status_section(
                db_snapshot_at, observation="UNKNOWN", value=None, reason=scalar_unknown_reason
            )
            compaction_ready_section = self._db_status_section(
                db_snapshot_at, observation="UNKNOWN", value=None, reason=scalar_unknown_reason
            )
            compaction_blocked_section = self._db_status_section(
                db_snapshot_at, observation="UNKNOWN", value=None, reason=scalar_unknown_reason
            )
            terminal_materialized_section = self._db_status_section(
                db_snapshot_at, observation="UNKNOWN", value=None, reason=scalar_unknown_reason
            )
            terminal_graph_section = self._db_status_section(
                db_snapshot_at, observation="UNKNOWN", value=None, reason=scalar_unknown_reason
            )
            terminal_partial_section = self._db_status_section(
                db_snapshot_at, observation="UNKNOWN", value=None, reason=scalar_unknown_reason
            )
            if materialization_index_ok:
                material_rows = conn.execute(
                    "SELECT a.lifecycle_state,"
                    "EXISTS(SELECT 1 FROM memories m INDEXED BY idx_content_hash "
                    " WHERE m.content_hash=a.native_content_hash),"
                    "EXISTS(SELECT 1 FROM memories m INDEXED BY idx_content_hash "
                    " WHERE m.content_hash=a.native_content_hash LIMIT 1 OFFSET 1),"
                    "EXISTS(SELECT 1 FROM memories m INDEXED BY idx_content_hash "
                    " WHERE m.content_hash=a.native_content_hash AND m.deleted_at IS NULL),"
                    "EXISTS(SELECT 1 FROM memories m INDEXED BY idx_content_hash "
                    " JOIN memory_embeddings e ON e.rowid=m.id "
                    " WHERE m.content_hash=a.native_content_hash AND e.store=?),"
                    "EXISTS(SELECT 1 FROM memories m INDEXED BY idx_content_hash "
                    " JOIN memory_embeddings e ON e.rowid=m.id "
                    " WHERE m.content_hash=a.native_content_hash),"
                    "(EXISTS(SELECT 1 FROM memory_graph g INDEXED BY idx_graph_source "
                    " WHERE g.source_hash=a.native_content_hash) OR "
                    " EXISTS(SELECT 1 FROM memory_graph g INDEXED BY idx_graph_target "
                    " WHERE g.target_hash=a.native_content_hash)) "
                    "FROM adapter_records a INDEXED BY idx_adapter_scope_current "
                    f"WHERE {where} ORDER BY a.record_id",
                    [self._engine.embedding_store_key, *params],
                ).fetchall()
                ambiguous = any(bool(row[2]) or (bool(row[5]) and not bool(row[4])) for row in material_rows)
                if not ambiguous:
                    materialized_by_state = {state: 0 for state in _STATUS_LIFECYCLE_STATES}
                    history_only_total = 0
                    compaction_ready = 0
                    terminal_materialized = 0
                    terminal_graph_linked = 0
                    terminal_partial = 0
                    for row in material_rows:
                        state = str(row[0])
                        native_any = bool(row[1])
                        live_native = bool(row[3])
                        matching_vector = bool(row[4])
                        graph_link = bool(row[6])
                        full_materialized = live_native and matching_vector
                        if full_materialized:
                            materialized_by_state[state] += 1
                        if state == "HISTORICAL" and not native_any and not matching_vector and not graph_link:
                            history_only_total += 1
                        if state == "SUPERSEDED" and full_materialized:
                            compaction_ready += 1
                        if state in {"REVOKED", "REJECTED"}:
                            if full_materialized:
                                terminal_materialized += 1
                            if graph_link:
                                terminal_graph_linked += 1
                            if not full_materialized and (native_any or matching_vector or graph_link):
                                terminal_partial += 1
                    compaction_blocked = logical_counts["SUPERSEDED"] - compaction_ready
                    materialized_unknown = self._db_status_section(
                        db_snapshot_at, observation="EXACT", value=materialized_by_state
                    )
                    history_section = self._db_status_section(
                        db_snapshot_at, observation="EXACT", value=history_only_total
                    )
                    compaction_ready_section = self._db_status_section(
                        db_snapshot_at, observation="EXACT", value=compaction_ready
                    )
                    compaction_blocked_section = self._db_status_section(
                        db_snapshot_at, observation="EXACT", value=compaction_blocked
                    )
                    terminal_materialized_section = self._db_status_section(
                        db_snapshot_at, observation="EXACT", value=terminal_materialized
                    )
                    terminal_graph_section = self._db_status_section(
                        db_snapshot_at, observation="EXACT", value=terminal_graph_linked
                    )
                    terminal_partial_section = self._db_status_section(
                        db_snapshot_at, observation="EXACT", value=terminal_partial
                    )

            return {
                "db_snapshot_at": db_snapshot_at,
                "logical_counts": logical_section,
                "freshness_review_counts": freshness_section,
                "native_vector_materialized_by_state": materialized_unknown,
                "history_only_total": history_section,
                "compaction_ready": compaction_ready_section,
                "compaction_blocked_partial": compaction_blocked_section,
                "terminal_native_vector_materialized": terminal_materialized_section,
                "terminal_graph_linked": terminal_graph_section,
                "terminal_partial_materialization": terminal_partial_section,
                "db_capacity": db_capacity,
            }
        finally:
            conn.rollback()

    @staticmethod
    def _filesystem_size(path: str) -> int | None:
        try:
            return int(os.path.getsize(path))
        except FileNotFoundError:
            return 0
        except OSError:
            return None

    async def retention_status(
        self,
        *,
        scope: str,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        scope_context = self._scope(scope, project_id, worktree_id, workflow_id, agent_id)
        request_id_value = self._request_id(request_id)
        snapshot = await self._engine._run(self._retention_snapshot_sync, scope_context)
        capacity_sampled_at = iso_now()
        database_file_bytes = self._filesystem_size(self._engine.db_path)
        wal_file_bytes = self._filesystem_size(f"{self._engine.db_path}-wal")
        shm_file_bytes = self._filesystem_size(f"{self._engine.db_path}-shm")
        try:
            filesystem_free_bytes: int | None = int(
                shutil.disk_usage(os.path.dirname(self._engine.db_path)).free
            )
        except OSError:
            filesystem_free_bytes = None
        capacity_values = {
            "database_file_bytes": database_file_bytes,
            "wal_file_bytes": wal_file_bytes,
            "shm_file_bytes": shm_file_bytes,
            "database_allocated_bytes": snapshot["db_capacity"]["database_allocated_bytes"],
            "freelist_bytes": snapshot["db_capacity"]["freelist_bytes"],
            "filesystem_free_bytes": filesystem_free_bytes,
        }
        capacity_observation = (
            "KNOWN" if all(value is not None for value in capacity_values.values()) else "UNKNOWN"
        )
        response = {
            "status": "OK",
            "request_id": request_id_value,
            "scope": scope_context.to_dict(),
            "logical_counts": snapshot["logical_counts"],
            "freshness_review_counts": snapshot["freshness_review_counts"],
            "native_vector_materialized_by_state": snapshot["native_vector_materialized_by_state"],
            "history_only_total": snapshot["history_only_total"],
            "compaction_ready": snapshot["compaction_ready"],
            "compaction_blocked_partial": snapshot["compaction_blocked_partial"],
            "terminal_native_vector_materialized": snapshot["terminal_native_vector_materialized"],
            "terminal_graph_linked": snapshot["terminal_graph_linked"],
            "terminal_partial_materialization": snapshot["terminal_partial_materialization"],
            "capacity": {
                "capacity_sampled_at": capacity_sampled_at,
                "consistency": "OUTSIDE_DB_SNAPSHOT",
                "observation": capacity_observation,
                **capacity_values,
            },
        }
        if len(canonical_json(response).encode("utf-8")) > MAX_STATUS_RESPONSE_BYTES:
            raise AdapterError("STATUS_UNAVAILABLE", "retention status exceeds the 16 KiB response cap")
        return response
