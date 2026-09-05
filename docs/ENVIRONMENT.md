# FactLane Environment Policy

FactLane is local-first and portable by default. Current machine paths are evidence,
not architecture constants.

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

Python packages are resolved by `uv.lock`. Use a project-owned virtual environment and
run project tools through that environment.

## Reuse without hidden coupling

A local cache, wheel, source checkout, model blob, or CLI can be reused when its
provenance and compatibility are verified. Reuse the asset; do not inherit another
product's runtime owner.

Caches are acquisition sources, not runtime authority. Once bootstrap resolves the
environment, normal development uses declared project dependencies and explicit
external assets rather than unbounded machine-wide discovery.

## Host integration

FactLane must not import or depend on a host application's private Python runtime,
site-packages, configuration, MCP packages, or caches as product runtime authority.
Host integration belongs at the edge; portable policy belongs in the core.

The current MCP server supports local stdio transport only. Codex and Hermes are the
currently tested host integrations. The server does not branch on those product names;
other command-based stdio MCP clients can use the same executable and a distinct
`--host-id`, but they are not individually certified by the current evidence. SSE and
streamable HTTP server transports are intentionally rejected.

## Local embedding provider

The current provider accepts loopback HTTP only. Non-local provider URLs and automatic
external fallbacks are rejected.

The selected production profile for the current FactLane deployment is:

```text
PROFILE=embeddinggemma-300m-768
PROVIDER=OLLAMA_LOCAL_LOOPBACK
MODEL=embeddinggemma:300m
MODEL_DIGEST=85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1
SOURCE_DIMENSION=768
OUTPUT_DIMENSION=768
DOCUMENT_PREFIX=title: none | text:
QUERY_PREFIX=task: search result | query:
TRUNCATE_POLICY=FAIL_CLOSED_PROVIDER_REJECTION
EFFECTIVE_CONTEXT_WINDOW=2048
```

This selection is project-specific evidence, not a recommendation that every FactLane
installation should use the same model.

### Profile and model evidence

| Model / profile | Product status | Evidence boundary |
| --- | --- | --- |
| `embeddinggemma:300m` / `embeddinggemma-300m-768` | Built-in; selected current production profile | Selected by the project's fresh-blind comparison |
| `nomic-embed-text:latest` / `nomic-768`, `nomic-512`, `nomic-256` | Built-in supported profiles | Tested baseline profiles; Nomic prefix contracts remain supported |
| `all-minilm:l6-v2` / `minilm-384` | Built-in but limited by current no-truncation contract | Short-input embedding works; observed 512-token context rejected a maximum-size 2,000-byte fact |
| `jina/jina-embeddings-v2-base-en:latest` | Evaluation only | Diagnostic retrieval candidate; not a built-in profile |
| `qwen3-embedding:0.6b` | Evaluation only | Diagnostic retrieval/storage candidate; not a built-in profile |

Model choice should be made against the user's own language mix, fact distribution,
quality target, latency, hardware, throughput, and operating-cost constraints. A model
that wins one controlled evaluation is not guaranteed to win another workload.

The exact effective context capability is runtime evidence for the approved model
artifact; FactLane does not fabricate a larger capability or silently reduce its input
contract.

## Remote embedding providers

`EmbeddingProvider` is an explicit adapter boundary, but the current product only ships
the local Ollama implementation. A managed or remote embedding provider would require a
new provider implementation, explicit configuration/security policy, tests, and new
acceptance evidence. It is therefore an extension path, not a current runtime option.

This distinction matters for high-throughput workloads: a user may rationally prefer a
managed service, GPU-backed provider, or another model when throughput and latency are
more important than the local-only constraints used by this deployment. FactLane does
not make that choice on the user's behalf.

## Large-corpus boundary

FactLane is a governed fact plane, not a crawler or arbitrary folder indexer. Facts are
bounded to 2,000 UTF-8 bytes before storage. Large source collections should be handled
by a separate ingestion/extraction layer that decides which source material becomes a
durable FactLane fact.

The project has not benchmarked terabyte-scale raw-corpus ingestion, and its controlled
small-corpus/profile measurements must not be extrapolated into a terabyte indexing-time
claim. At that scale, parsing, deduplication, batching, embedding throughput, hardware,
and provider economics become separate system-design decisions.

## Pinned backend reuse

At the exact backend pin, the backend owns reusable SQLite connection locking,
synchronous database thread offload, bounded locked/busy retry, WAL journal mode, and
`busy_timeout`. FactLane owns the higher-level transaction-local revision/CAS and
lost-update semantics. No duplicate lock/backoff layer was added.

Synchronous local provider calls are offloaded from the asyncio event loop at the
adapter boundary. No custom executor or provider worker service became a product
dependency.

## Deployment state

A bounded authoritative local bootstrap using the selected EmbeddingGemma profile has
passed exact readback, storage integrity, retrieval smoke, and restart-durability checks.
That evidence does not imply remote/cloud deployment, bulk historical-memory migration,
raw-corpus indexing, or final production-grade acceptance.
