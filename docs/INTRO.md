---
slug: /
---

# FactLane documentation

FactLane is a local-first MCP memory layer for **bounded reusable facts**. It helps compatible AI-agent hosts carry useful preferences, project facts, workflow rules, and other durable context across sessions without allowing remembered information to silently become execution authority or unquestioned current truth.

The product centers on four boundaries:

- **Candidate is not Current** — a contribution may be worth retaining before it is trusted.
- **Current is still subordinate to reality** — current instructions, live project/product state, and verified live sources can outrank memory.
- **Scope is explicit** — facts stay inside the exact user, project, workflow, tool-environment, or governed cross-project boundary selected for them.
- **Authority is separate from visibility** — seeing a tool or remembering a policy does not grant write, verifier, or Owner permissions.

The current product also includes a trusted local **Owner control plane** for inventory, history, conflicts, lifecycle actions, manual freshness recheck, readmission, health, and retention inspection. Those Owner surfaces remain separate from the public five-tool stdio MCP contract.

## New to FactLane?

Use this reading path:

1. [Everyday use cases](USE_CASES.md) — decide whether governed facts solve a real problem for you.
2. [FAQ](FAQ.md) — check fit, non-goals, local-data behavior, and setup expectations.
3. [Core concepts](CORE_CONCEPTS.md) — understand Candidate, Current, scope, freshness, provenance, and authority.
4. [Quick Start](QUICKSTART.md) — install a supported release and establish a read-only connection.
5. [Five MCP tools](TOOLS.md) — use the public MCP contract once connectivity is proven.

### Do I need to be a developer?

Not for everyday memory use after FactLane is connected, but setup is still technical. Someone needs to install the runtime, verify Python/SQLite/Ollama prerequisites, configure a compatible command-launched stdio MCP host, and choose the launcher authority profile.

If that is not practical in your environment, FactLane is not currently a zero-configuration memory feature.

## Building, integrating, or operating?

Use the deeper references directly:

- [Architecture](ARCHITECTURE.md) — request path, lifecycle, storage, Owner control, migration, and Recovery.
- [Environment and compatibility](ENVIRONMENT.md) — supported runtime, embedding, host, storage, and platform boundaries.
- [Security](../SECURITY.md) — trust boundaries, consent, write authority, incident Recovery, and rollback safety.
- [Release operations](RELEASE_OPERATIONS.md) — authoritative release identity, artifacts, installation, migration, and rollback.
- [Changelog](CHANGELOG.md) — user-facing changes between releases.
- [Project history](PROJECT_HISTORY.md) — why the public contract and product shape evolved.

The live MCP schema and `factlane --help-tools` are authoritative for exact public request fields and enums. Packaged operator CLIs remain authoritative for their own current flags and arguments.

## Release status without coupling product docs to a version

Evergreen product and installation documentation intentionally does not embed a specific FactLane release number. For the current published release, exact tag/tree, hashes, and release-status truth, use [Release operations](RELEASE_OPERATIONS.md). For what changed, use the [Changelog](CHANGELOG.md).
