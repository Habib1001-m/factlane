from __future__ import annotations

import inspect
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .contract import AdapterError, canonical_json, contains_sensitive, iso_now, parse_iso, validate_identifier, validate_scope
from .owner_control import OwnerAdapterMaintenanceGate, OwnerControlService, owner_adapter_request


_MAX_AGENT_ROWS = 256
_MAX_OBSERVATION_ROWS = 512
_MAX_PERMISSIONS = 32
_MAX_SETTINGS = 32
_MAX_SETTING_CHANGES = 16
_MAX_SETTING_CHOICES = 32
_MAX_SETTING_STRING_BYTES = 256
_MAX_SUPPORT_REPORT_BYTES = 65_536
_MAX_SUPPORT_DEPTH = 8
_MAX_SUPPORT_ITEMS = 128

_SETTING_SPEC_FIELDS = frozenset({"type", "min", "max", "choices", "max_bytes", "reload_required"})
_SETTING_TYPES = frozenset({"boolean", "integer", "string", "enum"})
_PRIVILEGED_SETTING_TOKENS = frozenset(
    {
        "owner",
        "permission",
        "permissions",
        "role",
        "roles",
        "grant",
        "grants",
        "authorization",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "write_profile",
        "writeprofile",
    }
)
_SUPPORT_REDACT_TOKENS = frozenset(
    {
        "authorization",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "cookie",
        "cookies",
        "content",
        "fact",
        "payload",
        "source_provenance",
    }
)
_MISSING = object()


def _bounded_text(value: Any, field: str, *, maximum: int = 256, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise AdapterError("INVALID_ENVELOPE", f"{field} is required")
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise AdapterError("INVALID_ENVELOPE", f"{field} must be a bounded string")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > maximum or "\n" in normalized or "\r" in normalized:
        raise AdapterError("INVALID_ENVELOPE", f"{field} is invalid or unbounded")
    return normalized


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AdapterError("INVALID_ENVELOPE", f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdapterError("INVALID_ENVELOPE", f"{field} must be a non-negative integer")
    return value


def _key_tokens(value: str) -> set[str]:
    lowered = value.casefold()
    tokens = {token for token in re.split(r"[^a-z0-9]+", lowered) if token}
    tokens.add(lowered)
    return tokens


class OwnerOperationsService:
    """Owner-only operational reads and bounded Task-9 orchestration.

    Storage eligibility and compaction remain owned by the existing retention and
    housekeeping kernels. Runtime/config truth is supplied by injected adapters and
    is never inferred from configuration alone.
    """

    def __init__(
        self,
        owner_control: OwnerControlService,
        runtime_observer: Any,
        config_adapter: Any,
    ) -> None:
        if not isinstance(owner_control, OwnerControlService):
            raise AdapterError(
                "WRITE_AUTHORIZATION_DENIED",
                "OwnerOperationsService requires a governed OwnerControlService",
            )
        owner_context = getattr(owner_control, "_owner_context", None)
        if (
            owner_context is None
            or owner_context.authorization_class != "DIRECT_OWNER_OPERATOR"
            or owner_context.contributor_class != "OWNER"
        ):
            raise AdapterError(
                "WRITE_AUTHORIZATION_DENIED",
                "OwnerOperationsService requires direct Owner operator authority",
            )
        self._owner = owner_control
        self._engine = owner_control._engine
        self._maintenance_gate = owner_control._maintenance_gate
        self._runtime_observer = runtime_observer
        self._config = config_adapter

    def _rebind_owner_adapter(self) -> None:
        if self._maintenance_gate.state != OwnerAdapterMaintenanceGate.REOPENING:
            raise AdapterError(
                "BACKEND_UNAVAILABLE",
                "Owner operations rebinding is allowed only during governed reopen",
            )
        self._engine = self._owner._engine

    @staticmethod
    async def _invoke(adapter: Any, method: str, *args: Any) -> Any:
        function = getattr(adapter, method, None)
        if not callable(function):
            raise AdapterError(
                "OPERATIONS_ADAPTER_UNAVAILABLE",
                f"required operations adapter method is unavailable: {method}",
            )
        result = function(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    async def _invoke_optional(adapter: Any, method: str, *args: Any) -> tuple[bool, Any]:
        function = getattr(adapter, method, None)
        if not callable(function):
            return False, None
        result = function(*args)
        if inspect.isawaitable(result):
            result = await result
        return True, result

    def _request_id(self, value: str | None) -> str:
        return self._owner._request_id(value)

    @staticmethod
    def _scope(
        scope: str,
        project_id: str | None,
        worktree_id: str | None,
        workflow_id: str | None,
        agent_id: str | None,
    ):
        return validate_scope(scope, project_id, worktree_id, workflow_id, agent_id)

    @staticmethod
    def _housekeeping_inputs(max_records: Any, required_free_bytes: Any) -> tuple[int, int]:
        return _positive_int(max_records, "max_records"), _nonnegative_int(
            required_free_bytes,
            "required_free_bytes",
        )

    @owner_adapter_request
    async def housekeeping_preview(
        self,
        *,
        scope: str,
        max_records: int,
        required_free_bytes: int,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        page_limit, free_requirement = self._housekeeping_inputs(max_records, required_free_bytes)
        scope_context = self._scope(scope, project_id, worktree_id, workflow_id, agent_id)
        status = await self._owner.retention_status(
            scope=scope_context.scope,
            project_id=scope_context.project_id,
            worktree_id=scope_context.worktree_id,
            workflow_id=scope_context.workflow_id,
            agent_id=scope_context.agent_id,
        )

        ready = status["compaction_ready"]
        blocked = status["compaction_blocked_partial"]
        logical = status["logical_counts"]
        capacity = status["capacity"]
        ready_value = ready.get("value") if ready.get("observation") == "EXACT" else None
        blocked_value = blocked.get("value") if blocked.get("observation") == "EXACT" else None
        current_value = None
        if logical.get("observation") == "EXACT" and isinstance(logical.get("value"), Mapping):
            raw_current = logical["value"].get("VALIDATED_CURRENT")
            if isinstance(raw_current, int) and not isinstance(raw_current, bool):
                current_value = raw_current

        free_bytes = capacity.get("filesystem_free_bytes")
        capacity_known = capacity.get("observation") == "KNOWN" and isinstance(free_bytes, int)
        capacity_met = free_bytes >= free_requirement if capacity_known else None
        blockers: list[dict[str, Any]] = []
        if ready_value is None:
            blockers.append(
                {
                    "code": "ELIGIBILITY_UNKNOWN",
                    "reason": ready.get("reason") or "retention eligibility is unavailable",
                }
            )
        if blocked_value is None:
            blockers.append(
                {
                    "code": "PARTIAL_RECLAIM_STATUS_UNKNOWN",
                    "reason": blocked.get("reason") or "partial reclaim status is unavailable",
                }
            )
        elif blocked_value > 0:
            blockers.append(
                {
                    "code": "PARTIAL_RECLAIM_STATE",
                    "reason": "partially materialized superseded records require repair before housekeeping",
                    "count": blocked_value,
                }
            )
        if not capacity_known:
            blockers.append(
                {
                    "code": "CAPACITY_UNKNOWN",
                    "reason": "filesystem capacity is not currently observable",
                }
            )

        return {
            "status": "PREVIEW",
            "request_id": self._request_id(request_id),
            "scope": scope_context.to_dict(),
            "mutation": "NONE",
            "requires_explicit_run": True,
            "max_records": page_limit,
            "required_free_bytes": free_requirement,
            "eligibility": {
                "compaction_ready": ready,
                "compaction_blocked_partial": blocked,
                "selected_upper_bound": min(ready_value, page_limit) if ready_value is not None else None,
                "validated_current": current_value,
                "validated_current_is_eligible": False,
            },
            "blockers": blockers,
            "capacity": capacity,
            "capacity_requirement_met": capacity_met,
        }

    @owner_adapter_request
    async def run_housekeeping(
        self,
        *,
        scope: str,
        max_records: int,
        required_free_bytes: int,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        page_limit, free_requirement = self._housekeeping_inputs(max_records, required_free_bytes)
        scope_context = self._scope(scope, project_id, worktree_id, workflow_id, agent_id)
        outcome = await self._engine.housekeep_superseded(
            scope_context,
            max_records=page_limit,
            required_free_bytes=free_requirement,
        )
        return {
            "status": "OK",
            "request_id": self._request_id(request_id),
            "scope": scope_context.to_dict(),
            **outcome,
        }

    @staticmethod
    def _permission_list(value: Any, field: str) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise AdapterError("INVALID_ENVELOPE", f"{field} must be a bounded list")
        if len(value) > _MAX_PERMISSIONS:
            raise AdapterError("INVALID_ENVELOPE", f"{field} exceeds the permission bound")
        result: list[str] = []
        for index, item in enumerate(value):
            permission = _bounded_text(item, f"{field}[{index}]", maximum=96)
            assert permission is not None
            if permission not in result:
                result.append(permission)
        return result

    @classmethod
    def _configured_agent(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise AdapterError("INVALID_ENVELOPE", "configured agent entry must be an object")
        agent_id = validate_identifier(value.get("agent_id"), "agent_id", required=True)
        host_id = validate_identifier(value.get("host_id"), "host_id")
        profile = _bounded_text(value.get("configured_profile"), "configured_profile", maximum=128, required=False)
        return {
            "agent_id": agent_id,
            "host_id": host_id,
            "configured_profile": profile,
            "configured_permissions": cls._permission_list(
                value.get("configured_permissions"),
                "configured_permissions",
            ),
        }

    @classmethod
    def _observation(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise AdapterError("INVALID_ENVELOPE", "runtime observation must be an object")
        agent_id = validate_identifier(value.get("agent_id"), "agent_id", required=True)
        host_id = validate_identifier(value.get("host_id"), "host_id")
        runtime_id = validate_identifier(value.get("runtime_id"), "runtime_id")
        last_seen = _bounded_text(value.get("last_seen"), "last_seen", maximum=64, required=False)
        if last_seen is not None:
            parse_iso(last_seen, required=True)
        source = _bounded_text(value.get("source"), "observation source", maximum=128, required=False)
        live = value.get("live")
        stale = value.get("stale")
        if live is not None and not isinstance(live, bool):
            raise AdapterError("INVALID_ENVELOPE", "runtime observation live field must be boolean or null")
        if stale is not None and not isinstance(stale, bool):
            raise AdapterError("INVALID_ENVELOPE", "runtime observation stale field must be boolean or null")
        if stale is True:
            state = "STALE"
        elif live is True and stale is False:
            state = "OBSERVED_LIVE"
        elif live is False and stale is False:
            state = "OBSERVED_NOT_LIVE"
        else:
            state = "OBSERVED_UNKNOWN"
        return {
            "agent_id": agent_id,
            "host_id": host_id,
            "runtime_id": runtime_id,
            "effective_permissions": cls._permission_list(
                value.get("effective_permissions"),
                "effective_permissions",
            ),
            "last_seen": last_seen,
            "observation_source": source,
            "live": live,
            "stale": stale,
            "observation_state": state,
        }

    @owner_adapter_request
    async def agent_inventory(self, *, request_id: str | None = None) -> dict[str, Any]:
        configured_raw = await self._invoke(self._config, "configured_agents")
        observed_raw = await self._invoke(self._runtime_observer, "observations")
        if isinstance(configured_raw, (str, bytes)) or not isinstance(configured_raw, Sequence):
            raise AdapterError("INVALID_ENVELOPE", "configured agent inventory must be a list")
        if isinstance(observed_raw, (str, bytes)) or not isinstance(observed_raw, Sequence):
            raise AdapterError("INVALID_ENVELOPE", "runtime observation inventory must be a list")
        if len(configured_raw) > _MAX_AGENT_ROWS:
            raise AdapterError("INVALID_ENVELOPE", "configured agent inventory exceeds the bounded row limit")
        if len(observed_raw) > _MAX_OBSERVATION_ROWS:
            raise AdapterError("INVALID_ENVELOPE", "runtime observation inventory exceeds the bounded row limit")

        configured = [self._configured_agent(item) for item in configured_raw]
        observations = [self._observation(item) for item in observed_raw]
        configured_by_key: dict[tuple[str, str | None], dict[str, Any]] = {}
        for item in configured:
            key = (str(item["agent_id"]), item["host_id"])
            if key in configured_by_key:
                raise AdapterError("INVALID_ENVELOPE", "configured agent inventory contains duplicate identity")
            configured_by_key[key] = item

        rows: list[dict[str, Any]] = []
        observed_keys: set[tuple[str, str | None]] = set()
        for observation in observations:
            key = (str(observation["agent_id"]), observation["host_id"])
            observed_keys.add(key)
            configured_item = configured_by_key.get(key)
            rows.append(
                {
                    "agent_id": observation["agent_id"],
                    "host_id": observation["host_id"],
                    "runtime_id": observation["runtime_id"],
                    "configured": configured_item is not None,
                    "observed": True,
                    "configured_profile": configured_item["configured_profile"] if configured_item else None,
                    "configured_permissions": configured_item["configured_permissions"] if configured_item else [],
                    "effective_permissions": observation["effective_permissions"],
                    "last_seen": observation["last_seen"],
                    "observation_source": observation["observation_source"],
                    "live": observation["live"],
                    "stale": observation["stale"],
                    "observation_state": observation["observation_state"],
                }
            )
        for key, configured_item in configured_by_key.items():
            if key in observed_keys:
                continue
            rows.append(
                {
                    "agent_id": configured_item["agent_id"],
                    "host_id": configured_item["host_id"],
                    "runtime_id": None,
                    "configured": True,
                    "observed": False,
                    "configured_profile": configured_item["configured_profile"],
                    "configured_permissions": configured_item["configured_permissions"],
                    "effective_permissions": [],
                    "last_seen": None,
                    "observation_source": None,
                    "live": None,
                    "stale": None,
                    "observation_state": "UNKNOWN",
                }
            )
        rows.sort(
            key=lambda item: (
                str(item["agent_id"]),
                str(item["host_id"] or ""),
                str(item["runtime_id"] or ""),
            )
        )
        return {
            "status": "OK",
            "request_id": self._request_id(request_id),
            "observation_truth": "INJECTED_RUNTIME_OBSERVATIONS_ONLY",
            "items": rows,
            "count": len(rows),
        }

    @staticmethod
    def _privileged_setting(name: str) -> bool:
        lowered = name.casefold()
        if lowered in _PRIVILEGED_SETTING_TOKENS:
            return True
        segments = {segment for segment in re.split(r"[^a-z0-9]+", lowered) if segment}
        return bool(segments & _PRIVILEGED_SETTING_TOKENS)

    @classmethod
    def _setting_specs(cls, value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(value, Mapping):
            raise AdapterError("INVALID_ENVELOPE", "settings allowlist must be an object")
        if len(value) > _MAX_SETTINGS:
            raise AdapterError("INVALID_ENVELOPE", "settings allowlist exceeds the bounded setting limit")
        result: dict[str, dict[str, Any]] = {}
        for raw_name, raw_spec in value.items():
            name = validate_identifier(raw_name, "setting name", required=True)
            assert name is not None
            if cls._privileged_setting(name):
                continue
            if not isinstance(raw_spec, Mapping) or set(raw_spec) - _SETTING_SPEC_FIELDS:
                raise AdapterError("INVALID_ENVELOPE", f"setting specification is invalid: {name}")
            setting_type = raw_spec.get("type")
            if setting_type not in _SETTING_TYPES:
                raise AdapterError("INVALID_ENVELOPE", f"setting type is invalid: {name}")
            reload_required = raw_spec.get("reload_required", False)
            if not isinstance(reload_required, bool):
                raise AdapterError("INVALID_ENVELOPE", f"reload_required is invalid: {name}")
            spec: dict[str, Any] = {"type": setting_type, "reload_required": reload_required}
            if setting_type == "integer":
                minimum = raw_spec.get("min")
                maximum = raw_spec.get("max")
                if (
                    isinstance(minimum, bool)
                    or not isinstance(minimum, int)
                    or isinstance(maximum, bool)
                    or not isinstance(maximum, int)
                    or minimum > maximum
                ):
                    raise AdapterError("INVALID_ENVELOPE", f"integer setting bounds are invalid: {name}")
                spec.update({"min": minimum, "max": maximum})
            elif setting_type == "string":
                max_bytes = raw_spec.get("max_bytes", _MAX_SETTING_STRING_BYTES)
                if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 4096:
                    raise AdapterError("INVALID_ENVELOPE", f"string setting bound is invalid: {name}")
                spec["max_bytes"] = max_bytes
            elif setting_type == "enum":
                choices = raw_spec.get("choices")
                if (
                    isinstance(choices, (str, bytes))
                    or not isinstance(choices, Sequence)
                    or not 1 <= len(choices) <= _MAX_SETTING_CHOICES
                ):
                    raise AdapterError("INVALID_ENVELOPE", f"enum setting choices are invalid: {name}")
                normalized_choices: list[str] = []
                for index, choice in enumerate(choices):
                    normalized = _bounded_text(choice, f"{name}.choices[{index}]", maximum=128)
                    assert normalized is not None
                    if normalized not in normalized_choices:
                        normalized_choices.append(normalized)
                spec["choices"] = normalized_choices
            result[name] = spec
        return result

    @staticmethod
    def _validate_setting_value(name: str, value: Any, spec: Mapping[str, Any]) -> Any:
        setting_type = spec["type"]
        if setting_type == "boolean":
            if not isinstance(value, bool):
                raise AdapterError("INVALID_SETTING", f"{name} must be boolean")
            return value
        if setting_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise AdapterError("INVALID_SETTING", f"{name} must be an integer")
            if value < int(spec["min"]) or value > int(spec["max"]):
                raise AdapterError("INVALID_SETTING", f"{name} is outside the supported range")
            return value
        if setting_type == "string":
            if not isinstance(value, str) or len(value.encode("utf-8")) > int(spec["max_bytes"]):
                raise AdapterError("INVALID_SETTING", f"{name} must be a bounded string")
            if contains_sensitive(value):
                raise AdapterError("RAW_OR_SENSITIVE_CONTENT", f"{name} contains credential-like material")
            return value
        if setting_type == "enum":
            if not isinstance(value, str) or value not in spec["choices"]:
                raise AdapterError("INVALID_SETTING", f"{name} is not one of the supported values")
            return value
        raise AdapterError("INVALID_ENVELOPE", f"unsupported setting specification: {name}")

    async def _read_settings(self) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
        specs = self._setting_specs(await self._invoke(self._config, "setting_specs"))
        configured_raw = await self._invoke(self._config, "configured_settings")
        effective_raw = await self._invoke(self._config, "effective_settings")
        if not isinstance(configured_raw, Mapping) or not isinstance(effective_raw, Mapping):
            raise AdapterError("INVALID_ENVELOPE", "configured and effective settings must be objects")
        configured = {
            name: self._validate_setting_value(name, configured_raw[name], specs[name])
            for name in specs
            if name in configured_raw
        }
        effective = {
            name: self._validate_setting_value(name, effective_raw[name], specs[name])
            for name in specs
            if name in effective_raw
        }
        return specs, configured, effective

    @staticmethod
    def _surface_setting_value(value: Any) -> Any:
        if isinstance(value, str) and contains_sensitive(value):
            return "[REDACTED]"
        return value

    @classmethod
    def _settings_state(
        cls,
        specs: Mapping[str, Mapping[str, Any]],
        configured: Mapping[str, Any],
        effective: Mapping[str, Any],
    ) -> dict[str, Any]:
        pending = sorted(
            name
            for name, spec in specs.items()
            if spec.get("reload_required") is True
            and configured.get(name, _MISSING) != effective.get(name, _MISSING)
        )
        return {
            "configured": {
                name: cls._surface_setting_value(value)
                for name, value in configured.items()
            },
            "effective": {
                name: cls._surface_setting_value(value)
                for name, value in effective.items()
            },
            "allowlist": sorted(specs),
            "pending_reload": pending,
            "reload_state": "PENDING_RELOAD" if pending else "ACTIVE",
        }

    @owner_adapter_request
    async def settings_snapshot(self, *, request_id: str | None = None) -> dict[str, Any]:
        specs, configured, effective = await self._read_settings()
        return {
            "status": "OK",
            "request_id": self._request_id(request_id),
            **self._settings_state(specs, configured, effective),
        }

    async def _validated_setting_changes(
        self,
        changes: Any,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
        if not isinstance(changes, Mapping) or not changes or len(changes) > _MAX_SETTING_CHANGES:
            raise AdapterError("INVALID_SETTING", "settings changes must be a bounded non-empty object")
        specs, configured, effective = await self._read_settings()
        canonical: dict[str, Any] = {}
        for raw_name, raw_value in changes.items():
            if not isinstance(raw_name, str) or raw_name not in specs:
                raise AdapterError("SETTING_NOT_ALLOWED", "setting is outside the Owner-editable allowlist")
            canonical[raw_name] = self._validate_setting_value(raw_name, raw_value, specs[raw_name])

        adapter_validated = await self._invoke(self._config, "validate_settings", dict(canonical))
        if not isinstance(adapter_validated, Mapping) or set(adapter_validated) != set(canonical):
            raise AdapterError("INVALID_SETTING", "config adapter validation returned a mismatched setting set")
        validated: dict[str, Any] = {}
        for name in canonical:
            validated[name] = self._validate_setting_value(name, adapter_validated[name], specs[name])
        return specs, configured, effective, validated

    @owner_adapter_request
    async def settings_preview(
        self,
        changes: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        specs, configured, effective, validated = await self._validated_setting_changes(changes)
        proposed = dict(configured)
        proposed.update(validated)
        reload_required = any(specs[name].get("reload_required") is True for name in validated)
        predicted = self._settings_state(specs, proposed, effective)
        return {
            "status": "PREVIEW",
            "request_id": self._request_id(request_id),
            "changes": dict(validated),
            "configured": self._settings_state(specs, configured, effective)["configured"],
            "effective": self._settings_state(specs, configured, effective)["effective"],
            "reload_required": reload_required,
            "predicted_reload_state": predicted["reload_state"],
            "pending_reload": predicted["pending_reload"],
            "mutation": "NONE",
        }

    @staticmethod
    def _normalize_reload(value: Any, *, available: bool) -> dict[str, Any]:
        if not available:
            return {
                "supported": False,
                "confirmed": False,
                "observed_at": None,
                "reason": "RELOAD_ADAPTER_UNAVAILABLE",
                "error_code": None,
            }
        if not isinstance(value, Mapping):
            raise AdapterError("INVALID_SETTING", "reload adapter returned an invalid result")
        supported = value.get("supported")
        confirmed = value.get("confirmed")
        if not isinstance(supported, bool) or not isinstance(confirmed, bool):
            raise AdapterError("INVALID_SETTING", "reload adapter returned invalid confirmation flags")
        observed_at = _bounded_text(value.get("observed_at"), "reload observed_at", maximum=64, required=False)
        if observed_at is not None:
            parse_iso(observed_at, required=True)
        reason = _bounded_text(value.get("reason"), "reload reason", maximum=128, required=False)
        return {
            "supported": supported,
            "confirmed": confirmed,
            "observed_at": observed_at,
            "reason": reason,
            "error_code": None,
        }

    @owner_adapter_request
    async def settings_apply(
        self,
        changes: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        specs, _configured_before, _effective_before, validated = await self._validated_setting_changes(changes)
        apply_result = await self._invoke(self._config, "apply_settings", dict(validated))
        if not isinstance(apply_result, Mapping):
            raise AdapterError("INVALID_SETTING", "config adapter apply result is invalid")
        reload_required = any(specs[name].get("reload_required") is True for name in validated)
        reload_result: dict[str, Any] | None = None
        if reload_required:
            try:
                available, raw_reload = await self._invoke_optional(
                    self._config,
                    "reload_settings",
                    dict(validated),
                )
                reload_result = self._normalize_reload(raw_reload, available=available)
            except AdapterError as exc:
                reload_result = {
                    "supported": True,
                    "confirmed": False,
                    "observed_at": None,
                    "reason": "RELOAD_FAILED",
                    "error_code": exc.code,
                }

        current_specs, configured, effective = await self._read_settings()
        state = self._settings_state(current_specs, configured, effective)
        if reload_required and reload_result is not None and reload_result["confirmed"] is not True:
            state["reload_state"] = "PENDING_RELOAD"
            for name in validated:
                if specs[name].get("reload_required") is True and name not in state["pending_reload"]:
                    state["pending_reload"].append(name)
            state["pending_reload"].sort()
        return {
            "status": "APPLIED",
            "request_id": self._request_id(request_id),
            "changes": dict(validated),
            "reload_required": reload_required,
            "reload": reload_result,
            **state,
        }

    @classmethod
    def _redact_support(cls, value: Any, *, key: str | None = None, depth: int = 0) -> Any:
        if key is not None:
            lowered = key.casefold()
            if lowered in _SUPPORT_REDACT_TOKENS or _key_tokens(lowered) & _SUPPORT_REDACT_TOKENS:
                return "[REDACTED]"
        if depth >= _MAX_SUPPORT_DEPTH:
            return "[TRUNCATED]"
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for index, (raw_key, item) in enumerate(value.items()):
                if index >= _MAX_SUPPORT_ITEMS:
                    result["__truncated__"] = True
                    break
                output_key = str(raw_key)[:128]
                result[output_key] = cls._redact_support(item, key=output_key, depth=depth + 1)
            return result
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            items = list(value[:_MAX_SUPPORT_ITEMS]) if hasattr(value, "__getitem__") else list(value)[:_MAX_SUPPORT_ITEMS]
            result = [cls._redact_support(item, depth=depth + 1) for item in items]
            if len(value) > _MAX_SUPPORT_ITEMS:
                result.append("[TRUNCATED]")
            return result
        if isinstance(value, str):
            if contains_sensitive(value):
                return "[REDACTED]"
            if len(value.encode("utf-8")) > 1024:
                return value.encode("utf-8")[:1024].decode("utf-8", errors="ignore") + "…[TRUNCATED]"
            return value
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else "[INVALID_NUMBER]"
        return "[UNSUPPORTED_VALUE]"

    @owner_adapter_request
    async def support_report_preview(
        self,
        *,
        scope: str,
        max_records: int,
        required_free_bytes: int,
        project_id: str | None = None,
        worktree_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        report_request_id = self._request_id(request_id)
        housekeeping = await self.housekeeping_preview(
            scope=scope,
            project_id=project_id,
            worktree_id=worktree_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            max_records=max_records,
            required_free_bytes=required_free_bytes,
        )
        agents = await self.agent_inventory()
        settings = await self.settings_snapshot()
        diagnostics_available, diagnostics = await self._invoke_optional(self._runtime_observer, "diagnostics")
        if not diagnostics_available:
            diagnostics = {}
        raw = {
            "status": "PREVIEW",
            "request_id": report_request_id,
            "schema": "factlane.owner-support-preview.v1",
            "generated_at": iso_now(),
            "housekeeping": housekeeping,
            "agents": agents,
            "settings": settings,
            "diagnostics": diagnostics,
            "delivery": {
                "requires_explicit_download": True,
                "automatic_upload": False,
                "uploaded": False,
                "external_telemetry": False,
            },
        }
        redacted = self._redact_support(raw)
        if len(canonical_json(redacted).encode("utf-8")) > _MAX_SUPPORT_REPORT_BYTES:
            raise AdapterError("SUPPORT_REPORT_TOO_LARGE", "support report preview exceeds the bounded response size")
        return redacted
