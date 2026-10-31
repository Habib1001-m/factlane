from __future__ import annotations

import argparse
import asyncio
import json
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest

from factlane.contract import AdapterError
from factlane import owner_cli


class FakeOwnerService:
    def __init__(self, *, error: AdapterError | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error = error

    async def _record(self, name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return {"status": "OK", "operation": name, "payload": kwargs}

    async def inventory(self, **kwargs: Any) -> dict[str, Any]:
        return await self._record("inventory", kwargs)

    async def detail(self, **kwargs: Any) -> dict[str, Any]:
        return await self._record("detail", kwargs)

    async def conflicts(self, **kwargs: Any) -> dict[str, Any]:
        return await self._record("conflicts", kwargs)

    async def health(self, **kwargs: Any) -> dict[str, Any]:
        return await self._record("health", kwargs)

    async def retention_status(self, **kwargs: Any) -> dict[str, Any]:
        return await self._record("retention_status", kwargs)

    async def manual_recheck(self, **kwargs: Any) -> dict[str, Any]:
        return await self._record("manual_recheck", kwargs)

    async def readmit(self, **kwargs: Any) -> dict[str, Any]:
        return await self._record("readmit", kwargs)

    async def reverify(self, **kwargs: Any) -> dict[str, Any]:
        return await self._record("reverify", kwargs)

    async def replace(self, **kwargs: Any) -> dict[str, Any]:
        return await self._record("replace", kwargs)

    async def revoke(self, **kwargs: Any) -> dict[str, Any]:
        return await self._record("revoke", kwargs)

    async def reject(self, **kwargs: Any) -> dict[str, Any]:
        return await self._record("reject", kwargs)

    async def resolve(self, **kwargs: Any) -> dict[str, Any]:
        return await self._record("resolve", kwargs)


def _args(command: str, **kwargs: Any) -> argparse.Namespace:
    defaults = {
        "command": command,
        "scope": "PROJECT",
        "project_id": "project-a",
        "worktree_id": None,
        "workflow_id": None,
        "agent_id": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_parser_exposes_only_bounded_local_owner_commands() -> None:
    parser = owner_cli._build_parser()
    help_text = parser.format_help()
    assert "--write-profile" not in help_text
    parsed = parser.parse_args(
        ["--db", "state.db", "--host-id", "owner-cli", "--scope", "GLOBAL_USER", "inventory"]
    )
    assert parsed.command == "inventory"
    assert parsed.scope == "GLOBAL_USER"
    for scope in ("PROJECT", "WORKFLOW", "TOOL_ENVIRONMENT", "CROSS_PROJECT_WORKFLOW"):
        assert parser.parse_args(
            ["--db", "state.db", "--host-id", "owner-cli", "--scope", scope, "inventory"]
        ).scope == scope
    for unsupported in ("GLOBAL", "WORKTREE", "AGENT"):
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["--db", "state.db", "--host-id", "owner-cli", "--scope", unsupported, "inventory"]
            )
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(
            ["--db", "state.db", "--host-id", "owner-cli", "--scope", "GLOBAL_USER", "serve-remote"]
        )
    assert exc.value.code == 2


def test_mutation_input_uses_stdin_and_preserves_observed_group_token(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "a" * 64
    payload = {
        "memory_id": "11111111-1111-4111-8111-111111111111",
        "expected_record_id": "22222222-2222-4222-8222-222222222222",
        "expected_revision": 3,
        "observed_group_token": token,
        "idempotency_key": "owner-recheck-1",
        "evidence": {
            "origin": "OWNER_SUPPLIED",
            "claim": "UNAVAILABLE",
            "recheck_ref": "source-a",
            "observed_at": "2026-10-31T20:00:00Z",
            "method": "OWNER_REVIEWED_SOURCE",
        },
    }
    monkeypatch.setattr(owner_cli.sys, "stdin", StringIO(json.dumps(payload)))
    service = FakeOwnerService()
    result = asyncio.run(owner_cli._invoke(service, _args("recheck", input="-")))
    assert result["operation"] == "manual_recheck"
    assert len(service.calls) == 1
    _, kwargs = service.calls[0]
    assert kwargs["observed_group_token"] == token
    assert kwargs["evidence"] == payload["evidence"]
    assert kwargs["scope"] == "PROJECT"
    assert kwargs["project_id"] == "project-a"


def test_mutation_input_cannot_override_trusted_launcher_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "memory_id": "11111111-1111-4111-8111-111111111111",
        "expected_record_id": "22222222-2222-4222-8222-222222222222",
        "expected_revision": 3,
        "observed_group_token": "b" * 64,
        "idempotency_key": "owner-revoke-1",
        "reason_code": "OUTDATED",
        "scope": "GLOBAL",
    }
    monkeypatch.setattr(owner_cli.sys, "stdin", StringIO(json.dumps(payload)))
    with pytest.raises(owner_cli.CliInputError, match="may not override trusted launcher"):
        asyncio.run(owner_cli._invoke(FakeOwnerService(), _args("revoke", input="-")))


def test_run_validates_local_input_before_opening_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner_cli.sys, "stdin", StringIO("not-json"))
    opened = False

    async def fake_create(*args: Any, **kwargs: Any) -> None:
        nonlocal opened
        opened = True
        raise AssertionError("adapter must not open for invalid local input")

    monkeypatch.setattr(owner_cli.MemoryAdapter, "create", fake_create)
    args = _args(
        "revoke",
        input="-",
        db="state.db",
        profile="nomic-768",
        ollama_url="http://127.0.0.1:11434",
        tokenizer_path=None,
        host_id="owner-cli",
    )
    with pytest.raises(owner_cli.CliInputError):
        asyncio.run(owner_cli._run(args))
    assert opened is False


def test_group_version_conflict_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "memory_id": "11111111-1111-4111-8111-111111111111",
        "expected_record_id": "22222222-2222-4222-8222-222222222222",
        "expected_revision": 3,
        "observed_group_token": "c" * 64,
        "idempotency_key": "owner-revoke-2",
        "reason_code": "OUTDATED",
    }
    monkeypatch.setattr(owner_cli.sys, "stdin", StringIO(json.dumps(payload)))
    service = FakeOwnerService(error=AdapterError("GROUP_VERSION_CONFLICT", "group changed"))
    with pytest.raises(AdapterError, match="group changed"):
        asyncio.run(owner_cli._invoke(service, _args("revoke", input="-")))
    assert len(service.calls) == 1


@pytest.mark.parametrize(
    ("error_code", "exit_code"),
    [
        ("VERSION_CONFLICT", 5),
        ("GROUP_VERSION_CONFLICT", 5),
        ("IDEMPOTENCY_CONFLICT", 5),
        ("EVIDENCE_TOO_OLD_FOR_TRUST_RENEWAL", 6),
        ("EVIDENCE_NOT_YET_OBSERVED", 6),
        ("TRUST_CLOCK_UNRELIABLE", 8),
        ("FRESHNESS_SEQUENCE_EXHAUSTED", 8),
        ("GROUP_LIMIT_EXCEEDED", 11),
        ("GROUP_OBSERVATION_TOO_LARGE", 11),
    ],
)
def test_exit_code_mapping_separates_structural_group_overflow(error_code: str, exit_code: int) -> None:
    assert owner_cli._exit_code_for_error(error_code) == exit_code


def test_json_output_is_structural_result_parity(capsys: pytest.CaptureFixture[str]) -> None:
    result = {
        "status": "OK",
        "operation_id": "op-1",
        "idempotent_replay": False,
        "resulting_group_token": "d" * 64,
    }
    owner_cli._emit_success(result, json_output=True)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == result
    assert captured.err == ""


def test_cli_never_falls_back_to_private_adapter_when_service_operation_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "memory_id": "11111111-1111-4111-8111-111111111111",
        "expected_record_id": "22222222-2222-4222-8222-222222222222",
        "expected_revision": 3,
        "observed_group_token": "e" * 64,
        "idempotency_key": "owner-reverify-1",
        "verification": {"verified_by": "OWNER"},
    }
    monkeypatch.setattr(owner_cli.sys, "stdin", StringIO(json.dumps(payload)))
    service = SimpleNamespace()
    with pytest.raises(AdapterError) as exc:
        asyncio.run(owner_cli._invoke(service, _args("reverify", input="-")))
    assert exc.value.code == "OWNER_CONTROL_UNAVAILABLE"


def test_status_retention_uses_owner_control_service_only() -> None:
    service = FakeOwnerService()
    result = asyncio.run(owner_cli._invoke(service, _args("status", detail="retention")))
    assert result["operation"] == "retention_status"
    assert [name for name, _ in service.calls] == ["retention_status"]
