# FactLane Quick Start

This guide is for someone who wants to run FactLane and connect an MCP-capable agent
without needing to understand the internal campaign history.

## 1. Install the project

Requirements:

- Python 3.11 or newer;
- `uv`;
- a local Ollama runtime for the currently supported embedding-provider path.

```bash
git clone https://github.com/Habib1001-m/factlane.git
cd factlane
uv sync --frozen
uv run factlane --help
```

FactLane does not automatically download models. The model required by the selected
profile must already exist in the local Ollama runtime and match the exact identity
pinned by that profile. Provider identity or dimension mismatches fail closed.

If you want to reproduce the **current tested FactLane deployment** rather than choose a
profile for your own workload, install the model used by that deployment:

```bash
ollama pull embeddinggemma:300m
```

and use:

```text
PROFILE=embeddinggemma-300m-768
MODEL=embeddinggemma:300m
```

This is a reproducibility path for the deployment tested by this project, not a claim
that EmbeddingGemma is the best model for every user or workload.

If you choose another built-in profile, install the model required by that profile
instead. The profile table below shows what this project actually tested.

## 2. Choose a database and embedding profile

A normal launch needs:

```text
--db <path-to-sqlite-database>
--profile <profile-id>
--host-id <stable-non-secret-host-label>
```

Example server command shape:

```bash
/path/to/factlane/.venv/bin/factlane \
  --db /path/to/state/factlane.sqlite3 \
  --profile <profile-id> \
  --host-id <host-id>
```

The MCP client should normally launch this process for you over stdio. Running the
server command directly is not an interactive application; it waits for an MCP client on
stdin/stdout.

### Models and profiles we evaluated

The following names describe this project's evidence. They are not a universal ranking
for every workload.

| Model / profile | Classification in FactLane | Current meaning |
| --- | --- | --- |
| `embeddinggemma:300m` / `embeddinggemma-300m-768` | Built-in, tested, selected production profile for the current FactLane deployment | Selected after the project's fresh-blind comparison; not a universal recommendation |
| `nomic-embed-text:latest` / `nomic-768`, `nomic-512`, `nomic-256` | Built-in, tested supported profiles | Important baseline and supported alternatives; not the selected production profile |
| `all-minilm:l6-v2` / `minilm-384` | Built-in, partially compatible profile | Identity and short-input embedding were verified, but the observed 512-token context rejected a maximum-size 2,000-byte FactLane fact under the no-truncation contract |
| `jina/jina-embeddings-v2-base-en:latest` | Evaluation-only diagnostic candidate | Used in comparative retrieval evidence; not a built-in product profile |
| `qwen3-embedding:0.6b` | Evaluation-only diagnostic candidate | Used in comparative retrieval/storage evidence; not a built-in product profile |

Choose a model/profile for **your** language mix, fact shape, latency target, hardware,
quality target, and operating cost. A result from this project's data is not a guarantee
that another user's data will produce the same ranking.

The current release does not accept a remote embedding endpoint. The provider interface
is an explicit product boundary, so a remote/provider-specific implementation can be
added in future work, but that is not a supported runtime feature today.

## 3. Connect Codex

Codex is one of the tested FactLane hosts. Configure a stdio MCP server in Codex and use
a stable host ID such as `codex`.

Example `~/.codex/config.toml` entry reproducing the current tested profile:

```toml
[mcp_servers.factlane]
command = "/absolute/path/to/factlane/.venv/bin/factlane"
args = [
  "--db", "/absolute/path/to/state/factlane.sqlite3",
  "--profile", "embeddinggemma-300m-768",
  "--host-id", "codex"
]
enabled = true
```

Restart/reload Codex's MCP configuration using the mechanism provided by your installed
Codex version, then verify that the five FactLane memory tools are visible.

## 4. Connect Hermes

Hermes is the other currently tested FactLane host. Add a command-based stdio MCP server
to `~/.hermes/config.yaml` and use a stable host ID such as `hermes`.

```yaml
mcp_servers:
  factlane:
    command: "/absolute/path/to/factlane/.venv/bin/factlane"
    args:
      - "--db"
      - "/absolute/path/to/state/factlane.sqlite3"
      - "--profile"
      - "embeddinggemma-300m-768"
      - "--host-id"
      - "hermes"
```

Start/reload Hermes so it discovers the MCP server and its tools.

## 5. Connect another MCP client

FactLane's server code does not contain Codex- or Hermes-specific request logic. The
current transport boundary is standard MCP over local stdio.

A different MCP client can use the same executable when it supports command-based stdio
MCP servers. Configure its equivalent of:

```text
command = /absolute/path/to/factlane/.venv/bin/factlane
args = --db ... --profile ... --host-id <unique-stable-host-label>
```

Compatibility outside Codex and Hermes is protocol-level, not an individual certification
claim. The current FactLane server deliberately rejects SSE and streamable HTTP MCP
transport.

## 6. Verify the connection before storing anything

First verify that the client discovers exactly these normal tools:

```text
memory_search
memory_get
memory_store
memory_update
memory_status
```

Then call `memory_status` with an exact scope. For example, a project-scoped check uses:

```json
{
  "scope": "PROJECT",
  "project_id": "example-project"
}
```

Do not start by importing a large body of context. FactLane is designed to admit small,
reviewable facts with explicit scope, provenance, freshness, and authority semantics.

## 7. Large datasets and very large folders

FactLane is not a general-purpose crawler or raw corpus indexer. A stored fact is bounded
to 2,000 UTF-8 bytes, and the normal product surface is designed around governed facts,
not direct ingestion of arbitrary directories.

If you have hundreds of gigabytes or terabytes of source material, use a separate
upstream ingestion/extraction system to parse, deduplicate, classify, and decide which
items should become durable facts. That upstream system may need a very different
embedding strategy from this project's local deployment: higher-throughput hardware,
batching, a different model, or a managed embedding service may be more appropriate.

FactLane currently does **not** provide that remote-provider path itself, and this project
has not benchmarked a 1 TB ingestion workload. Do not estimate a 1 TB indexing time from
our controlled small-corpus tests.

## Current limitations

- MCP transport is stdio only; no SSE or streamable HTTP server path is supported.
- Codex and Hermes are tested hosts; other stdio MCP clients are not individually
  certified yet.
- The current embedding runtime is local Ollama over loopback HTTP only.
- Remote embedding-provider support is not implemented in the current release.
- FactLane is a governed fact plane, not a raw transcript, repository dump, or bulk
  document-indexing product.
- Retrieval specificity under Arabic/mixed-language and document-crowding cases remains
  an open quality debt before final production-grade closure.
- Final real-host production-path acceptance and authoritative backup/restore acceptance
  are still open closure items.
