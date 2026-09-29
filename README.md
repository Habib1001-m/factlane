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
- `memory_update` — reverify/promote a reviewed Candidate or explicitly replace a current fact with revision/CAS protection;
- `memory_status` — inspect bounded backend/profile health for one scope.

FactLane keeps scope, freshness, authority, contradictions, lineage, idempotency, and
retrieval budgets in the product boundary. It is supporting memory, never execution
authority.

## Current status

The local core has passed bounded production bootstrap, restart durability, storage
integrity, real Codex/Hermes shared-store concurrency, lost-update prevention,
crash-safety, retention/capacity observability, atomic compaction of eligible superseded
state, and bounded manual housekeeping. Controlled Codex/Hermes natural-use qualification
has also passed for the project's qualified post-R2 product identity.

The repository's CI and CodeQL security checks are part of the verified project baseline.

FactLane does **not** yet claim final production-grade closure. Real-host qualification is
accepted evidence for that qualified identity, but it does not by itself claim that this
checkout has been merged, publicly released, or deployed.
Remaining closure work includes preparation/admission of the real production corpus,
production retrieval validation on that curated corpus, authoritative backup/restore
proof, and final public package/distribution review. Historical retrieval evaluation on an
experimental corpus did not justify a ranking-policy change.

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

Use `uv run factlane --help-tools` for the complete offline request reference. The portable
[using-factlane Agent Skill](skills/using-factlane/SKILL.md) is the public behavioral
baseline for normal agent use; live MCP schemas remain the authoritative interface.
MCP-only operation is still valid for protocol/mechanical integration, but is not the
qualified natural-use baseline.

FactLane is an MCP server over **stdio**. Configure your MCP client to launch the
project-owned `factlane` executable with a database path, profile, and host ID.

Codex and Hermes are the currently tested host integrations. The server implementation
is not hard-coded to either host: another MCP client can use the same path when it
supports local command-based stdio MCP servers. Other clients are not individually
certified yet, and HTTP/SSE MCP transport is not supported by the current FactLane
server. Private Codex/Hermes enhancement plugins used during qualification are not part
of the public distribution commitment.

### Public Contract Revision 2

FactLane has five exact scopes. `CROSS_PROJECT_WORKFLOW` is for workflow doctrine that applies across projects and carries no `project_id`, `worktree_id`, `workflow_id`, or `agent_id`; those keys must be absent. Existing `WORKFLOW` remains project-bound and still requires exact `project_id` plus `workflow_id`. There is no implicit inheritance or cross-scope fanout.

Normal agent connections use the `delegated-candidate` write profile: they may contribute bounded Candidates but cannot make themselves current by claiming Owner or verification authority in a request. Current verification is supplied by a separately trusted launcher profile such as `owner-current`, `repo-verifier`, or `automated-verifier`. Candidate review uses `REVIEW_HISTORY`; promotion uses `memory_update` with `REVERIFY`, `expected_revision`, and the Candidate `expected_record_id`.

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
embedding services are not a supported runtime option in the current implementation.

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
  -> HostBinding + immutable TrustedWriteContext
  -> stdio-only FastMCP boundary
  -> MemoryGateway
  -> MemoryAdapter.dispatch / five operations
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

## Distribution boundary

The source repository and built Python wheel distribute the host-neutral FactLane runtime
and the portable `using-factlane` Skill. The wheel installs the Skill as shared data under
`share/factlane/skills/using-factlane/SKILL.md`. Host-specific Codex/Hermes plugins and
private qualification harnesses are not part of that public package.

Installing the wheel does not silently register the Skill or mutate a host configuration.
Expose the shipped Skill through the host's supported Skill discovery/installation
mechanism when using the normal natural-use baseline.

The package version remains `0.1.0`; qualification evidence does not by itself declare a
public release complete. Final publication/release review is a separate gate.

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
