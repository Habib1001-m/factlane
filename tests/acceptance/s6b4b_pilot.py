from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from factlane.adapter import MemoryAdapter, PROFILE_DEFINITIONS, TokenCounter
from factlane.contract import AdapterError, canonical_json, iso_now
from factlane.embeddings import OllamaLocalProvider


TOKENIZER_DEFAULT = str(
    Path.home()
    / ".cache/huggingface/hub/models--Systran--faster-whisper-base/snapshots/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66/tokenizer.json"
)


def digest_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return iso_now()


def run_capture(args: list[str]) -> str:
    try:
        return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True, timeout=30)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}\n"


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def p50(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))]


def file_family_size(path: Path) -> int:
    return sum(q.stat().st_size for q in path.parent.glob(path.name + "*") if q.is_file())


def file_family_snapshot(path: Path) -> dict[str, str]:
    return {
        q.name: hashlib.sha256(q.read_bytes()).hexdigest()
        for q in sorted(path.parent.glob(path.name + "*"))
        if q.is_file()
    }


def provenance(fixture_id: str, review: str = "s6b4b-synthetic-fixtures-v1") -> dict[str, str]:
    return {
        "source_class": "PILOT_SYNTHETIC",
        "source_ref": fixture_id,
        "source_hash": digest_id(fixture_id),
        "review_ref": review,
        "extraction_method": "PILOT_SYNTHETIC",
    }


def current_args(fixture_id: str, *, subject: str, fact: str, scope: str, memory_type: str, project_id: str | None = None, agent_id: str | None = None, idempotency: str | None = None) -> dict[str, Any]:
    stamp = now_iso()
    return {
        "fact": fact,
        "scope": scope,
        "memory_type": memory_type,
        "source_provenance": provenance(fixture_id),
        "freshness_policy": {"kind": "manual"},
        "idempotency_key": idempotency or f"store-{fixture_id}",
        "project_id": project_id,
        "agent_id": agent_id,
        "source_timestamp": stamp,
        "last_verified_at": stamp,
        "verified_by": "OWNER",
        "requested_lifecycle_state": "VALIDATED_CURRENT",
        "confidence": 0.9,
        "tags": [f"subject:{subject}", "synthetic", "pilot"],
        "request_id": f"request-{fixture_id}",
    }


def candidate_args(fixture_id: str, *, subject: str, fact: str, scope: str, memory_type: str, project_id: str | None = None, agent_id: str | None = None) -> dict[str, Any]:
    return {
        "fact": fact,
        "scope": scope,
        "memory_type": memory_type,
        "source_provenance": provenance(fixture_id),
        "freshness_policy": {"kind": "manual"},
        "idempotency_key": f"store-{fixture_id}",
        "project_id": project_id,
        "agent_id": agent_id,
        "source_timestamp": None,
        "last_verified_at": None,
        "verified_by": "UNVERIFIED",
        "requested_lifecycle_state": "CANDIDATE",
        "confidence": 0.5,
        "tags": [f"subject:{subject}", "synthetic", "pilot"],
        "request_id": f"request-{fixture_id}",
    }


async def verify_models(ollama_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    providers: dict[str, OllamaLocalProvider] = {}
    for profile_name in ("nomic-768", "nomic-512", "nomic-256", "minilm-384"):
        definition = PROFILE_DEFINITIONS[profile_name]
        provider = OllamaLocalProvider(
            model=definition["model"],
            profile_id=profile_name,
            output_dimension=definition["output_dimension"],
            source_dimension=definition["source_dimension"],
            model_digest={
                "nomic-embed-text:latest": "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
                "all-minilm:l6-v2": "1b226e2802dbb772b5fc32a58f103ca1804ef7501331012de126ab22f67475ef",
            }[definition["model"]],
            document_prefix=definition["document_prefix"],
            query_prefix=definition["query_prefix"],
            base_url=ollama_url,
        )
        providers[profile_name] = provider

    statuses: dict[str, Any] = {}
    for name, provider in providers.items():
        statuses[name] = provider.provider_status()
        # One local document/query call proves the provider boundary and captures
        # normalized finite output without retaining the vector itself.
        doc = provider.embed_documents(["A bounded local embedding probe."])[0]
        query = provider.embed_query("Which local embedding profile is active?")
        statuses[name]["probe_document_dimension"] = len(doc)
        statuses[name]["probe_query_dimension"] = len(query)
        statuses[name]["probe_document_norm"] = round(sum(v * v for v in doc) ** 0.5, 9)
        statuses[name]["probe_query_norm"] = round(sum(v * v for v in query) ** 0.5, 9)
        statuses[name]["local_embedding_calls"] = provider.document_calls + provider.query_calls

    text = "The adapter preserves exact scope and explicit lineage."
    assert not text.startswith("search_document: "), "provider owns the document prefix"
    full = providers["nomic-768"].embed_documents([text])[0]
    matryoshka: dict[str, Any] = {
        "model": "nomic-embed-text:latest",
        "model_digest": statuses["nomic-768"]["digest"],
        "native_dimension": len(full),
        "distance_metric": "cosine",
        "query_document_prefix_policy": {
            "documents": "search_document: ",
            "queries": "search_query: ",
            "source": "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5",
        },
        "truncation_method": "Ollama /api/embed dimensions parameter",
        "post_truncation_normalization": "observed unit norm after API projection",
        "profiles": {},
    }
    for profile_name, dimension in (("nomic-768", 768), ("nomic-512", 512), ("nomic-256", 256)):
        projected = providers[profile_name].embed_documents([text])[0]
        prefix = full[:dimension]
        dot = sum(a * b for a, b in zip(projected, prefix, strict=True))
        norm_a = sum(v * v for v in projected) ** 0.5
        norm_b = sum(v * v for v in prefix) ** 0.5
        cosine = dot / (norm_a * norm_b)
        matryoshka["profiles"][str(dimension)] = {
            "dimension": dimension,
            "api_norm": round(norm_a, 9),
            "full_prefix_norm": round(norm_b, 9),
            "cosine_api_to_full_prefix": round(cosine, 9),
            "result": "PASS" if abs(cosine - 1.0) < 1e-6 and abs(norm_a - 1.0) < 1e-3 else "HOLD",
        }
    matryoshka["all_required_profiles_pass"] = all(row["result"] == "PASS" for row in matryoshka["profiles"].values())
    return statuses, matryoshka


async def run_contract_pilot(db: Path, tokenizer: str, ollama_url: str) -> dict[str, Any]:
    adapter = await MemoryAdapter.create(str(db), "nomic-768", ollama_url=ollama_url, tokenizer_path=tokenizer)
    evidence: dict[str, Any] = {"profile": "nomic-768", "fixtures": {}, "checks": {}}
    try:
        evidence["status_before"] = await adapter.status(scope="PROJECT", project_id="project-alpha")
        alpha_input = current_args(
            "F-PROJECT-A-01",
            subject="project-store",
            fact="Project alpha uses an isolated adapter pilot store.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            project_id="project-alpha",
        )
        alpha = await adapter.store(**alpha_input)
        beta = await adapter.store(**current_args(
            "F-PROJECT-B-01",
            subject="project-store",
            fact="Project beta uses an isolated adapter pilot store.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            project_id="project-beta",
        ))
        global_result = await adapter.store(**current_args(
            "F-GLOBAL-01",
            subject="owner-preference",
            fact="The pilot owner prefers bounded memory context.",
            scope="GLOBAL_USER",
            memory_type="PREFERENCE",
        ))
        tool_result = await adapter.store(**current_args(
            "F-TOOL-01",
            subject="tool-state",
            fact="The synthetic pilot tool uses a local embedding provider.",
            scope="TOOL_ENVIRONMENT",
            memory_type="TOOL_ENVIRONMENT_FACT",
            agent_id="pilot-tool",
        ))
        stale_args = current_args(
            "F-STALE-01",
            subject="stale-rule",
            fact="This synthetic rule has an expired freshness window.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            project_id="project-alpha",
        )
        stale_args.update({
            "freshness_policy": {"kind": "ttl", "ttl_seconds": 1},
            "source_timestamp": "2020-01-01T00:00:00Z",
            "last_verified_at": "2020-01-01T00:00:00Z",
        })
        stale = await adapter.store(**stale_args)
        truth_old = await adapter.store(**current_args(
            "F-CONFLICT-OLD",
            subject="truth-rule",
            fact="Project alpha keeps the pilot database private.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            project_id="project-alpha",
        ))
        near = await adapter.store(**candidate_args(
            "F-NEAR-01",
            subject="project-store",
            fact="Project alpha uses a separated adapter pilot database.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            project_id="project-alpha",
        ))
        truth_new = await adapter.store(**current_args(
            "F-CONFLICT-NEW",
            subject="truth-rule",
            fact="Project alpha shares the pilot database with every project.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            project_id="project-alpha",
        ))
        quarantine = await adapter.store(**candidate_args(
            "F-QUARANTINE-01",
            subject="truth-rule",
            fact="Unresolved candidate requires owner review before use.",
            scope="PROJECT",
            memory_type="PROJECT_LEARNED_FACT",
            project_id="project-alpha",
        ))
        duplicate = await adapter.store(**alpha_input)
        evidence["fixtures"].update({
            "alpha": alpha,
            "beta": beta,
            "global": global_result,
            "tool": tool_result,
            "stale": stale,
            "truth_old": truth_old,
            "near": near,
            "truth_new": truth_new,
            "quarantine": quarantine,
            "duplicate": duplicate,
        })
        alpha_id = alpha["results"][0]["memory_id"]
        beta_id = beta["results"][0]["memory_id"]
        global_id = global_result["results"][0]["memory_id"]

        search_snapshot_before = file_family_snapshot(db)
        exact = await adapter.search(
            query="Project alpha uses an isolated adapter pilot store.",
            intent_class="PROJECT_DESIGN_RATIONALE",
            scope="PROJECT",
            project_id="project-alpha",
            retrieval_mode_kind="EXACT",
        )
        keyword = await adapter.search(
            query="isolated adapter",
            intent_class="PROJECT_DESIGN_RATIONALE",
            scope="PROJECT",
            project_id="project-alpha",
            retrieval_mode_kind="KEYWORD",
        )
        semantic = await adapter.search(
            query="Which project uses an isolated pilot store?",
            intent_class="PROJECT_DESIGN_RATIONALE",
            scope="PROJECT",
            project_id="project-alpha",
            retrieval_mode_kind="SEMANTIC",
        )
        hybrid = await adapter.search(
            query="Which project uses an isolated pilot store?",
            intent_class="PROJECT_DESIGN_RATIONALE",
            scope="PROJECT",
            project_id="project-alpha",
            retrieval_mode_kind="HYBRID",
        )
        alpha_scope = await adapter.search(
            query="pilot store",
            intent_class="PROJECT_DESIGN_RATIONALE",
            scope="PROJECT",
            project_id="project-alpha",
        )
        beta_scope = await adapter.search(
            query="pilot store",
            intent_class="PROJECT_DESIGN_RATIONALE",
            scope="PROJECT",
            project_id="project-beta",
        )
        global_scope = await adapter.search(
            query="bounded memory context",
            intent_class="USER_PREFERENCE_OR_DURABLE_FACT",
            scope="GLOBAL_USER",
        )
        stale_search = await adapter.search(
            query="This synthetic rule has an expired freshness window.",
            intent_class="PROJECT_DESIGN_RATIONALE",
            scope="PROJECT",
            project_id="project-alpha",
            retrieval_mode_kind="EXACT",
        )
        skip = await adapter.search(
            query="anything",
            intent_class="GENERAL_TASK_NO_MEMORY_REQUIRED",
            scope="PROJECT",
            project_id="project-alpha",
            direct_truth_available=True,
        )
        search_snapshot_after = file_family_snapshot(db)
        unauthorized_writes = int(search_snapshot_before != search_snapshot_after)
        evidence["search_storage_snapshot_before"] = search_snapshot_before
        evidence["search_storage_snapshot_after"] = search_snapshot_after
        evidence["checks"]["unauthorized_writes"] = unauthorized_writes
        evidence["searches"] = {
            "exact": exact,
            "keyword": keyword,
            "semantic": semantic,
            "hybrid": hybrid,
            "alpha_scope": alpha_scope,
            "beta_scope": beta_scope,
            "global_scope": global_scope,
            "stale": stale_search,
            "skip": skip,
        }
        all_scope_checks = []
        for result in (alpha_scope, beta_scope, global_scope):
            requested = result["scope"]
            all_scope_checks.extend(
                row["scope"] == requested for row in result.get("results", [])
            )
        cross_scope_leaks = 0 if all(all_scope_checks) else sum(not value for value in all_scope_checks)
        try:
            await adapter.get(memory_id=alpha_id, scope="PROJECT", project_id="project-beta")
            cross_scope_denied = False
        except AdapterError as exc:
            cross_scope_denied = exc.code == "CROSS_SCOPE_DENIED"
        try:
            await adapter.search(
                query="x",
                intent_class="PROJECT_DESIGN_RATIONALE",
                scope="PROJECT",
            )
            unknown_project_denied = False
        except AdapterError as exc:
            unknown_project_denied = exc.code == "UNKNOWN_PROJECT_ID"
        try:
            await adapter.store(**{**current_args(
                "F-SECRET-01",
                subject="secret",
                fact="api_key=synthetic_fixture_value_12345",
                scope="PROJECT",
                memory_type="PROJECT_LEARNED_FACT",
                project_id="project-alpha",
            ), "idempotency_key": "store-F-SECRET-01"})
            secret_rejected = False
        except AdapterError as exc:
            secret_rejected = exc.code == "RAW_OR_SENSITIVE_CONTENT"
        try:
            await adapter.store(**{**current_args(
                "F-UNVERIFIED-01",
                subject="unverified",
                fact="A current claim without verification.",
                scope="PROJECT",
                memory_type="PROJECT_LEARNED_FACT",
                project_id="project-alpha",
            ), "last_verified_at": None, "verified_by": "UNVERIFIED"})
            unverified_rejected = False
        except AdapterError as exc:
            unverified_rejected = exc.code == "UNVERIFIED_CURRENT"

        replacement_fact = "Project alpha uses a dedicated isolated adapter pilot database."
        replacement_payload = {
            "fact": replacement_fact,
            "memory_type": "PROJECT_LEARNED_FACT",
            "source_provenance": provenance("F-REPLACE-A-01"),
            "source_timestamp": now_iso(),
            "freshness_policy": {"kind": "manual"},
            "verified_by": "OWNER",
            "last_verified_at": now_iso(),
            "confidence": 0.95,
            "tags": ["subject:project-store", "synthetic", "pilot"],
        }
        replacement_request = {
            "memory_id": alpha_id,
            "scope": "PROJECT",
            "project_id": "project-alpha",
            "expected_revision": 1,
            "mode": "REPLACE",
            "idempotency_key": "update-F-REPLACE-A-01",
            "replacement": replacement_payload,
            "request_id": "request-F-REPLACE-A-01",
        }
        replacement = await adapter.update(**replacement_request)
        replacement_replay = await adapter.update(
            **{**replacement_request, "request_id": "request-F-REPLACE-A-01-replay"}
        )
        reverify_payload = {
            "source_provenance": provenance("F-REVERIFY-GLOBAL-01"),
            "source_timestamp": now_iso(),
            "freshness_policy": {"kind": "manual"},
            "verified_by": "OWNER",
            "last_verified_at": now_iso(),
            "tags": ["subject:owner-preference", "synthetic", "pilot"],
        }
        reverify_request = {
            "memory_id": global_id,
            "scope": "GLOBAL_USER",
            "expected_revision": 1,
            "mode": "REVERIFY",
            "idempotency_key": "update-F-REVERIFY-GLOBAL-01",
            "verification": reverify_payload,
            "request_id": "request-F-REVERIFY-GLOBAL-01",
        }
        reverify = await adapter.update(**reverify_request)
        reverify_replay = await adapter.update(
            **{**reverify_request, "request_id": "request-F-REVERIFY-GLOBAL-01-replay"}
        )
        replacement_id = replacement["results"][0]["memory_id"]
        history = await adapter.get(
            memory_id=alpha_id,
            scope="PROJECT",
            project_id="project-alpha",
            retrieval_mode="REVIEW_HISTORY",
        )
        current_replacement = await adapter.get(
            memory_id=replacement_id,
            scope="PROJECT",
            project_id="project-alpha",
        )
        old_query = await adapter.search(
            query="Project alpha uses an isolated adapter pilot store.",
            intent_class="PROJECT_DESIGN_RATIONALE",
            scope="PROJECT",
            project_id="project-alpha",
            retrieval_mode_kind="EXACT",
        )
        after_status = await adapter.status(scope="PROJECT", project_id="project-alpha")
        evidence["updates"] = {"replacement": replacement, "replacement_replay": replacement_replay, "reverify": reverify, "reverify_replay": reverify_replay, "history": history, "current_replacement": current_replacement, "old_query": old_query}
        evidence["checks"].update({
            "exact_result": bool(exact["results"]),
            "keyword_result": bool(keyword["results"]),
            "semantic_result": bool(semantic["results"]),
            "hybrid_result": bool(hybrid["results"]),
            "memory_can_be_skipped": skip["status"] == "NO_MEMORY_NEEDED" and not skip["memory_needed"],
            "cross_scope_leaks": cross_scope_leaks,
            "cross_scope_denied": cross_scope_denied,
            "unknown_project_denied": unknown_project_denied,
            "stale_current_results": len(stale_search["results"]),
            "stale_only_status": stale_search.get("degradation") == "STALE_ONLY",
            "contradiction_surfaced": truth_new.get("status") == "CONTRADICTION" and bool(truth_new.get("contradictions")),
            "exact_duplicate_idempotent": duplicate.get("idempotent_replay") is True,
            "update_replay_idempotent": replacement_replay.get("idempotent_replay") is True and reverify_replay.get("idempotent_replay") is True,
            "secret_admission_rejected": secret_rejected,
            "unverified_current_rejected": unverified_rejected,
            "read_after_write": all(bool(item.get("results")) for item in (alpha, beta, global_result, tool_result, stale, truth_old, near, truth_new, quarantine, replacement, reverify)),
            "replacement_new_logical_id": replacement_id != alpha_id,
            "replacement_old_preserved": any(row["memory_id"] == alpha_id and row["lifecycle_state"] == "SUPERSEDED" for row in history["results"]),
            "old_excluded_from_current": all(row["memory_id"] != alpha_id for row in old_query["results"]),
            "reverify_same_logical_id": reverify["results"][0]["memory_id"] == global_id and reverify["results"][0]["revision"] == 2,
            "current_status_readback": after_status["backend_status"]["counts"]["VALIDATED_CURRENT"] >= 1,
        })
        evidence["status_after"] = after_status
        evidence["memory_ids"] = {"alpha": alpha_id, "beta": beta_id, "global": global_id, "replacement": replacement_id}
        evidence["acceptance"] = {
            "CROSS_SCOPE_LEAKS": cross_scope_leaks,
            "STALE_RESULT_RETRIEVALS_IN_CURRENT_MODE": len(stale_search["results"]),
            "SUPERSEDED_CURRENT_RESULTS": sum(row["memory_id"] == alpha_id for row in old_query["results"]),
            "UNAUTHORIZED_WRITES": unauthorized_writes,
            "CONTRADICTION_SURFACED": "100_PERCENT_OF_FIXTURES" if evidence["checks"]["contradiction_surfaced"] else "HOLD",
            "RAW_TRANSCRIPT_OR_SECRET_ADMISSIONS": 0 if secret_rejected else 1,
            "READ_AFTER_WRITE": "PASS" if evidence["checks"]["read_after_write"] else "HOLD",
            "LOGICAL_ID_LINEAGE": "PASS" if evidence["checks"]["replacement_new_logical_id"] and evidence["checks"]["replacement_old_preserved"] and evidence["checks"]["reverify_same_logical_id"] else "HOLD",
        }
        return evidence
    finally:
        await adapter.close()


COMPARISON_DOCS = [
    ("D-SCOPE", "The memory adapter enforces exact project scope and denies cross-scope queries."),
    ("D-ROUTER", "The truth router skips memory when current repository truth answers the task."),
    ("D-PROFILE", "Embedding profiles use cosine distance and isolated vector spaces."),
    ("D-STALE", "A stale record is excluded from current retrieval after its TTL expires."),
    ("D-LINEAGE", "Supersession creates a new logical record and preserves explicit lineage."),
    ("D-CONFLICT", "Contradictions remain visible and are never resolved by majority vote."),
    ("D-ARABIC", "يجب أن تبقى ذاكرة المشروع ضمن نطاقه ولا تتسرب إلى مشروع آخر."),
    ("D-MIXED", "SQLite-vec serves the five MCP memory tools with bounded context budgets."),
    ("D-ID", "The stable logical memory_id is separate from the changing content hash."),
    ("D-FRESH", "Access frequency never refreshes last_verified_at or truth validity."),
]
COMPARISON_QUERIES = [
    ("Q-SCOPE", "Which rule prevents memory from leaking between projects?", {"D-SCOPE"}),
    ("Q-ROUTER", "When can the truth router skip memory?", {"D-ROUTER"}),
    ("Q-PROFILE", "Which profiles use cosine distance and separate vector spaces?", {"D-PROFILE"}),
    ("Q-STALE", "What happens to a record after its TTL expires?", {"D-STALE"}),
    ("Q-LINEAGE", "How does replacement preserve memory lineage?", {"D-LINEAGE"}),
    ("Q-CONFLICT", "How are contradictions shown without majority voting?", {"D-CONFLICT"}),
    ("Q-ARABIC", "ما هي قاعدة نطاق ذاكرة المشروع؟", {"D-ARABIC", "D-SCOPE"}),
    ("Q-MIXED", "Which SQLite-vec MCP surface bounds context?", {"D-MIXED"}),
    ("Q-ID", "What remains stable when the content hash changes?", {"D-ID"}),
    ("Q-FRESH", "Does repeated access verify a memory again?", {"D-FRESH"}),
]


async def run_profile_comparison(root: Path, tokenizer: str, ollama_url: str, profile_name: str) -> dict[str, Any]:
    db = root / "data" / f"comparison-{profile_name}.sqlite"
    started = time.perf_counter()
    adapter = await MemoryAdapter.create(str(db), profile_name, ollama_url=ollama_url, tokenizer_path=tokenizer)
    open_ms = (time.perf_counter() - started) * 1000
    store_times: list[float] = []
    search_times: list[float] = []
    memory_by_fixture: dict[str, str] = {}
    try:
        for fixture_id, fact in COMPARISON_DOCS:
            args = current_args(
                fixture_id,
                subject=fixture_id,
                fact=fact,
                scope="PROJECT",
                memory_type="PROJECT_LEARNED_FACT",
                project_id="comparison-project",
            )
            started_store = time.perf_counter()
            result = await adapter.store(**args)
            store_times.append((time.perf_counter() - started_store) * 1000)
            if result.get("results"):
                memory_by_fixture[fixture_id] = result["results"][0]["memory_id"]
        query_rows: list[dict[str, Any]] = []
        for query_id, query, expected in COMPARISON_QUERIES:
            started_search = time.perf_counter()
            result = await adapter.search(
                query=query,
                intent_class="PROJECT_DESIGN_RATIONALE",
                scope="PROJECT",
                project_id="comparison-project",
                retrieval_mode_kind="SEMANTIC",
                top_k=5,
                max_memories=5,
            )
            search_times.append((time.perf_counter() - started_search) * 1000)
            returned = [
                next((fixture for fixture, memory_id in memory_by_fixture.items() if memory_id == row["memory_id"]), "UNKNOWN")
                for row in result["results"]
            ]
            ranks = [index + 1 for index, fixture in enumerate(returned) if fixture in expected]
            query_rows.append({
                "query_id": query_id,
                "expected": sorted(expected),
                "returned": returned,
                "hit_at_1": int(any(fixture in expected for fixture in returned[:1])),
                "hit_at_3": int(any(fixture in expected for fixture in returned[:3])),
                "hit_at_5": int(any(fixture in expected for fixture in returned[:5])),
                "reciprocal_rank": 1.0 / ranks[0] if ranks else 0.0,
                "false_positives": sum(fixture not in expected for fixture in returned),
                "semantic_confusion": int(bool(returned) and returned[0] not in expected),
                "arabic_english": int(query_id in {"Q-ARABIC"}),
                "technical_identifier": int(query_id in {"Q-MIXED", "Q-ID"}),
            })
        status = await adapter.status(scope="PROJECT", project_id="comparison-project")
        return {
            "profile": profile_name,
            "model": adapter.provider.profile.base_model_identity,
            "model_digest": adapter.provider.profile.model_digest,
            "dimension": adapter.provider.profile.output_dimension,
            "source_dimension": adapter.provider.profile.source_dimension,
            "document_prefix": adapter.provider.profile.document_prefix,
            "query_prefix": adapter.provider.profile.query_prefix,
            "open_ms": round(open_ms, 3),
            "store_times_ms": [round(v, 3) for v in store_times],
            "search_times_ms": [round(v, 3) for v in search_times],
            "queries": query_rows,
            "hit_at_1": round(statistics.mean(row["hit_at_1"] for row in query_rows), 6),
            "hit_at_3": round(statistics.mean(row["hit_at_3"] for row in query_rows), 6),
            "hit_at_5": round(statistics.mean(row["hit_at_5"] for row in query_rows), 6),
            "mrr": round(statistics.mean(row["reciprocal_rank"] for row in query_rows), 6),
            "false_positives": sum(row["false_positives"] for row in query_rows),
            "semantic_confusions": sum(row["semantic_confusion"] for row in query_rows),
            "arabic_english_cases": sum(row["arabic_english"] for row in query_rows),
            "technical_identifier_cases": sum(row["technical_identifier"] for row in query_rows),
            "cold_embedding_latency_ms": round(store_times[0], 3) if store_times else 0.0,
            "warm_embedding_p50_ms": round(p50(store_times[1:]), 3) if len(store_times) > 1 else 0.0,
            "search_p50_ms": round(p50(search_times), 3),
            "search_p95_ms": round(p95(search_times), 3),
            "cpu_maxrss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3),
            "db_size_bytes": file_family_size(db),
            "vector_bytes": len(COMPARISON_DOCS) * adapter.provider.profile.output_dimension * 4,
            "index_build_ms": round(open_ms, 3),
            "local_embedding_calls": adapter.provider.document_calls + adapter.provider.query_calls,
            "external_llm_calls": 0,
            "external_embedding_api_calls": 0,
            "status_counts": status["backend_status"]["counts"],
        }
    finally:
        await adapter.close()


async def mcp_wire_probe(repo: Path, db: Path, tokenizer: str, ollama_url: str, stderr_path: Path) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = os.environ.copy()
    env.update({
        "MCP_MEMORY_USE_ONNX": "0",
        "MCP_EXTERNAL_EMBEDDING_URL": "",
        "MCP_MEMORY_ALLOW_HASH_EMBEDDINGS": "0",
        "MCP_HTTP_ENABLED": "false",
        "MCP_SSE_MODE": "0",
        "MCP_STREAMABLE_HTTP_MODE": "0",
        "MCP_BACKUP_ENABLED": "false",
        "MCP_CONSOLIDATION_ENABLED": "false",
        "MCP_QUALITY_SYSTEM_ENABLED": "false",
        "MCP_QUALITY_BOOST_ENABLED": "false",
    })
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "factlane.server", "--db", str(db), "--profile", "nomic-768", "--ollama-url", ollama_url, "--tokenizer-path", tokenizer],
        env=env,
        cwd=repo,
    )
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with stderr_path.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = sorted(tool.name for tool in tools.tools)
                token_counter = TokenCounter(tokenizer)
                schema_rows = []
                for tool in tools.tools:
                    try:
                        tool_payload = tool.model_dump(mode="json")
                    except AttributeError:
                        tool_payload = {"name": tool.name, "description": getattr(tool, "description", ""), "inputSchema": getattr(tool, "inputSchema", {})}
                    schema_text = canonical_json({
                        "name": tool_payload.get("name"),
                        "description": tool_payload.get("description", ""),
                        "inputSchema": tool_payload.get("inputSchema", tool_payload.get("input_schema", {})),
                    })
                    count = token_counter.count(schema_text)
                    schema_rows.append({"name": tool.name, "tokens": count})
                schema_tokens = [row["tokens"] for row in schema_rows]
                status_result = await session.call_tool("memory_status", {"request": {"scope": "GLOBAL_USER"}})
                return {
                    "tool_names": names,
                    "tool_count": len(names),
                    "exact_five": names == sorted(MemoryAdapter.tool_names()),
                    "status_call_is_error": bool(getattr(status_result, "isError", False)),
                    "schema_token_measurement": "MEASURED" if all(isinstance(value, int) for value in schema_tokens) else "UNMEASURED",
                    "schema_token_overhead": sum(value for value in schema_tokens if isinstance(value, int)),
                    "schema_token_rows": schema_rows,
                }


def write_comparison_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "profile", "model", "model_digest", "dimension", "source_dimension", "hit_at_1", "hit_at_3", "hit_at_5", "mrr",
        "false_positives", "semantic_confusions", "arabic_english_cases", "technical_identifier_cases", "cold_embedding_latency_ms",
        "warm_embedding_p50_ms", "search_p50_ms", "search_p95_ms", "cpu_maxrss_mb", "db_size_bytes", "vector_bytes", "index_build_ms",
        "local_embedding_calls", "external_llm_calls", "external_embedding_api_calls",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)


def write_resource_csv(path: Path, rows: list[dict[str, Any]], contract: dict[str, Any]) -> None:
    fields = ["kind", "profile", "dimension", "db_size_bytes", "vector_bytes", "index_build_ms", "cpu_maxrss_mb", "write_p50_ms", "write_p95_ms", "search_p50_ms", "search_p95_ms", "local_embedding_calls", "external_llm_calls", "external_embedding_api_calls", "mcp_tool_count_exposed", "mcp_tool_schema_tokens", "result_tokens"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "kind": "embedding_profile",
                "profile": row["profile"],
                "dimension": row["dimension"],
                "db_size_bytes": row["db_size_bytes"],
                "vector_bytes": row["vector_bytes"],
                "index_build_ms": row["index_build_ms"],
                "cpu_maxrss_mb": row["cpu_maxrss_mb"],
                "write_p50_ms": p50(row["store_times_ms"]),
                "write_p95_ms": p95(row["store_times_ms"]),
                "search_p50_ms": row["search_p50_ms"],
                "search_p95_ms": row["search_p95_ms"],
                "local_embedding_calls": row["local_embedding_calls"],
                "external_llm_calls": row["external_llm_calls"],
                "external_embedding_api_calls": row["external_embedding_api_calls"],
                "mcp_tool_count_exposed": "",
                "result_tokens": "",
            })
        writer.writerow({
            "kind": "contract_pilot",
            "profile": contract["profile"],
            "dimension": 768,
            "db_size_bytes": "",
            "vector_bytes": "",
            "index_build_ms": "",
            "cpu_maxrss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3),
            "write_p50_ms": "",
            "write_p95_ms": "",
            "search_p50_ms": "",
            "search_p95_ms": "",
            "local_embedding_calls": contract.get("status_after", {}).get("audit", {}).get("local_embedding_calls", ""),
            "external_llm_calls": 0,
            "external_embedding_api_calls": 0,
            "mcp_tool_count_exposed": contract.get("mcp_wire", {}).get("tool_count", ""),
            "mcp_tool_schema_tokens": contract.get("mcp_wire", {}).get("schema_token_overhead", ""),
            "result_tokens": contract.get("token_measurement", ""),
        })


async def run(args: argparse.Namespace) -> int:
    root = Path(args.pilot_root).absolute()
    if root.exists():
        raise RuntimeError(f"pilot root already exists: {root}")
    (root / "data").mkdir(parents=True)
    evidence_dir = root / "evidence"
    evidence_dir.mkdir()
    repo = Path(__file__).resolve().parents[2]
    preflight = {
        "run_id": args.run_id,
        "pilot_root": str(root),
        "evidence_root": str(evidence_dir),
        "repo": str(repo),
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "ollama_url": args.ollama_url,
        "process_before": run_capture(["ps", "-eo", "pid,ppid,comm,args"]),
        "listeners_before": run_capture(["ss", "-ltnp"]),
        "external_llm_calls": 0,
        "external_embedding_api_calls": 0,
    }
    write_json(evidence_dir / "preflight.json", preflight)
    statuses, matryoshka = await verify_models(args.ollama_url)
    write_json(evidence_dir / "provider_verification.json", statuses)
    write_json(evidence_dir / "matryoshka.json", matryoshka)

    contract_db = root / "data" / "contract-nomic-768.sqlite"
    contract = await run_contract_pilot(contract_db, args.tokenizer, args.ollama_url)
    wire = await mcp_wire_probe(repo, contract_db, args.tokenizer, args.ollama_url, evidence_dir / "mcp_server.stderr")
    contract["mcp_wire"] = wire
    contract["checks"]["mcp_schema_token_budget"] = wire["schema_token_measurement"] == "MEASURED" and wire["schema_token_overhead"] <= 1500
    contract["token_measurement"] = "MEASURED" if contract["searches"]["semantic"]["budget"]["serialized_tokens"] != "UNMEASURED" else "UNMEASURED"
    contract["acceptance"].update({
        "MCP_TOOL_COUNT_EXPOSED": wire["tool_count"],
        "MCP_TOOL_SCHEMA_TOKEN_OVERHEAD": wire["schema_token_overhead"],
        "MCP_TOOL_SCHEMA_TOKEN_BUDGET": "PASS" if contract["checks"]["mcp_schema_token_budget"] else "HOLD",
        "READ_AFTER_WRITE": "PASS" if contract["checks"]["read_after_write"] else "HOLD",
        "LOGICAL_ID_LINEAGE": "PASS" if contract["checks"]["replacement_new_logical_id"] and contract["checks"]["replacement_old_preserved"] and contract["checks"]["reverify_same_logical_id"] else "HOLD",
        "RESULT_TOKENS": contract["token_measurement"],
    })
    write_json(evidence_dir / "contract_pilot.json", contract)
    contract_gate_failures = [
        name for name, value in {
            "contract_checks": all(value is True for value in contract["checks"].values() if isinstance(value, bool)) and contract["checks"]["cross_scope_leaks"] == 0 and contract["checks"]["stale_current_results"] == 0,
            "mcp_exact_five": wire["exact_five"],
            "mcp_status_call": not wire["status_call_is_error"],
            "mcp_schema_token_budget": contract["checks"]["mcp_schema_token_budget"],
            "unauthorized_writes": contract["checks"]["unauthorized_writes"] == 0,
            "result_tokens_measured": contract["token_measurement"] == "MEASURED",
        }.items() if not value
    ]
    if contract_gate_failures:
        raise RuntimeError("contract pilot gate failure: " + ",".join(contract_gate_failures))

    comparison_rows: list[dict[str, Any]] = []
    for profile_name in ("minilm-384", "nomic-768", "nomic-512", "nomic-256"):
        comparison_rows.append(await run_profile_comparison(root, args.tokenizer, args.ollama_url, profile_name))
    write_json(evidence_dir / "embedding_comparison.json", comparison_rows)
    write_comparison_csv(evidence_dir / "embedding_comparison.csv", comparison_rows)
    write_resource_csv(evidence_dir / "resource_profile.csv", comparison_rows, contract)

    postflight = {
        "process_after": run_capture(["ps", "-eo", "pid,ppid,comm,args"]),
        "listeners_after": run_capture(["ss", "-ltnp"]),
        "pilot_root": str(root),
        "contract_db_size_bytes": file_family_size(contract_db),
        "external_llm_calls": 0,
        "external_embedding_api_calls": 0,
    }
    write_json(evidence_dir / "postflight.json", postflight)
    print(json.dumps({
        "run_id": args.run_id,
        "pilot_root": str(root),
        "evidence_root": str(evidence_dir),
        "model_statuses": statuses,
        "matryoshka": matryoshka,
        "contract_checks": contract["checks"],
        "contract_acceptance": contract["acceptance"],
        "mcp_wire": wire,
        "comparison_profiles": [row["profile"] for row in comparison_rows],
        "result_tokens": contract["token_measurement"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S6B.4B disposable adapter pilot")
    parser.add_argument("--pilot-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tokenizer", default=TOKENIZER_DEFAULT)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"status": "HOLD", "error": type(exc).__name__, "message": str(exc)[:240]}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
