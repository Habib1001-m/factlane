# FactLane

**Share facts, not context.**

FactLane is a local-first, multi-host governed memory plane for AI agents. It shares
small, validated, provenance-bearing facts without turning raw transcripts, whole
context windows, or historical memory into execution authority.

## Current status

FactLane is an early public engineering project. The implementation provides
transport-bound host identity, a narrow shared gateway, transaction-local
lost-update prevention, asynchronous local-provider offload, and crash-safe
transaction boundaries.

Retention, compaction, reclaim, archive/recovery, lifecycle hygiene, and native-memory
bootstrap/migration remain future work. The production embedding profile remains
undecided.

## Normal agent surface

FactLane exposes exactly five normal memory operations:

- `memory_search`
- `memory_get`
- `memory_store`
- `memory_update`
- `memory_status`

The normal agent surface does not expose delete/purge, backend administration,
configuration mutation, harvesting, distillation, consolidation, or service-control
operations.

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

FactLane owns scope, freshness, authority, contradiction handling, logical lineage,
idempotency, retrieval budgets, and adapter transactions. The pinned backend supplies
reusable SQLite/SQLite-vec mechanics behind an explicit compatibility boundary.

## Local-first baseline

The baseline is CPU-capable and does not require Docker, a GPU, a cloud LLM, or an
external embedding API. The embedding provider accepts loopback HTTP only and has no
automatic external fallback.

## Development

Requirements: Python 3.11+ and `uv`.

```bash
uv sync --frozen --dev
uv run pytest
uv run factlane --help
```

## Security

See [SECURITY.md](SECURITY.md) for reporting guidance and product security boundaries.
Architecture details are documented in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
and runtime requirements in [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md).

FactLane is licensed under Apache-2.0. Upstream projects remain owned and licensed by
their respective authors; FactLane does not vendor the pinned backend or SQLite-vec
source.
