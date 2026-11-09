from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract import AdapterError
from .webui_windows import WindowsWebUIError, write_owner_private_text_atomic
from .windows_security import (
    WindowsSecurityError,
    current_user_sid,
    ensure_owner_private_directory,
    validate_owner_private_path,
)


_VERSION = 1
_MAX_EVENTS = 500
_MAX_BYTES = 512 * 1024
_DOMAINS = frozenset({"SERVICE", "HOUSEKEEPING"})
_RESULTS = frozenset({"STARTED", "SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OwnerActivityJournal:
    """Owner-private bounded history for operations without an existing durable ledger."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._ensure_private_parent()
        self._load()

    def start(
        self,
        *,
        domain: str,
        operation: str,
        correlation_reference: str,
        scope: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> str:
        if domain not in _DOMAINS:
            raise AdapterError("ACTIVITY_INVALID", "activity domain is unsupported")
        if not isinstance(operation, str) or not operation or len(operation.encode("utf-8")) > 64:
            raise AdapterError("ACTIVITY_INVALID", "activity operation is invalid")
        if (
            not isinstance(correlation_reference, str)
            or not correlation_reference
            or len(correlation_reference.encode("utf-8")) > 512
        ):
            raise AdapterError("ACTIVITY_INVALID", "activity correlation reference is invalid")
        event_id = str(uuid.uuid4())
        event = {
            "event_id": event_id,
            "domain": domain,
            "operation": operation,
            "actor": "OWNER",
            "scope": self._safe_mapping(scope),
            "target": self._safe_mapping(target),
            "time": _now(),
            "updated_at": _now(),
            "result": "STARTED",
            "correlation_reference": correlation_reference,
            "detail": None,
        }
        with self._lock:
            events = self._load()
            events.append(event)
            self._write(events[-_MAX_EVENTS:])
        return event_id

    def finish(self, event_id: str, *, result: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        if result not in _RESULTS - {"STARTED"}:
            raise AdapterError("ACTIVITY_INVALID", "activity terminal result is invalid")
        try:
            event_id = str(uuid.UUID(event_id))
        except (ValueError, AttributeError) as exc:
            raise AdapterError("ACTIVITY_INVALID", "activity event identity is invalid") from exc
        safe_detail = self._safe_mapping(detail)
        with self._lock:
            events = self._load()
            matches = [event for event in events if event.get("event_id") == event_id]
            if len(matches) != 1:
                raise AdapterError("ACTIVITY_NOT_FOUND", "activity event was not found")
            event = matches[0]
            event["result"] = result
            event["detail"] = safe_detail
            event["updated_at"] = _now()
            self._write(events)
            return dict(event)

    def list(
        self,
        *,
        limit: int = 100,
        domain: str | None = None,
        operation: str | None = None,
        result: str | None = None,
        occurred_after: str | None = None,
        occurred_before: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise AdapterError("ACTIVITY_FILTER_INVALID", "activity limit must be from 1 through 100")
        if domain is not None and domain not in _DOMAINS:
            raise AdapterError("ACTIVITY_FILTER_INVALID", "activity domain is unsupported")
        if result is not None and result not in _RESULTS:
            raise AdapterError("ACTIVITY_FILTER_INVALID", "activity result is unsupported")
        after = self._timestamp(occurred_after)
        before = self._timestamp(occurred_before)
        with self._lock:
            events = self._load()
        filtered = [
            event
            for event in events
            if (domain is None or event["domain"] == domain)
            and (operation is None or event["operation"] == operation)
            and (result is None or event["result"] == result)
            and (after is None or self._timestamp(event["time"]) >= after)
            and (before is None or self._timestamp(event["time"]) <= before)
        ]
        filtered.sort(key=lambda event: (event["time"], event["event_id"]), reverse=True)
        return {"status": "OK", "items": filtered[:limit], "truncated": len(filtered) > limit}

    @staticmethod
    def _timestamp(value: str | None) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise AdapterError("ACTIVITY_FILTER_INVALID", "activity timestamp must be text")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AdapterError("ACTIVITY_FILTER_INVALID", "activity timestamp is invalid") from exc
        if parsed.tzinfo is None:
            raise AdapterError("ACTIVITY_FILTER_INVALID", "activity timestamp must include timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _safe_mapping(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise AdapterError("ACTIVITY_INVALID", "activity metadata must be an object")
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise AdapterError("ACTIVITY_INVALID", "activity metadata key is invalid")
            if item is None or isinstance(item, (bool, int, float)):
                safe[key] = item
            elif isinstance(item, str) and len(item.encode("utf-8")) <= 512:
                safe[key] = item
            else:
                raise AdapterError("ACTIVITY_INVALID", "activity metadata value is invalid")
        return safe

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        if os.name == "nt":
            try:
                validate_owner_private_path(self._path, owner_sid=current_user_sid(), directory=False)
            except WindowsSecurityError as exc:
                raise AdapterError("ACTIVITY_JOURNAL_INVALID", "activity journal is not owner-private") from exc
        try:
            if self._path.stat().st_size > _MAX_BYTES:
                raise AdapterError("ACTIVITY_JOURNAL_INVALID", "activity journal exceeds its bounded size")
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError("ACTIVITY_JOURNAL_INVALID", "activity journal cannot be read") from exc
        if not isinstance(raw, dict) or raw.get("version") != _VERSION or not isinstance(raw.get("events"), list):
            raise AdapterError("ACTIVITY_JOURNAL_INVALID", "activity journal is malformed")
        if len(raw["events"]) > _MAX_EVENTS:
            raise AdapterError("ACTIVITY_JOURNAL_INVALID", "activity journal exceeds its bounded event count")
        events: list[dict[str, Any]] = []
        for event in raw["events"]:
            if not isinstance(event, dict) or set(event) != {
                "event_id",
                "domain",
                "operation",
                "actor",
                "scope",
                "target",
                "time",
                "updated_at",
                "result",
                "correlation_reference",
                "detail",
            }:
                raise AdapterError("ACTIVITY_JOURNAL_INVALID", "activity journal contains a malformed event")
            try:
                uuid.UUID(str(event["event_id"]))
            except (ValueError, AttributeError) as exc:
                raise AdapterError("ACTIVITY_JOURNAL_INVALID", "activity journal event identity is invalid") from exc
            if event["domain"] not in _DOMAINS or event["result"] not in _RESULTS or event["actor"] != "OWNER":
                raise AdapterError("ACTIVITY_JOURNAL_INVALID", "activity journal event classification is invalid")
            self._timestamp(event["time"])
            self._timestamp(event["updated_at"])
            self._safe_mapping(event["scope"])
            self._safe_mapping(event["target"])
            self._safe_mapping(event["detail"])
            events.append(dict(event))
        if os.name != "nt" and self._path.stat().st_mode & 0o077:
            os.chmod(self._path, 0o600)
        return events

    def _write(self, events: list[dict[str, Any]]) -> None:
        payload = json.dumps({"version": _VERSION, "events": events}, sort_keys=True, separators=(",", ":")) + "\n"
        if len(payload.encode("utf-8")) > _MAX_BYTES:
            raise AdapterError("ACTIVITY_JOURNAL_INVALID", "activity journal exceeds its bounded size")
        self._ensure_private_parent()
        if os.name == "nt":
            try:
                write_owner_private_text_atomic(str(self._path), payload, owner_sid=current_user_sid())
            except WindowsWebUIError as exc:
                raise AdapterError("ACTIVITY_JOURNAL_WRITE_FAILED", "activity journal could not be persisted") from exc
            return
        fd, temp_name = tempfile.mkstemp(prefix=f".{self._path.name}.", dir=self._path.parent)
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
            os.chmod(self._path, 0o600)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            temp_path.unlink(missing_ok=True)
            raise AdapterError("ACTIVITY_JOURNAL_WRITE_FAILED", "activity journal could not be persisted") from exc

    def _ensure_private_parent(self) -> None:
        if os.name == "nt":
            try:
                ensure_owner_private_directory(self._path.parent, owner_sid=current_user_sid())
            except WindowsSecurityError as exc:
                raise AdapterError("ACTIVITY_JOURNAL_INVALID", "activity journal directory is not owner-private") from exc
            return
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self._path.parent, 0o700)
        except OSError as exc:
            raise AdapterError("ACTIVITY_JOURNAL_INVALID", "activity journal directory is not owner-private") from exc
