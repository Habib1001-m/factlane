# FactLane Architecture

## Purpose

FactLane is a governed fact-sharing plane for AI agents. It deliberately separates small durable facts from knowledge corpora, session history, and current project truth.

```text
MEMORY != KNOWLEDGE != SESSION_HISTORY != PROJECT_TRUTH
```

The core principle is **share facts, not context**.

## Core flow

```text
Host edge
  -> TruthRouter / FactLane Router decision
  -> five-operation MemoryAdapter
  -> SQLiteVecEngine compatibility boundary
  -> pinned mcp-memory-service / SQLite-vec
  -> local EmbeddingProvider
```

The normal operations are `memory_search`, `memory_get`, `memory_store`, `memory_update`, and `memory_status`.

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

The adapter already exposes `expected_revision` and rejects a stale sequential revision. That API is **not** proof of atomic multi-client compare-and-swap behavior. S6B.4C must test and, where needed, implement single-winner concurrent revision semantics and lost-update prevention across independent clients/processes.

## Embedding boundary

The provider receives raw fact/query text. Nomic profiles apply exactly one `search_document: ` prefix for stored documents and exactly one `search_query: ` prefix for queries. Runtime embedding requests use `truncate=false`; provider/model overflow must fail closed rather than silently truncate.

Embedding profile changes create a new projection/index boundary. Canonical memory records are separate from vector projections.

## Later ownership

S6B.4C owns transport-bound host identity, shared gateway/write-plane behavior, multi-client revision/CAS, lost-update prevention, async embedding concurrency, and crash-injection proof.

S6B.4D owns retention, compaction/reclaim, archive/recovery, and lifecycle hygiene. SQLite-vec delete/reclaim behavior is an explicit input to that phase.
