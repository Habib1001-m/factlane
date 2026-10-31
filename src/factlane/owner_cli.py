from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Protocol

from .adapter import MemoryAdapter, PROFILE_DEFINITIONS, trusted_write_context_for_profile
from .contract import AdapterError, SCOPES, canonical_json
from .owner_control import OwnerControlService


MAX_INPUT_BYTES = 64 * 1024

_SCOPE_FIELDS = ("scope", "project_id", "worktree_id", "workflow_id", "agent_id")
_FORBIDDEN_INPUT_FIELDS = frozenset(
    {
        "scope",
        "project_id",
        "worktree_id",
        "workflow_id",
        "agent_id",
        "db",
        "profile",
        "host_id",
        "write_profile",
        "grant_ref",
        "authorization_class",
    }
)

_MUTATION_METHODS = {
    "recheck": "manual_recheck",
    "readmit": "readmit",
    "reverify": "reverify",
    "replace": "replace",
    "revoke": "revoke",
    "reject": "reject",
    "resolve": "resolve",
}

_MUTATION_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "recheck": (
        frozenset(
            {
                "memory_id",
                "expected_record_id",
                "expected_revision",
                "observed_group_token",
                "idempotency_key",
                "evidence",
            }
        ),
        frozenset({"request_id"}),
    ),
    "readmit": (
        frozenset(
            {
                "memory_id",
                "revoked_record_id",
                "expected_revoked_revision",
                "observed_group_token",
                "idempotency_key",
                "verification",
            }
        ),
        frozenset({"request_id"}),
    ),
    "reverify": (
        frozenset(
            {
                "memory_id",
                "expected_record_id",
                "expected_revision",
                "observed_group_token",
                "idempotency_key",
                "verification",
            }
        ),
        frozenset({"request_id"}),
    ),
    "replace": (
        frozenset(
            {
                "memory_id",
                "expected_record_id",
                "expected_revision",
                "observed_group_token",
                "idempotency_key",
                "replacement",
            }
        ),
        frozenset({"request_id"}),
    ),
    "revoke": (
        frozenset(
            {
                "memory_id",
                "expected_record_id",
                "expected_revision",
                "observed_group_token",
                "idempotency_key",
                "reason_code",
            }
        ),
        frozenset({"request_id"}),
    ),
    "reject": (
        frozenset(
            {
                "memory_id",
                "expected_record_id",
                "expected_revision",
                "observed_group_token",
                "idempotency_key",
                "reason_code",
            }
        ),
        frozenset({"request_id"}),
    ),
    "resolve": (
        frozenset(
            {
                "contradiction_key",
                "observed_group_token",
                "winner_memory_id",
                "winner_record_id",
                "winner_expected_revision",
                "idempotency_key",
                "reason_code",
            }
        ),
        frozenset({"verification", "request_id"}),
    ),
}

_EXIT_3 = frozenset(
    {
        "WRITE_AUTHORIZATION_DENIED",
        "CROSS_SCOPE_DENIED",
        "INVALID_WRITE_CONTEXT",
        "SCOPE_AUTHORITY_DENIED",
    }
)
_EXIT_4 = frozenset(
    {
        "NOT_FOUND",
        "NOT_REVOKED",
        "INVALID_WINNER",
        "CANDIDATE_REPLACE_DENIED",
        "NOT_ON_CHANGE",
    }
)
_EXIT_5 = frozenset(
    {
        "VERSION_CONFLICT",
        "GROUP_VERSION_CONFLICT",
        "IDEMPOTENCY_CONFLICT",
        "LINEAGE_CHANGED",
    }
)
_EXIT_6 = frozenset(
    {
        "EVIDENCE_BINDING_MISMATCH",
        "EVIDENCE_STALE",
        "EVIDENCE_TOO_OLD_FOR_TRUST_RENEWAL",
        "EVIDENCE_NOT_YET_OBSERVED",
        "EVIDENCE_INVALID",
        "RECHECK_EVIDENCE_REQUIRED",
        "VERIFICATION_REQUIRED",
        "SEMANTIC_IDENTITY_MISMATCH",
        "WINNER_PROOF_REQUIRED",
        "WINNER_PROOF_INVALID",
        "PROVENANCE_REQUIRED",
        "UNVERIFIED_CURRENT",
        "INVALID_TIMESTAMP",
    }
)
_EXIT_7 = frozenset(
    {
        "GROUP_MEMBER_NOT_ADMISSIBLE",
        "READMIT_RECOVERY_PURGED_DENIED",
        "RECOVERY_INVALIDATED_REPLAY",
        "FRESHNESS_BLOCKED",
        "QUARANTINED",
    }
)
_EXIT_8 = frozenset(
    {
        "BACKEND_BUSY",
        "BACKEND_UNAVAILABLE",
        "MAINTENANCE_IN_PROGRESS",
        "MIGRATION_REQUIRED",
        "PROFILE_MISMATCH",
        "STATUS_UNAVAILABLE",
        "STORAGE_UNAVAILABLE",
        "STORAGE_VERSION_UNSUPPORTED",
        "TRUST_CLOCK_UNRELIABLE",
        "FRESHNESS_SEQUENCE_EXHAUSTED",
    }
)
_EXIT_9 = frozenset({"WRITE_UNCONFIRMED"})
_EXIT_11 = frozenset({"GROUP_LIMIT_EXCEEDED", "GROUP_OBSERVATION_TOO_LARGE"})
_EXIT_2 = frozenset({"INVALID_ENVELOPE", "INVALID_ENUM", "INVALID_CURSOR"})


class _OwnerService(Protocol):
    async def inventory(self, **kwargs: Any) -> dict[str, Any]: ...

    async def detail(self, **kwargs: Any) -> dict[str, Any]: ...

    async def conflicts(self, **kwargs: Any) -> dict[str, Any]: ...

    async def health(self, **kwargs: Any) -> dict[str, Any]: ...

    async def retention_status(self, **kwargs: Any) -> dict[str, Any]: ...


class CliInputError(ValueError):
    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factlane-owner",
        description="Local Owner control-plane CLI for a trusted FactLane launcher.",
        allow_abbrev=False,
    )
    parser.add_argument("--db", required=True, help="FactLane SQLite database path")
    parser.add_argument("--profile", choices=sorted(PROFILE_DEFINITIONS), default="nomic-768")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--tokenizer-path", default=None)
    parser.add_argument("--host-id", required=True, help="stable non-secret trusted launcher identity")
    parser.add_argument("--scope", required=True, choices=sorted(SCOPES))
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--worktree-id", default=None)
    parser.add_argument("--workflow-id", default=None)
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--json", action="store_true", dest="json_output", help="emit the exact structural service result")

    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory", allow_abbrev=False)
    inventory.add_argument("--lifecycle-state", default=None)
    inventory.add_argument("--contradiction-state", default=None)
    inventory.add_argument("--memory-type", default=None)
    inventory.add_argument("--limit", type=int, default=100)
    inventory.add_argument("--cursor", default=None)

    get = commands.add_parser("get", allow_abbrev=False)
    get.add_argument("memory_id")
    get.add_argument("--limit", type=int, default=100)
    get.add_argument("--cursor", default=None)

    conflicts = commands.add_parser("conflicts", allow_abbrev=False)
    conflicts.add_argument("--limit", type=int, default=100)
    conflicts.add_argument("--cursor", default=None)

    status = commands.add_parser("status", allow_abbrev=False)
    status.add_argument("--detail", choices=("health", "retention"), default="health")

    for name in _MUTATION_METHODS:
        mutation = commands.add_parser(name, allow_abbrev=False)
        mutation.add_argument(
            "--input",
            required=True,
            metavar="PATH|-",
            help="bounded JSON request body; use '-' for stdin so proof never enters argv",
        )

    return parser


def _scope_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {field: getattr(args, field) for field in _SCOPE_FIELDS}


def _read_bounded_input(location: str) -> str:
    if location == "-":
        text = sys.stdin.read(MAX_INPUT_BYTES + 1)
        if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            raise CliInputError("input exceeds the 64 KiB CLI request cap")
        return text
    path = Path(location)
    try:
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise CliInputError("input exceeds the 64 KiB CLI request cap")
        raw = path.read_bytes()
    except CliInputError:
        raise
    except OSError as exc:
        raise CliInputError("input file could not be read") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise CliInputError("input exceeds the 64 KiB CLI request cap")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliInputError("input must be UTF-8 JSON") from exc


def _load_mutation_input(command: str, location: str) -> dict[str, Any]:
    try:
        payload = json.loads(_read_bounded_input(location))
    except json.JSONDecodeError as exc:
        raise CliInputError("input must contain one JSON object") from exc
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise CliInputError("input must contain one JSON object with string keys")
    forbidden = sorted(set(payload) & _FORBIDDEN_INPUT_FIELDS)
    if forbidden:
        raise CliInputError("input may not override trusted launcher scope or authority")
    required, optional = _MUTATION_FIELDS[command]
    missing = sorted(required - set(payload))
    unexpected = sorted(set(payload) - required - optional)
    if missing:
        raise CliInputError("input is missing required structural fields: " + ", ".join(missing))
    if unexpected:
        raise CliInputError("input contains unsupported structural fields: " + ", ".join(unexpected))
    return payload


async def _invoke(
    service: _OwnerService,
    args: argparse.Namespace,
    *,
    mutation_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scope = _scope_kwargs(args)
    if args.command == "inventory":
        return await service.inventory(
            lifecycle_state=args.lifecycle_state,
            contradiction_state=args.contradiction_state,
            memory_type=args.memory_type,
            limit=args.limit,
            cursor=args.cursor,
            **scope,
        )
    if args.command == "get":
        return await service.detail(memory_id=args.memory_id, limit=args.limit, cursor=args.cursor, **scope)
    if args.command == "conflicts":
        return await service.conflicts(limit=args.limit, cursor=args.cursor, **scope)
    if args.command == "status":
        if args.detail == "retention":
            return await service.retention_status(**scope)
        return await service.health(**scope)

    payload = mutation_payload if mutation_payload is not None else _load_mutation_input(args.command, args.input)
    method_name = _MUTATION_METHODS[args.command]
    method = getattr(service, method_name, None)
    if method is None or not callable(method):
        raise AdapterError(
            "OWNER_CONTROL_UNAVAILABLE",
            f"OwnerControlService does not expose the governed {method_name} operation",
        )
    return await method(**payload, **scope)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    mutation_payload = None
    if args.command in _MUTATION_METHODS:
        mutation_payload = _load_mutation_input(args.command, args.input)
    adapter = await MemoryAdapter.create(
        args.db,
        args.profile,
        ollama_url=args.ollama_url,
        tokenizer_path=args.tokenizer_path,
        runtime_agent_id=args.host_id,
        trusted_write_context=trusted_write_context_for_profile(
            "owner-current",
            contributor_ref=args.host_id,
            grant_ref=f"launcher:owner-current:{args.host_id}",
        ),
    )
    try:
        service = OwnerControlService(adapter)
        return await _invoke(service, args, mutation_payload=mutation_payload)
    finally:
        await adapter.close()


def _exit_code_for_error(code: str) -> int:
    if code in _EXIT_11:
        return 11
    if code in _EXIT_2:
        return 2
    if code in _EXIT_3:
        return 3
    if code in _EXIT_4:
        return 4
    if code in _EXIT_5:
        return 5
    if code in _EXIT_6:
        return 6
    if code in _EXIT_7:
        return 7
    if code in _EXIT_8:
        return 8
    if code in _EXIT_9:
        return 9
    return 10


def _emit_success(result: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(canonical_json(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def _emit_error(error: AdapterError, *, json_output: bool) -> None:
    if json_output:
        print(canonical_json({"status": "BLOCKED", "error_code": error.code, "message": error.safe_message}))
    else:
        print(f"ERROR {error.code}: {error.safe_message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = asyncio.run(_run(args))
    except CliInputError as exc:
        error = AdapterError("INVALID_ENVELOPE", str(exc))
        _emit_error(error, json_output=args.json_output)
        return 2
    except AdapterError as exc:
        _emit_error(exc, json_output=args.json_output)
        return _exit_code_for_error(exc.code)
    except Exception:
        error = AdapterError("INTERNAL_ERROR", "Owner CLI failed safely")
        _emit_error(error, json_output=args.json_output)
        return 10
    _emit_success(result, json_output=args.json_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
