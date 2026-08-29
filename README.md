# FactLane

**Share facts, not context.**

FactLane is a local-first, multi-host governed memory plane for AI agents. It is designed to share small, validated, provenance-bearing facts across eligible hosts without turning raw transcripts, whole context windows, or historical memory into execution authority.

## Current status

FactLane is an early public engineering project. The S6B.4B single-client contract pilot is accepted. Shared Codex/Hermes multi-client concurrency, transport-bound host identity, crash recovery, retention/compaction, and the production embedding profile are **not yet accepted**.

The live project state is always `TASKBOARD.md`.

## Normal agent surface

FactLane exposes exactly five normal memory operations:

- `memory_search`
- `memory_get`
- `memory_store`
- `memory_update`
- `memory_status`

The normal agent surface does not expose delete/purge, backend administration, configuration mutation, harvesting, distillation, consolidation, or service-control operations.

## Architecture

```text
Host edge
  -> FactLane Router
  -> five-operation narrow adapter
  -> backend compatibility boundary
  -> mcp-memory-service / SQLite-vec
  -> local embedding provider
```

FactLane owns scope, freshness, authority, contradiction handling, logical lineage, idempotency, retrieval budgets, and its adapter schema/transactions. The pinned backend owns reusable SQLite/SQLite-vec mechanics behind an explicit compatibility boundary.

The backend is pinned to `doobidoo/mcp-memory-service` commit `e5155b937051db4fa99a384018c5ebd621d8c5ef`. Python dependency resolution is locked in `uv.lock`.

## Local-first baseline

The baseline is CPU-capable and does not require Docker, a GPU, a cloud LLM, or an external embedding API. The current embedding provider boundary targets a local Ollama endpoint and rejects non-loopback provider URLs. There is no automatic external embedding fallback.

Memory is supporting state. Current repository truth, accepted evidence, and explicit Owner authority remain higher-authority sources.

## Development

Requirements: Python 3.11+ and `uv`.

```bash
uv sync --frozen --dev
uv run pytest
uv run factlane --help
```

Live Ollama/model acceptance tests are opt-in evidence tests and are not required for every CI run.

## Governance and security

Agent entry rules are in `AGENTS.md`. Architecture and ownership boundaries are documented in `docs/ARCHITECTURE.md`; development governance in `docs/GOVERNANCE.md`; environment policy in `docs/ENVIRONMENT.md`; project provenance in `docs/PROJECT_HISTORY.md`; security reporting in `SECURITY.md`.

FactLane is licensed under Apache-2.0. Upstream projects remain owned and licensed by their respective authors; FactLane does not vendor the pinned backend or SQLite-vec source in this genesis.
