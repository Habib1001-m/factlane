# FactLane

**Share facts, not context.**

FactLane is a production-capable, local-first governed memory product for AI agents. It
shares small, validated, provenance-bearing facts without turning raw transcripts, whole
context windows, or historical memory into execution authority.

## What it does

A connected agent gets five normal memory operations:

- `memory_search` — find validated facts in one exact scope;
- `memory_get` — read one logical memory record;
- `memory_store` — admit one bounded, provenance-bearing fact;
- `memory_update` — reverify or explicitly replace a fact with revision/CAS protection;
- `memory_status` — inspect bounded backend/profile health for one scope.

FactLane keeps scope, freshness, authority, contradictions, lineage, idempotency, and
retrieval budgets in the product boundary. It is supporting memory, never execution
authority.

## Current status

The local core has passed bounded production bootstrap, restart durability, storage
integrity, real Codex/Hermes shared-store concurrency, lost-update prevention,
crash-safety, retention/capacity observability, atomic compaction of eligible superseded
state, and bounded manual housekeeping.

The repository's CI and CodeQL security checks are part of the verified project baseline.

FactLane does **not** yet claim final production-grade closure. Remaining work is final
real-host production-path acceptance, preparation and curation of the real production
corpus, and authoritative backup/restore proof. Historical retrieval evaluation on an
experimental corpus did not justify a ranking-policy change; production retrieval
validation remains deferred until a curated production corpus exists.

## Start here

See [docs/QUICKSTART.md](docs/QUICKSTART.md) for installation, host connection examples,
model/profile notes, and current limitations.

The short version is:

```bash
git clone https://github.com/Habib1001-m/factlane.git
cd factlane
uv sync --frozen
uv run factlane --help
```

Use `uv run factlane --help-tools` for the complete offline request reference. The optional
[using-factlane Agent Skill](skills/using-factlane/SKILL.md) accelerates agent use, but the
MCP schemas remain the authoritative interface and do not require the Skill.

FactLane is an MCP server over **stdio**. Configure your MCP client to launch the
project-owned `factlane` executable with a database path, profile, and host ID.

Codex and Hermes are the currently tested host integrations. The server implementation
is not hard-coded to either host: another MCP client can use the same path when it
supports local command-based stdio MCP servers. Other clients are not individually
certified yet, and HTTP/SSE MCP transport is not supported by the current FactLane
server.

## Embedding profiles

The current FactLane deployment selected `embeddinggemma-300m-768` after project-specific
fresh-blind evaluation. That is a tested deployment decision, **not a universal model
recommendation**.

Model choice depends on language mix, retrieval quality, latency, hardware, corpus/fact
shape, and operating cost. FactLane also contains supported Nomic profiles, while other
models were used as diagnostic/evaluation candidates during development. See
[docs/QUICKSTART.md](docs/QUICKSTART.md) and [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) for
the exact classification.

The current provider implementation is local Ollama over loopback HTTP only. Remote
embedding services are not a supported runtime option in this release.

## What FactLane is not

FactLane is not a raw transcript store, whole-repository memory dump, general document
search engine, or bulk folder indexer. A FactLane fact is intentionally bounded. If you
have a very large corpus, an upstream ingestion/extraction pipeline should decide what
becomes a durable fact before it reaches FactLane.

For very large ingestion workloads, embedding throughput, batching, hardware, and
provider economics may matter more than the model selected for this project's current
local deployment. Do not extrapolate the project's small controlled benchmarks into a
claim that one local model is appropriate for every scale.

## Architecture

```text
MCP host / trusted launcher
  -> HostBinding
  -> stdio-only FastMCP boundary
  -> MemoryGateway
  -> MemoryAdapter / five operations
       -> TruthRouter
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
external embedding API. The current embedding provider accepts loopback HTTP only and
has no automatic external fallback.

## Development

Requirements: Python 3.11+ and `uv`.

```bash
uv sync --frozen --dev
uv run pytest
uv run factlane --help
```

## Security

See [SECURITY.md](SECURITY.md) for reporting guidance and product security boundaries.
Architecture details are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and runtime/model
requirements are in [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md).

FactLane is licensed under Apache-2.0. Upstream projects remain owned and licensed by
their respective authors; FactLane does not vendor the pinned backend or SQLite-vec
source.
