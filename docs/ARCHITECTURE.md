# FactLane Architecture

## Purpose

FactLane is a governed fact-sharing plane for AI agents. It separates small durable
facts from knowledge corpora, session history, and current project truth.

```text
MEMORY != KNOWLEDGE != SESSION_HISTORY != PROJECT_TRUTH
```

The core principle is **share facts, not context**.

## Core flow

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

The normal operations are `memory_search`, `memory_get`, `memory_store`,
`memory_update`, and `memory_status`.

`TruthRouter` makes bounded memory-routing decisions inside the adapter; it is not the
transport gateway.

## Host identity and gateway

`HostBinding` is an immutable binding supplied by the trusted launcher. The runtime
supports the `stdio` transport only, and an unbound gateway fails closed. Reserved
transport-identity claims in request payloads are rejected. The gateway projects its
bound host identity into the audit envelope; launcher binding is separate from an
arbitrary request `agent_id`. This boundary is not cryptographic or operating-system
identity attestation.

## Ownership boundary

FactLane owns:

- exact scope, freshness, and authority policy;
- contradiction visibility and fail-closed behavior;
- logical memory IDs, record revisions, lineage, supersession, and idempotency;
- bounded result/tool envelopes;
- embedding profile identity and projection metadata;
- adapter-owned schema and transaction semantics;
- the public five-operation surface.

The exact pinned backend is reused for SQLite-vec extension/schema primitives,
connection locking, synchronous database thread offload, bounded locked/busy retry,
WAL initialization, and `busy_timeout`. FactLane does not duplicate those mechanics.

## Revision and transaction semantics

FactLane uses transaction-local parent-current compare-and-swap. A stale independent
writer receives deterministic `VERSION_CONFLICT`; successor insertion, vector write,
and parent supersession occur in one transaction.

## Embedding boundary

The provider receives raw fact/query text. Nomic profiles apply exactly one
`search_document: ` prefix for stored documents and exactly one `search_query: ` prefix
for queries. Runtime embedding requests use `truncate=false`; provider/model overflow
fails closed rather than silently truncating.

Potentially blocking provider calls are offloaded from the asyncio event loop at the
adapter boundary using the standard library thread-offload mechanism. No custom worker
service or executor is a product dependency.

## Crash safety

Pre-commit process interruption leaves no partial adapter/native/vector rows. If a
process ends after commit but before its response, durable state remains resolvable
through idempotent replay. These guarantees apply at the tested transaction boundaries
and do not imply a separate recovery service.
