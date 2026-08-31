# FactLane Architecture

## Purpose

FactLane is a governed fact-sharing plane for AI agents. It deliberately separates small durable facts from knowledge corpora, session history, and current project truth.

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

The normal operations are `memory_search`, `memory_get`, `memory_store`, `memory_update`, and `memory_status`.

`TruthRouter` makes bounded memory-routing decisions inside the adapter; it is not
the transport gateway.

## HostBinding and MemoryGateway

`HostBinding` is an immutable binding supplied by the trusted launcher. The current
runtime supports the `stdio` transport only, and an unbound gateway fails closed.
Reserved transport-identity claims in request payloads are rejected. The gateway
projects its own bound host identity into the audit envelope; launcher binding is
separate from an arbitrary request `agent_id`. This boundary does not claim
cryptographic or operating-system identity attestation. Where required, real-host
provenance remains external accepted launcher evidence.

## Ownership boundary

FactLane owns:

- exact scope, freshness, and authority policy;
- contradiction visibility and fail-closed behavior;
- logical memory IDs, record revisions, lineage, supersession, and idempotency;
- bounded result/tool envelopes;
- embedding profile identity and projection metadata;
- adapter-owned schema and FactLane transaction semantics;
- the public five-operation surface.

The exact pinned backend is reused for:

- SQLite-vec extension/schema primitives;
- connection lock and synchronous DB thread offload;
- bounded retry/backoff for SQLite `locked`/`busy` conditions;
- WAL initialization and `busy_timeout`;
- lower-level SQLite/SQLite-vec mechanics that do not conflict with the FactLane contract.

FactLane does not reimplement those lock/retry mechanics. The compatibility boundary intentionally depends on the pinned backend's `_execute_with_retry` primitive; a regression test makes that private-hook dependency explicit so a future backend upgrade cannot silently break it.

## Revision/CAS boundary

FactLane uses transaction-local parent-current CAS. A stale independent writer
receives deterministic `VERSION_CONFLICT`; successor insertion, vector write, and
parent supersession occur in one transaction. SQLite lock/busy/WAL mechanics remain
delegated to the pinned backend, and no duplicate coordinator or backoff subsystem
was added.

## Embedding boundary

The provider receives raw fact/query text. Nomic profiles apply exactly one `search_document: ` prefix for stored documents and exactly one `search_query: ` prefix for queries. Runtime embedding requests use `truncate=false`; provider/model overflow must fail closed rather than silently truncate.

Embedding profile changes create a new projection/index boundary. Canonical memory records are separate from vector projections.

The provider API remains synchronous, but potentially blocking provider calls are
offloaded from the asyncio event loop at the adapter boundary using the standard
library thread-offload mechanism. No custom worker service, executor, or thread
abort mechanism is a product dependency.

## Crash boundary

The accepted 4C-06 proof showed that pre-commit process death leaves no partial
adapter/native/vector rows, while post-commit/pre-response death leaves durable
state resolvable through idempotent replay. The proof also found no stale writer
locks or lineage forks. This is crash-safety evidence at tested boundaries, not a
crash-recovery service or subsystem.

## Later ownership

S6B.4C owns and has accepted transport-bound host identity, shared gateway/write-plane
behavior, multi-client revision/CAS, lost-update prevention, async embedding
concurrency, and crash-injection proof.

S6B.4D remains the later owner of retention, compaction/reclaim, archive/recovery,
lifecycle hygiene, and vec0 delete/reclaim behavior investigation. S6B.5 remains
the native-memory/bootstrap/migration phase. Production embedding-profile selection
is unresolved.
