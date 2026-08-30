from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


def _write_bootstrap_phase(run_dir: Path, actor: str, phase: str) -> None:
    phase_path = run_dir / "phases" / f"{actor}.json"
    phase_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "actor": actor,
        "phase": phase,
        "pid": os.getpid(),
        "effective_home": os.environ.get("HOME", ""),
    }
    temporary = phase_path.with_name(f".{phase_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, phase_path)


def _bootstrap_actor_upstream_base() -> tuple[Path, str] | None:
    """Keep pinned-backend import-time state inside the disposable 4C-04 run."""
    args = sys.argv[1:]
    if not args or args[0] != "actor":
        return None
    try:
        run_dir_index = args.index("--run-dir")
        actor_index = args.index("--actor")
        run_dir = Path(args[run_dir_index + 1]).resolve()
        actor = args[actor_index + 1]
    except (ValueError, IndexError):
        return None
    os.environ["MCP_MEMORY_BASE_DIR"] = str(run_dir / "upstream-runtime" / actor)
    _write_bootstrap_phase(run_dir, actor, "BOOTSTRAP_PRE_IMPORT")
    return run_dir, actor


_BOOTSTRAP_ACTOR = _bootstrap_actor_upstream_base()

from factlane.adapter import MemoryAdapter
from factlane.contract import AdapterError
from factlane.embeddings import EmbeddingProfile
from factlane.gateway import HostBinding, MemoryGateway
from factlane.storage import SQLiteVecEngine

if _BOOTSTRAP_ACTOR is not None:
    _write_bootstrap_phase(*_BOOTSTRAP_ACTOR, "IMPORT_COMPLETE")


ACTORS = ("codex-disposable", "hermes-disposable")
BINDING_SOURCE = "s6b4c04-disposable"
STAMP = "2026-08-30T00:00:00Z"


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="s6b4c04-test-256",
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


class DeterministicProvider:
    """Acceptance-only provider that keeps 4C-04 outside embedding concurrency."""

    def __init__(self, profile: EmbeddingProfile) -> None:
        self.profile = profile
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += len(texts)
        vector = [0.0] * (self.profile.output_dimension - 1) + [1.0]
        return [list(vector) for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return [0.0] * (self.profile.output_dimension - 1) + [1.0]


def _paths(run_dir: Path) -> dict[str, Path]:
    root = run_dir.resolve()
    return {
        "root": root,
        "db": root / "shared.db",
        "manifest": root / "manifest.json",
        "ready": root / "ready",
        "results": root / "results",
        "phases": root / "phases",
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _write_phase(paths: dict[str, Path], actor: str, phase: str) -> None:
    _atomic_write_json(
        paths["phases"] / f"{actor}.json",
        {
            "actor": actor,
            "phase": phase,
            "pid": os.getpid(),
            "effective_home": os.environ.get("HOME", ""),
        },
    )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _print(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


async def _open_adapter(db_path: Path) -> tuple[SQLiteVecEngine, MemoryAdapter]:
    embedding_profile = _profile()
    engine = SQLiteVecEngine(str(db_path), embedding_profile)
    await engine.open()
    adapter = MemoryAdapter(engine, DeterministicProvider(embedding_profile))  # type: ignore[arg-type]
    return engine, adapter


async def _prepare(run_dir: Path) -> dict[str, Any]:
    paths = _paths(run_dir)
    paths["root"].mkdir(parents=True, exist_ok=False)
    paths["ready"].mkdir()
    paths["results"].mkdir()
    paths["phases"].mkdir()

    _engine, adapter = await _open_adapter(paths["db"])
    try:
        seed = await adapter.store(
            fact="FactLane 4C-04 disposable hosts share one current revision.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            source_provenance={
                "source_class": "CURRENT_REPO",
                "source_ref": "s6b4c04-seed",
                "source_hash": "a" * 64,
                "review_ref": "s6b4c04",
                "extraction_method": "AUTOMATED_CHECK",
            },
            freshness_policy={"kind": "manual"},
            idempotency_key="s6b4c04-seed",
            project_id="factlane",
            source_timestamp=STAMP,
            last_verified_at=STAMP,
            verified_by="OWNER",
            requested_lifecycle_state="VALIDATED_CURRENT",
            confidence=0.95,
            tags=["subject:disposable-host-concurrency", "s6b4c04"],
        )
        record = seed["results"][0]
    finally:
        await adapter.close()

    manifest = {
        "schema_version": 1,
        "campaign": "S6B.4C-04",
        "scenario": "REVERIFY_SHARED_PARENT",
        "db_path": str(paths["db"]),
        "memory_id": record["memory_id"],
        "parent_record_id": record["record_id"],
        "expected_revision": 1,
        "project_id": "factlane",
        "actors": list(ACTORS),
    }
    _atomic_write_json(paths["manifest"], manifest)
    return {
        "prepared": True,
        "run_dir": str(paths["root"]),
        "memory_id": manifest["memory_id"],
        "parent_record_id": manifest["parent_record_id"],
        "expected_revision": manifest["expected_revision"],
    }


def _install_file_barrier(
    engine: SQLiteVecEngine,
    *,
    actor: str,
    paths: dict[str, Path],
    manifest: dict[str, Any],
    binding: HostBinding,
    timeout_seconds: float,
) -> None:
    original_write = engine.write_record
    ready_path = paths["ready"] / f"{actor}.json"

    async def gated_write(*args: Any, **kwargs: Any) -> None:
        _atomic_write_json(
            ready_path,
            {
                "actor": actor,
                "pid": os.getpid(),
                "effective_home": os.environ.get("HOME", ""),
                "gateway_instance_id": binding.gateway_instance_id,
                "host_id": binding.bound_host_id,
                "memory_id": manifest["memory_id"],
                "parent_record_id": manifest["parent_record_id"],
                "expected_revision": manifest["expected_revision"],
                "pre_read_complete": True,
            },
        )
        _write_phase(paths, actor, "BARRIER_READY")
        deadline = time.monotonic() + timeout_seconds
        peer_paths = [paths["ready"] / f"{name}.json" for name in ACTORS]
        while not all(path.exists() for path in peer_paths):
            if time.monotonic() >= deadline:
                raise AdapterError("BARRIER_TIMEOUT", "peer execution context did not reach the shared write barrier")
            await asyncio.sleep(0.02)
        _write_phase(paths, actor, "BARRIER_RELEASED")
        await original_write(*args, **kwargs)

    engine.write_record = gated_write  # type: ignore[method-assign]


async def _actor(run_dir: Path, actor: str, timeout_seconds: float) -> dict[str, Any]:
    if actor not in ACTORS:
        raise ValueError(f"actor must be one of: {', '.join(ACTORS)}")
    paths = _paths(run_dir)
    _write_phase(paths, actor, "ACTOR_ENTER")
    manifest = _read_json(paths["manifest"])
    _write_phase(paths, actor, "MANIFEST_LOADED")
    if manifest.get("actors") != list(ACTORS):
        raise ValueError("manifest actor set does not match the 4C-04 contract")

    _write_phase(paths, actor, "OPEN_ADAPTER_START")
    engine, adapter = await _open_adapter(paths["db"])
    _write_phase(paths, actor, "OPEN_ADAPTER_COMPLETE")
    binding = HostBinding(actor, "stdio", BINDING_SOURCE)
    gateway = MemoryGateway(adapter, binding, transport_kind="stdio")
    _install_file_barrier(
        engine,
        actor=actor,
        paths=paths,
        manifest=manifest,
        binding=binding,
        timeout_seconds=timeout_seconds,
    )
    _write_phase(paths, actor, "GATEWAY_READY")

    marker = "b" if actor == ACTORS[0] else "c"
    request = {
        "memory_id": manifest["memory_id"],
        "scope": "PROJECT",
        "project_id": manifest["project_id"],
        "expected_revision": manifest["expected_revision"],
        "mode": "REVERIFY",
        "idempotency_key": f"s6b4c04-update-{actor}",
        "verification": {
            "source_provenance": {
                "source_class": "CURRENT_REPO",
                "source_ref": f"s6b4c04-{actor}",
                "source_hash": marker * 64,
                "review_ref": "s6b4c04",
                "extraction_method": "AUTOMATED_CHECK",
            },
            "source_timestamp": STAMP,
            "verified_by": "AUTOMATED_CHECK",
        },
    }

    outcome = "UNSET"
    error_code: str | None = None
    result_record_id: str | None = None
    result_revision: int | None = None
    audit_host_id: str | None = None
    audit_gateway_instance_id: str | None = None
    try:
        _write_phase(paths, actor, "DISPATCH_START")
        response = await gateway.dispatch("memory_update", request)
        _write_phase(paths, actor, "DISPATCH_COMPLETE")
        result = response["results"][0]
        audit = response["audit"]["host_binding"]
        outcome = "SUCCESS"
        result_record_id = result["record_id"]
        result_revision = result["revision"]
        audit_host_id = audit["host_id"]
        audit_gateway_instance_id = audit["gateway_instance_id"]
    except AdapterError as exc:
        if exc.code != "VERSION_CONFLICT":
            raise
        _write_phase(paths, actor, "VERSION_CONFLICT")
        outcome = "VERSION_CONFLICT"
        error_code = exc.code
    finally:
        await adapter.close()

    payload = {
        "actor": actor,
        "pid": os.getpid(),
        "effective_home": os.environ.get("HOME", ""),
        "host_id": binding.bound_host_id,
        "gateway_instance_id": binding.gateway_instance_id,
        "outcome": outcome,
        "error_code": error_code,
        "result_record_id": result_record_id,
        "result_revision": result_revision,
        "audit_host_id": audit_host_id,
        "audit_gateway_instance_id": audit_gateway_instance_id,
    }
    _atomic_write_json(paths["results"] / f"{actor}.json", payload)
    _write_phase(paths, actor, "RESULT_WRITTEN")
    return payload


async def _verify(run_dir: Path) -> tuple[dict[str, Any], bool]:
    paths = _paths(run_dir)
    manifest = _read_json(paths["manifest"])
    results = {actor: _read_json(paths["results"] / f"{actor}.json") for actor in ACTORS}
    ready = {actor: _read_json(paths["ready"] / f"{actor}.json") for actor in ACTORS}

    engine, adapter = await _open_adapter(paths["db"])
    try:
        scope = adapter._safe_scope("PROJECT", manifest["project_id"], None, None, None)
        current = await engine.get_record(manifest["memory_id"], scope, history=False)
        history = await engine.get_record(manifest["memory_id"], scope, history=True)
        loser_actor = next((actor for actor, payload in results.items() if payload["outcome"] == "VERSION_CONFLICT"), None)
        loser_row = await engine.find_idempotency(f"s6b4c04-update-{loser_actor}") if loser_actor else object()
    finally:
        await adapter.close()

    successes = [payload for payload in results.values() if payload["outcome"] == "SUCCESS"]
    conflicts = [payload for payload in results.values() if payload["outcome"] == "VERSION_CONFLICT"]
    current_rows = [row for row in history if row["lifecycle_state"] == "VALIDATED_CURRENT"]
    shared_pre_read = all(
        payload.get("pre_read_complete") is True
        and payload.get("memory_id") == manifest["memory_id"]
        and payload.get("parent_record_id") == manifest["parent_record_id"]
        and payload.get("expected_revision") == manifest["expected_revision"]
        for payload in ready.values()
    )
    process_isolation = len({payload["pid"] for payload in results.values()}) == len(ACTORS)
    home_isolation = len({payload["effective_home"] for payload in results.values()}) == len(ACTORS)
    gateway_isolation = len({payload["gateway_instance_id"] for payload in results.values()}) == len(ACTORS)
    host_bindings = {payload["host_id"] for payload in results.values()} == set(ACTORS)
    winner_matches_current = (
        len(successes) == 1
        and len(current) == 1
        and successes[0]["result_record_id"] == current[0]["record_id"]
        and successes[0]["result_revision"] == 2
    )
    winner_audit_matches = len(successes) == 1 and (
        successes[0]["audit_host_id"] == successes[0]["host_id"]
        and successes[0]["audit_gateway_instance_id"] == successes[0]["gateway_instance_id"]
    )
    partial_loser_rows = 0 if loser_actor and loser_row is None and len(history) == 2 else 1
    current_lineage_forks = max(0, len(current_rows) - 1)

    passed = all(
        (
            process_isolation,
            home_isolation,
            gateway_isolation,
            host_bindings,
            shared_pre_read,
            len(successes) == 1,
            len(conflicts) == 1,
            len(current) == 1,
            current_lineage_forks == 0,
            partial_loser_rows == 0,
            winner_matches_current,
            winner_audit_matches,
        )
    )
    payload = {
        "process_boundary_proof": "PASS" if process_isolation else "FAIL",
        "distinct_effective_homes": home_isolation,
        "distinct_gateway_instances": gateway_isolation,
        "distinct_host_bindings": host_bindings,
        "shared_pre_read_parent": shared_pre_read,
        "successful_writers": len(successes),
        "version_conflict_writers": len(conflicts),
        "current_record_count": len(current),
        "current_lineage_forks": current_lineage_forks,
        "partial_loser_rows": partial_loser_rows,
        "winner_matches_current": winner_matches_current,
        "winner_audit_matches_host_binding": winner_audit_matches,
        "lost_update_prevention": "PASS" if passed else "FAIL",
    }
    return payload, passed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Disposable S6B.4C-04 shared-store concurrency probe. It never mutates live host configuration or native memory."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--run-dir", type=Path, required=True)

    actor = subparsers.add_parser("actor")
    actor.add_argument("--run-dir", type=Path, required=True)
    actor.add_argument("--actor", choices=ACTORS, required=True)
    actor.add_argument("--timeout-seconds", type=float, default=10.0)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--run-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        payload = asyncio.run(_prepare(args.run_dir))
        _print(payload)
        return 0
    if args.command == "actor":
        payload = asyncio.run(_actor(args.run_dir, args.actor, args.timeout_seconds))
        _print(payload)
        return 0
    if args.command == "verify":
        payload, passed = asyncio.run(_verify(args.run_dir))
        _print(payload)
        return 0 if passed else 2
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
