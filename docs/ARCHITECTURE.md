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
MCP host / trusted launcher
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
transport gateway. The search path supports exact, keyword, semantic, and hybrid
retrieval modes behind the same scope/freshness/authority filtering boundary.

## Host identity and MCP compatibility

`HostBinding` is an immutable binding supplied by the trusted launcher. The runtime
supports the `stdio` transport only, and an unbound gateway fails closed. Reserved
transport-identity claims in request payloads are rejected. The gateway projects its
bound host identity into the audit envelope; launcher binding is separate from an
arbitrary request `agent_id`. This boundary is not cryptographic or operating-system
identity attestation.

The server implementation does not contain Codex- or Hermes-specific dispatch logic.
Codex and Hermes are the currently tested host integrations because they were the first
product targets. Another MCP client can use the same FactLane executable when it can
launch a local command-based stdio MCP server and supply a stable `--host-id`. That is a
protocol compatibility statement, not a claim that every MCP client has been certified.

SSE and streamable HTTP MCP server transports are intentionally rejected by the current
runtime.

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

The adapter depends on an `EmbeddingProvider` contract, while the current shipped
provider implementation is local Ollama over loopback HTTP. Runtime model identity,
capability, native dimension, and input-size expectations fail closed when they do not
match the selected profile.

The selected production profile for the current FactLane deployment is:

```text
PROFILE=embeddinggemma-300m-768
MODEL=embeddinggemma:300m
MODEL_DIGEST=85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1
SOURCE_DIMENSION=768
OUTPUT_DIMENSION=768
DOCUMENT_PREFIX=title: none | text:
QUERY_PREFIX=task: search result | query:
TRUNCATE=false
CONTEXT_WINDOW=2048
```

Nomic profiles remain supported product profiles with their documented
`search_document: ` and `search_query: ` prefixes, but they are not the selected
production profile. Other models used during evaluation are evidence, not silently
promoted runtime profiles.

A remote or managed embedding provider is architecturally possible behind the provider
boundary, but the current product does not ship or accept one. Adding it requires an
explicit implementation and acceptance path rather than pointing the existing local
provider at a non-loopback URL.

Potentially blocking provider calls are offloaded from the asyncio event loop at the
adapter boundary using the standard library thread-offload mechanism. No custom worker
service or executor is a product dependency.

## Fact plane versus corpus indexing

FactLane stores bounded facts; it is not a raw document crawler or bulk directory
indexer. Source collections may be much larger than FactLane's durable fact set, but an
upstream ingestion/extraction layer is responsible for deciding which source material
becomes a fact and for carrying the required provenance into that admission.

This separation is intentional. A large-corpus ingestion system may need different
batching, hardware, embedding models, or managed-provider economics without changing the
governed FactLane storage/authority contract.

## Retention and housekeeping

FactLane exposes read-only retention/capacity observations and a bounded manual
housekeeping path for eligible superseded state. Housekeeping reuses the accepted atomic
compaction boundary, preserves current authority and logical history, fails closed on
incomplete capacity/health observations, and does not introduce a background daemon,
scheduler, or `VACUUM` requirement.

This lifecycle support is distinct from disaster recovery. An authoritative
backup-to-disposable-restore acceptance proof is not yet part of the accepted public
product claim.

## Crash safety

Pre-commit process interruption leaves no partial adapter/native/vector rows. If a
process ends after commit but before its response, durable state remains resolvable
through idempotent replay. These guarantees apply at the tested transaction boundaries
and do not imply a separate recovery service.

## Current quality boundary

The selected profile has strong bounded retrieval evidence overall, while retrieval
specificity under Arabic/mixed-language and document-crowding cases remains an open
quality debt. That debt does not reopen the accepted storage, transaction, host-identity,
or embedding-profile architecture by default.
