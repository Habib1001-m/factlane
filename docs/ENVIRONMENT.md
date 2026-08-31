# FactLane Environment Policy

FactLane is local-first and portable by default. Current machine paths are evidence, not architecture constants.

## Baseline

```text
Python >= 3.11
CPU_ONLY_BASELINE=YES
GPU_REQUIRED=NO
DOCKER_REQUIRED=NO
EXTERNAL_LLM_REQUIRED=NO
EXTERNAL_EMBEDDING_API_REQUIRED=NO
PERSISTENT_SERVICE_REQUIRED=NO
```

Python packages are resolved by `uv.lock`. Use a project-owned virtual environment and run project tools through that environment.

## Reuse without hidden coupling

A local cache, wheel, source checkout, model blob, or CLI can be reused when its provenance and compatibility are verified. Reuse the asset; do not inherit another product's runtime owner.

Caches are acquisition sources, not runtime authority. Once bootstrap resolves the environment, normal development uses declared project dependencies and explicit external assets rather than unbounded machine-wide discovery.

## Hermes boundary

Hermes may act as environment operator, but FactLane must not import or depend on Hermes Python, Hermes site-packages, Hermes configuration, Hermes MCP packages, or Hermes-owned caches as product runtime authority. The same rule applies to any other host-specific runtime.

Host integration belongs at the edge; portable policy belongs in the core.

## Local embedding provider

The baseline provider accepts loopback HTTP only. Non-local provider URLs and automatic external fallbacks are rejected.

For Nomic profiles:

```text
DOCUMENT_PREFIX=search_document:
QUERY_PREFIX=search_query:
TRUNCATE_POLICY=FAIL_CLOSED_PROVIDER_REJECTION
```

The exact effective context capability is runtime evidence for the approved model artifact; FactLane does not fabricate an 8192-token capability or silently reduce its input contract.

## Pinned backend reuse

At the accepted backend pin, the pinned backend continues to own reusable SQLite
connection locking, synchronous database thread offload, bounded `locked`/`busy`
retry, WAL journal mode, and `busy_timeout`. FactLane owns the higher-level
transaction-local revision/CAS and lost-update semantics accepted in 4C-03. No
duplicate lock/backoff layer was added.

Synchronous local provider calls are offloaded from the asyncio event loop at the
adapter boundary. The accepted 4C-05 proof observed concurrent provider work while
preserving the pinned backend/runtime contract; no custom executor or provider
worker service became a product dependency.
