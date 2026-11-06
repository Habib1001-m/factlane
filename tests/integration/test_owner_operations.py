from __future__ import annotations

import asyncio
import shutil
from typing import Any

import pytest

from factlane.adapter import MemoryAdapter, trusted_write_context_for_profile
from factlane.contract import AdapterError
from factlane.embeddings import EmbeddingProfile
from factlane.owner_control import OwnerControlService
from factlane.owner_operations import OwnerOperationsService
from factlane.storage import SQLiteVecEngine


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="owner-operations-test-256",
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


class _Usage:
    def __init__(self, free: int) -> None:
        self.free = free


class _RuntimeObserver:
    def __init__(self, observations: list[dict[str, Any]] | None = None) -> None:
        self.items = observations or []
        self.diagnostic_payload: dict[str, Any] = {}

    async def observations(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.items]

    async def diagnostics(self) -> dict[str, Any]:
        return dict(self.diagnostic_payload)


class _ConfigAdapter:
    def __init__(self) -> None:
        self.agents: list[dict[str, Any]] = []
        self.specs: dict[str, dict[str, Any]] = {
            "request_timeout_seconds": {
                "type": "integer",
                "min": 1,
                "max": 120,
                "reload_required": True,
            },
            "default_search_mode": {
                "type": "enum",
                "choices": ["TEXT", "SEMANTIC"],
                "reload_required": False,
            },
            "service_label": {
                "type": "string",
                "max_bytes": 64,
                "reload_required": False,
            },
        }
        self.configured = {
            "request_timeout_seconds": 30,
            "default_search_mode": "TEXT",
            "service_label": "FactLane local",
            "api_token": "should-never-surface",
        }
        self.effective = dict(self.configured)
        self.validated: list[dict[str, Any]] = []
        self.applied: list[dict[str, Any]] = []
        self.reload_calls: list[dict[str, Any]] = []
        self.reload_supported = False
        self.reload_confirmed = False
        self.reload_error: AdapterError | None = None

    async def configured_agents(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.agents]

    async def setting_specs(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self.specs.items()}

    async def configured_settings(self) -> dict[str, Any]:
        return dict(self.configured)

    async def effective_settings(self) -> dict[str, Any]:
        return dict(self.effective)

    async def validate_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        self.validated.append(dict(changes))
        return dict(changes)

    async def apply_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        self.applied.append(dict(changes))
        self.configured.update(changes)
        for key, value in changes.items():
            if not self.specs[key].get("reload_required", False):
                self.effective[key] = value
        return {"status": "APPLIED"}

    async def reload_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        self.reload_calls.append(dict(changes))
        if self.reload_error is not None:
            raise self.reload_error
        if self.reload_supported and self.reload_confirmed:
            self.effective.update(changes)
        return {
            "supported": self.reload_supported,
            "confirmed": self.reload_confirmed,
            "observed_at": "2026-11-06T00:30:00Z" if self.reload_confirmed else None,
        }


async def _services(tmp_path, *, observer=None, config=None):
    engine = SQLiteVecEngine(str(tmp_path / "owner-operations.db"), _profile())
    await engine.open()
    adapter = MemoryAdapter(
        engine,
        _Provider(),
        trusted_write_context=trusted_write_context_for_profile(
            "owner-current",
            contributor_ref="owner-operations-test",
        ),
    )
    owner = OwnerControlService(adapter)
    runtime_observer = observer or _RuntimeObserver()
    config_adapter = config or _ConfigAdapter()
    return engine, adapter, OwnerOperationsService(owner, runtime_observer, config_adapter)


def _provenance(key: str, marker: str) -> dict[str, str]:
    return {
        "source_class": "CURRENT_REPO",
        "source_ref": key,
        "source_hash": marker * 64,
        "review_ref": "owner-operations-tests",
        "extraction_method": "AUTOMATED_CHECK",
    }


async def _store_current(
    adapter: MemoryAdapter,
    *,
    key: str,
    fact: str,
    marker: str,
    project_id: str = "project-a",
) -> dict[str, Any]:
    result = await adapter.store(
        fact=fact,
        scope="PROJECT",
        memory_type="PROJECT_LEARNED_FACT",
        source_provenance=_provenance(key, marker),
        freshness_policy={"kind": "manual"},
        idempotency_key=key,
        project_id=project_id,
        source_timestamp="2026-11-05T20:00:00Z",
        last_verified_at="2026-11-05T20:00:00Z",
        verified_by="OWNER",
        requested_lifecycle_state="VALIDATED_CURRENT",
        confidence=0.95,
        tags=["subject:owner-operations"],
        subject=key,
    )
    return result["results"][0]


async def _superseded(
    adapter: MemoryAdapter,
    *,
    key: str,
    project_id: str = "project-a",
) -> tuple[dict[str, Any], dict[str, Any]]:
    old = await _store_current(
        adapter,
        key=f"{key}-old",
        fact=f"Old {key}",
        marker="a",
        project_id=project_id,
    )
    result = await adapter.update(
        memory_id=old["memory_id"],
        scope="PROJECT",
        project_id=project_id,
        expected_revision=old["revision"],
        mode="REPLACE",
        idempotency_key=f"{key}-replacement",
        replacement={
            "fact": f"Current {key}",
            "memory_type": "PROJECT_LEARNED_FACT",
            "source_provenance": _provenance(f"{key}-replacement", "b"),
            "freshness_policy": {"kind": "manual"},
            "source_timestamp": "2026-11-05T20:00:00Z",
            "verified_by": "OWNER",
            "tags": ["subject:owner-operations"],
            "subject": f"{key}-replacement",
        },
    )
    return old, result["results"][0]


def test_housekeeping_preview_is_exact_scope_read_only_and_run_preserves_current_authority(
    tmp_path, monkeypatch
) -> None:
    async def run() -> None:
        engine, adapter, service = await _services(tmp_path)
        try:
            old_a, current_a = await _superseded(adapter, key="scope-a", project_id="project-a")
            current_extra = await _store_current(
                adapter,
                key="current-extra",
                fact="Current authority must stay materialized.",
                marker="c",
                project_id="project-a",
            )
            old_b, _ = await _superseded(adapter, key="scope-b", project_id="project-b")
            monkeypatch.setattr(shutil, "disk_usage", lambda _path: _Usage(0))

            assert engine.conn is not None
            before_changes = engine.conn.total_changes
            preview = await service.housekeeping_preview(
                scope="PROJECT",
                project_id="project-a",
                max_records=5,
                required_free_bytes=100,
            )
            assert engine.conn.total_changes == before_changes
            assert preview["scope"]["project_id"] == "project-a"
            assert preview["eligibility"]["compaction_ready"]["value"] == 1
            assert preview["eligibility"]["selected_upper_bound"] == 1
            assert preview["capacity"]["filesystem_free_bytes"] == 0
            assert preview["capacity_requirement_met"] is False
            assert preview["mutation"] == "NONE"

            current_a_before = await engine.get_record(
                current_a["memory_id"], adapter._safe_scope("PROJECT", "project-a", None, None, None), history=True
            )
            extra_before = await engine.get_record(
                current_extra["memory_id"], adapter._safe_scope("PROJECT", "project-a", None, None, None), history=True
            )
            result = await service.run_housekeeping(
                scope="PROJECT",
                project_id="project-a",
                max_records=1,
                required_free_bytes=100,
            )
            assert result["outcome"] == "HOUSEKEEPING_COMPLETE_CAPACITY_REQUIREMENT_NOT_MET"
            assert result["mutation"] == "BOUNDED_COMPACTION"
            assert result["compacted_records"] == 1
            assert result["max_records"] == 1
            assert result["required_free_bytes"] == 100

            project_a = adapter._safe_scope("PROJECT", "project-a", None, None, None)
            project_b = adapter._safe_scope("PROJECT", "project-b", None, None, None)
            assert (await engine.get_record(old_a["memory_id"], project_a, history=True))[0]["lifecycle_state"] == "HISTORICAL"
            assert await engine.get_record(current_a["memory_id"], project_a, history=True) == current_a_before
            assert await engine.get_record(current_extra["memory_id"], project_a, history=True) == extra_before
            assert (await engine.get_record(old_b["memory_id"], project_b, history=True))[0]["lifecycle_state"] == "SUPERSEDED"
        finally:
            await adapter.close()

    asyncio.run(run())


def test_current_only_housekeeping_never_compacts_current_authority(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine, adapter, service = await _services(tmp_path)
        try:
            current = await _store_current(
                adapter,
                key="only-current",
                fact="Current authority only.",
                marker="d",
            )
            scope = adapter._safe_scope("PROJECT", "project-a", None, None, None)
            before = await engine.get_record(current["memory_id"], scope, history=True)
            monkeypatch.setattr(shutil, "disk_usage", lambda _path: _Usage(0))

            result = await service.run_housekeeping(
                scope="PROJECT",
                project_id="project-a",
                max_records=10,
                required_free_bytes=100,
            )

            assert result["outcome"] == "NO_ELIGIBLE_RECORDS_CAPACITY_REQUIREMENT_NOT_MET"
            assert result["mutation"] == "NONE"
            assert result["compacted_records"] == 0
            assert await engine.get_record(current["memory_id"], scope, history=True) == before
        finally:
            await adapter.close()

    asyncio.run(run())


def test_agent_inventory_never_infers_live_from_configuration_and_preserves_observation_truth(tmp_path) -> None:
    async def run() -> None:
        observer = _RuntimeObserver(
            [
                {
                    "agent_id": "agent-observed",
                    "host_id": "host-a",
                    "runtime_id": "runtime-live",
                    "effective_permissions": ["memory_read"],
                    "last_seen": "2026-11-06T00:20:00Z",
                    "source": "runtime-registry",
                    "live": True,
                    "stale": False,
                },
                {
                    "agent_id": "agent-stale",
                    "host_id": "host-b",
                    "runtime_id": "runtime-stale",
                    "effective_permissions": ["memory_read"],
                    "last_seen": "2026-11-05T21:00:00Z",
                    "source": "runtime-registry",
                    "live": None,
                    "stale": True,
                },
            ]
        )
        config = _ConfigAdapter()
        config.agents = [
            {
                "agent_id": "agent-configured-only",
                "host_id": "host-c",
                "configured_profile": "repo-verifier",
                "configured_permissions": ["memory_read", "memory_write"],
            },
            {
                "agent_id": "agent-observed",
                "host_id": "host-a",
                "configured_profile": "repo-verifier",
                "configured_permissions": ["memory_read"],
            },
        ]
        engine, adapter, service = await _services(tmp_path, observer=observer, config=config)
        try:
            result = await service.agent_inventory()
            rows = {(row["agent_id"], row["host_id"]): row for row in result["items"]}

            configured_only = rows[("agent-configured-only", "host-c")]
            assert configured_only["configured"] is True
            assert configured_only["observed"] is False
            assert configured_only["observation_state"] == "UNKNOWN"
            assert configured_only["live"] is None
            assert configured_only["stale"] is None
            assert configured_only["last_seen"] is None
            assert configured_only["observation_source"] is None

            observed = rows[("agent-observed", "host-a")]
            assert observed["configured"] is True
            assert observed["observed"] is True
            assert observed["observation_state"] == "OBSERVED_LIVE"
            assert observed["last_seen"] == "2026-11-06T00:20:00Z"
            assert observed["observation_source"] == "runtime-registry"

            stale = rows[("agent-stale", "host-b")]
            assert stale["configured"] is False
            assert stale["observation_state"] == "STALE"
            assert stale["stale"] is True
        finally:
            await adapter.close()

    asyncio.run(run())


def test_settings_preview_apply_enforces_allowlist_validation_and_pending_reload(tmp_path) -> None:
    async def run() -> None:
        config = _ConfigAdapter()
        engine, adapter, service = await _services(tmp_path, config=config)
        try:
            with pytest.raises(AdapterError) as unsupported:
                await service.settings_preview({"write_profile": "owner-current"})
            assert unsupported.value.code == "SETTING_NOT_ALLOWED"
            assert config.validated == []
            assert config.applied == []

            with pytest.raises(AdapterError) as invalid:
                await service.settings_preview({"request_timeout_seconds": 999})
            assert invalid.value.code == "INVALID_SETTING"
            assert config.validated == []

            preview = await service.settings_preview({"request_timeout_seconds": 60})
            assert preview["status"] == "PREVIEW"
            assert preview["changes"] == {"request_timeout_seconds": 60}
            assert preview["reload_required"] is True
            assert preview["predicted_reload_state"] == "PENDING_RELOAD"
            assert config.validated == [{"request_timeout_seconds": 60}]
            assert config.applied == []

            pending = await service.settings_apply({"request_timeout_seconds": 60})
            assert pending["status"] == "APPLIED"
            assert pending["reload_state"] == "PENDING_RELOAD"
            assert pending["configured"]["request_timeout_seconds"] == 60
            assert pending["effective"]["request_timeout_seconds"] == 30
            assert config.applied == [{"request_timeout_seconds": 60}]
            assert len(config.reload_calls) == 1

            config.reload_supported = True
            config.reload_error = AdapterError("BACKEND_UNAVAILABLE", "runtime reload failed")
            reload_failed = await service.settings_apply({"request_timeout_seconds": 65})
            assert reload_failed["reload_state"] == "PENDING_RELOAD"
            assert reload_failed["configured"]["request_timeout_seconds"] == 65
            assert reload_failed["effective"]["request_timeout_seconds"] == 30
            assert reload_failed["reload"]["confirmed"] is False
            assert reload_failed["reload"]["error_code"] == "BACKEND_UNAVAILABLE"

            config.reload_error = None
            config.reload_supported = True
            config.reload_confirmed = True
            active = await service.settings_apply({"request_timeout_seconds": 75})
            assert active["reload_state"] == "ACTIVE"
            assert active["configured"]["request_timeout_seconds"] == 75
            assert active["effective"]["request_timeout_seconds"] == 75
            assert active["reload"]["confirmed"] is True
        finally:
            await adapter.close()

    asyncio.run(run())


def test_support_report_preview_redacts_content_credentials_and_never_uploads(tmp_path) -> None:
    async def run() -> None:
        observer = _RuntimeObserver()
        observer.diagnostic_payload = {
            "safe_status": "backend reachable",
            "content": "private remembered content",
            "authorization": "Bearer this-should-not-leak-1234567890",
            "nested": {
                "token": "secret-token-value",
                "api_token": "another-secret-token",
                "note": "sk-abcdefghijklmnopqrstuvwxyz0123456789",
            },
        }
        config = _ConfigAdapter()
        config.configured["service_label"] = "Visible support label"
        config.effective["service_label"] = "Visible support label"
        engine, adapter, service = await _services(tmp_path, observer=observer, config=config)
        try:
            report = await service.support_report_preview(
                scope="PROJECT",
                project_id="project-a",
                max_records=10,
                required_free_bytes=0,
            )
            rendered = repr(report)
            assert "private remembered content" not in rendered
            assert "this-should-not-leak" not in rendered
            assert "secret-token-value" not in rendered
            assert "another-secret-token" not in rendered
            assert "sk-abcdefghijklmnopqrstuvwxyz" not in rendered
            assert report["diagnostics"]["content"] == "[REDACTED]"
            assert report["diagnostics"]["authorization"] == "[REDACTED]"
            assert report["diagnostics"]["nested"]["api_token"] == "[REDACTED]"
            assert report["diagnostics"]["safe_status"] == "backend reachable"
            assert report["delivery"] == {
                "requires_explicit_download": True,
                "automatic_upload": False,
                "uploaded": False,
                "external_telemetry": False,
            }
            assert "api_token" not in report["settings"]["configured"]
        finally:
            await adapter.close()

    asyncio.run(run())
