# Frequently asked questions

This page is for deciding whether FactLane fits your use case before you spend time on technical
setup.

## Does FactLane replace my assistant?

No. FactLane is a local memory layer behind a **compatible AI-agent host**. You keep using the
assistant or coding agent; the host connects it to FactLane's memory tools.

## Do I need to be a developer?

Not necessarily for normal use after FactLane is connected, but **v0.1.4 setup is still
technical**. Someone must install FactLane, provide a compatible command-launched stdio MCP host,
and configure local storage and a supported local embedding model.

If nobody can perform that setup, FactLane is not currently a zero-configuration memory feature.

## What is FactLane a good fit for?

FactLane is designed for bounded durable facts such as:

- user preferences;
- important project facts;
- recurring workflow rules;
- scoped tool or environment facts;
- narrowly governed cross-project workflow rules.

It is useful when those facts should survive a session **without** allowing old memory to silently
overrule current instructions or verified live state.

## What is it not trying to be?

FactLane is not:

- a transcript archive;
- a bulk file or document crawler;
- a general search/indexing system for arbitrary corpora;
- a backup service;
- a managed remote memory cloud;
- an HTTP/SSE/Streamable HTTP MCP server in v0.1.4;
- an automatic permission system for an agent.

Facts are bounded to 2,000 UTF-8 bytes.

## Where does the data live?

The qualified v0.1.4 profile uses local SQLite/SQLite-vec storage and supported local Ollama
embeddings. The shipped embedding path does not fall back to a remote provider automatically.

Read [Environment and compatibility](ENVIRONMENT.md) for the exact supported profile.

## Can an ordinary agent verify its own memory?

No. A delegated agent may be allowed to contribute a **Candidate**, but Candidate promotion is a
separate trusted verification operation. Seeing `memory_update` in tool discovery does not grant
verifier authority.

Read [Core concepts](CORE_CONCEPTS.md) for the Candidate → Current model.

## What wins if memory conflicts with what I say now?

Current authoritative context wins. A current user instruction, current repository/product state,
or verified live source can outrank remembered information.

Memory supports the decision; it does not own the decision.

## Can two assistants use the same facts?

More than one compatible local agent host can use the same governed FactLane store when the
operator configures them against that environment. That lets hosts reuse bounded facts instead of
sharing entire transcripts.

The hosts do not automatically gain identical write or verification authority merely because they
can see the same public tools.

## What should I read next?

If the fit above matches what you need, continue with [Core concepts](CORE_CONCEPTS.md), then the
[Quick start](QUICKSTART.md).
