# FactLane

**Share facts, not context.**

FactLane is a local-first, multi-host governed memory plane for AI agents. It is designed to share small, validated, provenance-bearing facts across eligible hosts without turning raw transcripts, whole context windows, or historical memory into execution authority.

## Current status

FactLane is an early public engineering project.

```text
S6B_4B=CLOSED_PASS
S6B_4C=CLOSED_PASS
```

The accepted S6B.4C milestones are transport-bound host identity and the shared
gateway (4C-02), transaction-local multi-client CAS/lost-update prevention (4C-03),
disposable Codex/Hermes shared-store concurrency (4C-04), async embedding
concurrency and pinned-backend runtime proof (4C-05), and process-crash atomicity,
durability/idempotent replay, and cancellation characterization (4C-06). The
4C-06 result is crash-safety evidence; it does not introduce or accept a crash
recovery service or subsystem.

Retention/compaction/reclaim, archive/recovery/lifecycle hygiene, and native-memory
bootstrap/migration are **not yet accepted**. The production embedding profile
remains **undecided**.

```text
TRANSPORT_BOUND_HOST_IDENTITY=ACCEPTED_4C_02
SHARED_GATEWAY=ACCEPTED_4C_02
ATOMIC_MULTI_CLIENT_CAS=ACCEPTED_4C_03
LOST_UPDATE_PREVENTION=ACCEPTED_4C_03
DISPOSABLE_CODEX_HERMES_SHARED_STORE_CONCURRENCY=ACCEPTED_4C_04
ASYNC_EMBEDDING_CONCURRENCY=ACCEPTED_4C_05
PINNED_BACKEND_RUNTIME_PROOF=ACCEPTED_4C_05
PROCESS_CRASH_ATOMICITY_DURABILITY_PROOF=ACCEPTED_4C_06
RETENTION_COMPACTION_RECLAIM=NOT_YET_ACCEPTED
ARCHIVE_RECOVERY_LIFECYCLE_HYGIENE=NOT_YET_ACCEPTED
NATIVE_MEMORY_BOOTSTRAP_MIGRATION=NOT_YET_ACCEPTED
PRODUCTION_EMBEDDING_PROFILE=UNDECIDED
```

The live project state is always `TASKBOARD.md`.

## Normal agent surface

FactLane exposes exactly five normal memory operations:

- `memory_search`
- `memory_get`
- `memory_store`
- `memory_update`
- `memory_status`

The normal agent surface does not expose delete/purge, backend administration, configuration mutation, harvesting, distillation, consolidation, or service-control operations.

## Architecture

```text
Host / trusted launcher
  -> HostBinding
  -> stdio-only FastMCP boundary
  -> MemoryGateway
  -> MemoryAdapter / five operations
       -> TruthRouter for bounded search routing decisions
       -> EmbeddingProvider
       -> SQLiteVecEngine
            -> adapter-owned transaction/CAS semantics
            -> pinned backend SQLite/SQLite-vec primitives
```

FactLane owns scope, freshness, authority, contradiction handling, logical lineage, idempotency, retrieval budgets, and its adapter schema/transactions. The pinned backend owns reusable SQLite/SQLite-vec mechanics behind an explicit compatibility boundary.

The backend is pinned to `doobidoo/mcp-memory-service` commit `e5155b937051db4fa99a384018c5ebd621d8c5ef`. Python dependency resolution is locked in `uv.lock`.

## Local-first baseline

The baseline is CPU-capable and does not require Docker, a GPU, a cloud LLM, or an external embedding API. The current embedding provider boundary targets a local Ollama endpoint and rejects non-loopback provider URLs. There is no automatic external embedding fallback.

Memory is supporting state. Current repository truth, accepted evidence, and explicit Owner authority remain higher-authority sources.

## Development

Requirements: Python 3.11+ and `uv`.

```bash
uv sync --frozen --dev
uv run pytest
uv run factlane --help
```

Live Ollama/model acceptance tests are opt-in evidence tests and are not required for every CI run.

## Governance and security

Agent entry rules are in `AGENTS.md`. Architecture and ownership boundaries are documented in `docs/ARCHITECTURE.md`; development governance in `docs/GOVERNANCE.md`; environment policy in `docs/ENVIRONMENT.md`; project provenance in `docs/PROJECT_HISTORY.md`; security reporting in `SECURITY.md`.

FactLane is licensed under Apache-2.0. Upstream projects remain owned and licensed by their respective authors; FactLane does not vendor the pinned backend or SQLite-vec source in this genesis.
