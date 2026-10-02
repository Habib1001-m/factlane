# FactLane

**Share facts, not context.**

FactLane is a local-first memory service for agents that communicate over the Model Context
Protocol (MCP). It stores small facts with source provenance, scope, and freshness
information. Agents can reuse those facts without treating a past conversation, an
unverified contribution, or the memory store itself as authority over the current task.

An agent can propose a fact. A separately trusted verifier decides whether it becomes
*current*. Searches for current facts exclude unverified Candidates, even when a Candidate
is a closer semantic match.

## Get started

You need Python **3.11+**, a Python-linked SQLite runtime **3.42.0+**,
[`uv`](https://docs.astral.sh/uv/), and a local [Ollama](https://ollama.com/) instance with
the model for your chosen embedding profile. FactLane is CPU-capable; Docker, a GPU, and
external embedding APIs are not required.

For a source checkout with the `embeddinggemma-300m-768` profile:

```bash
git clone https://github.com/Habib1001-m/factlane.git
cd factlane
uv sync --frozen
uv run python -c 'import sqlite3; print(sqlite3.sqlite_version)'
ollama pull embeddinggemma:300m
uv run factlane --help
uv run factlane --help-tools
```

The SQLite check runs inside the project environment that launches FactLane. Python 3.11+ by
itself does not guarantee a compatible linked SQLite runtime.

In a **local, command-based stdio MCP client**, configure FactLane as a server process with
arguments equivalent to:

```bash
uv run factlane --db ./factlane.sqlite3 --profile embeddinggemma-300m-768 --host-id local-dev --write-profile delegated-candidate
```

Use a stable, non-secret host ID. The example allows the agent to contribute **Candidates**,
not to validate its own facts. Omit `--write-profile` for the fail-closed, read-only
default. A trusted operator must configure verifier authority separately. The database and
model remain local; the current provider connects only to Ollama on loopback.

The [quick start](docs/QUICKSTART.md) has client configuration examples, alternative
profiles, and troubleshooting. The CLI's `--help-tools` output is the offline reference for
request fields and supported values.

## What agents can do

FactLane exposes **five MCP tools**:

| Tool | Purpose |
| --- | --- |
| `memory_search` | Search facts within one exact scope; choose current facts or review history. |
| `memory_get` | Retrieve a logical memory record. |
| `memory_store` | Contribute a bounded fact with source and freshness metadata. |
| `memory_update` | Reverify a Candidate or replace a current revision under compare-and-swap protection. |
| `memory_status` | Inspect storage and embedding-profile health for a scope. |

The public contract has five scopes: `GLOBAL_USER`, `PROJECT`, `WORKFLOW`,
`TOOL_ENVIRONMENT`, and `CROSS_PROJECT_WORKFLOW`. Project and workflow identities can be
supplied by a trusted host binding; conflicts with caller-supplied bound IDs are rejected.
`CROSS_PROJECT_WORKFLOW` is deliberately identity-free: requests must omit all project,
worktree, workflow, and agent ID keys. Records never fan out implicitly to other scopes.

The normal write profile, `delegated-candidate`, cannot promote its own contribution by
supplying a privileged claim. Trusted promotion uses `memory_update` with `REVERIFY`,
`expected_revision`, and the Candidate's `expected_record_id`. `REVERIFY` preserves the
logical contradiction identity: it may refresh verification metadata but cannot reclassify
the memory type or subject; use `REPLACE` for a semantic identity change. `CURRENT` retrieval only
admits eligible, validated, fresh facts; review of historical records is explicit.

For normal agent use, the distribution includes the portable
[using-factlane Skill](skills/using-factlane/SKILL.md). It explains when memory is useful
and when the agent should rely on the current task instead. Live MCP schemas remain
authoritative. Installing the Python package does **not** install or activate the Skill
inside a host automatically.

## Architecture and operating limits

The stdio gateway enforces host identity and scope policy. The adapter owns validation,
authority, revision/transaction semantics, and retrieval budgets. A pinned upstream backend
supplies SQLite/SQLite-vec storage mechanics. Current semantic search filters for eligible
records *before* the KNN limit, rather than letting closer Candidates displace validated
facts.

FactLane checks the **linked SQLite version at startup** and refuses versions below 3.42.0
before opening or creating the database. This also establishes the minimum runtime for the
separate, trusted-operator sensitive-memory recovery path; the operator still probes the
required FTS5 capability before mutation. Recovery and deletion are **not public MCP
tools**.

The server currently supports **stdio**, not HTTP or SSE. The shipped embedding provider
uses local Ollama only. FactLane is a fact store, not a raw transcript archive, general
document search engine, or bulk file crawler; a separate ingestion layer should decide what
becomes a durable fact.

This repository is still being qualified for broader production use. Passing local
regression tests and controlled host checks does not establish authoritative backup/restore
acceptance, production-corpus retrieval quality, or suitability for every language and
workload. In particular, Arabic/mixed-language retrieval and document crowding are known
quality constraints. No public package release or production deployment is implied by the
source checkout.

See [Architecture](docs/ARCHITECTURE.md),
[Environment and supported profiles](docs/ENVIRONMENT.md), and [Security](SECURITY.md) for
the precise contracts and limitations.

## Develop

```bash
uv sync --frozen --dev
uv run pytest -q
```

The wheel and source distribution include the `using-factlane` Skill alongside the Python
runtime. FactLane is licensed under [Apache-2.0](LICENSE); the pinned upstream backend and
SQLite-vec retain their own licenses.
