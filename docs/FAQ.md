# Frequently asked questions

Use this page to decide whether FactLane fits before investing in setup.

## Does FactLane replace my assistant?

No. FactLane is a local memory layer behind a **compatible AI-agent host**. You keep using your assistant or coding agent; the host connects it to FactLane over MCP.

## Does FactLane replace built-in memory or a knowledge base?

No. Broad/native memory is useful for rich recall and continuity. Knowledge bases and graphs are useful for larger bodies of connected information.

FactLane is narrower: it governs **small reusable facts** that need exact scope, provenance, freshness, Candidate → Current verification, and explicit authority boundaries. It is designed to coexist with broader memory systems.

## Do I need to be a developer?

Not for normal use after FactLane is connected, but setup is technical. Someone must install FactLane, verify its local runtime prerequisites, configure a compatible command-launched stdio MCP host, and choose the launcher authority profile.

An agent can guide that work only within the machine and host permissions it actually has. It should never claim installation or configuration that it could not verify.

## What is FactLane a good fit for?

Examples include:

- durable user preferences;
- important project facts;
- recurring workflow rules;
- scoped tool/environment facts;
- deliberately governed cross-project workflow rules.

It is especially useful when those facts should survive a session **without** allowing old memory to silently overrule current instructions or verified live state.

## What is it not trying to be?

FactLane is not:

- a transcript archive;
- a bulk file/document crawler;
- a general search index for arbitrary corpora;
- a backup service;
- a managed remote memory cloud;
- a public HTTP/SSE/Streamable HTTP MCP server;
- an automatic permission system for an agent;
- a general agent-safety framework.

Facts are bounded to **2,000 UTF-8 bytes**.

## Where does the data live?

The documented profile uses local SQLite/SQLite-vec storage and supported local Ollama embeddings. The shipped provider does not silently fall back to a remote embedding endpoint.

Durable storage has an explicit contract. If an existing database needs migration before the installed runtime can use it, normal startup fails closed and the operator must follow the documented migration and verified backup/restore procedure.

See [Environment and compatibility](ENVIRONMENT.md) and [Release operations](RELEASE_OPERATIONS.md).

## Can an ordinary agent verify its own memory?

No. A delegated agent may be allowed to contribute a **Candidate**, but Candidate promotion is a separate trusted verification operation. Seeing `memory_update` does not grant verifier or Owner authority.

Direct Owner authority may additionally revoke eligible Current records or reject Candidates after reviewing the exact current record/group state. Those lifecycle dispositions are logical states, not physical erasure.

## What is the Owner control plane?

`OwnerControlService` is the trusted local governance surface for inventory, record detail, conflicts, history, health/retention, lifecycle actions, manual freshness recheck, and readmission.

It is exposed through:

- the packaged `factlane-owner` CLI; and
- the packaged `factlane-owner-webui` loopback launcher.

The WebUI binds to `127.0.0.1`, uses a one-use bootstrap exchange plus an authenticated session and CSRF protection, and does **not** turn FactLane into an HTTP MCP server.

## Does FactLane watch my sources in the background?

No. An `on_change` freshness policy stores a recheck contract. A trusted Owner performs an explicit manual recheck with fresh observation evidence.

Unchanged healthy evidence may renew eligibility. Changed, unavailable, indeterminate, stale, or mismatched evidence fails closed for review.

## What wins if memory conflicts with what I say now?

Current authoritative context wins. A current user instruction, current repository/product state, or verified live source can outrank remembered information.

Memory supports the decision; it does not own the decision.

## Can two assistants use the same facts?

Yes, when the operator connects compatible local hosts to the same governed store.

That does not give the hosts identical authority. Launcher profiles, bound identity, current instructions, and runtime policy still govern what each connection can do.

## How do I find the current supported release?

Use [Release operations](RELEASE_OPERATIONS.md) for the authoritative published tag, commit/tree, artifact hashes, migration boundary, and rollback rules. Evergreen setup docs intentionally do not hard-code a release number.

## What should I read next?

Continue with [Core concepts](CORE_CONCEPTS.md), then the [Quick Start](QUICKSTART.md).
