---
slug: /
---

# FactLane documentation

FactLane gives compatible AI-agent hosts a local memory layer for **bounded facts**: useful
preferences, project facts, workflow rules, and other durable information that should survive
longer than one conversation.

The important part is not merely remembering. FactLane separates a fact that is **worth keeping**
from one that is **trusted as Current**, and it keeps memory subordinate to current instructions
and live authoritative state.

## New to FactLane?

Start with the outcome rather than the protocol:

1. Read [Everyday use cases](USE_CASES.md) to see what FactLane can remember and where its
   boundaries matter.
2. Read the [FAQ](FAQ.md) to check fit before spending time on setup.
3. Read [Core concepts](CORE_CONCEPTS.md) for the plain-language model of Candidate, Current,
   scope, freshness, and authority.
4. Follow the [Quick start](QUICKSTART.md) when you are ready to install and connect a supported
   local MCP host.
5. Use [Five MCP tools](TOOLS.md) as the developer reference once the connection works.

### Do I need to be a developer to benefit from it?

Not necessarily for normal use, but **the current v0.1.4 setup is still technical**.

FactLane is not a separate chatbot. It runs locally as a memory service behind a compatible AI
agent host. Someone must install and configure that connection. After it is connected, the useful
part can feel much simpler: keep using the assistant while approved preferences, project facts and
recurring rules remain available across sessions.

If you are mainly trying to understand whether this solves your problem, start with
[Everyday use cases](USE_CASES.md) and the [FAQ](FAQ.md). You can leave Python, SQLite, MCP and
embedding details until you are ready to install it yourself.

## Building or integrating?

- [Quick start](QUICKSTART.md) — establish a safe read-only connection first.
- [Five MCP tools](TOOLS.md) — what each public tool is for after connectivity works.
- [Architecture](ARCHITECTURE.md) — request path, scope/identity model, storage and recovery.
- [Environment and compatibility](ENVIRONMENT.md) — Python, SQLite, embedding profiles and
  deployment limits.
- [Release operations](RELEASE_OPERATIONS.md) — exact release identity, upgrades, rollback and
  reverification.
- [Security](../SECURITY.md) — trust boundaries and sensitive-memory recovery.

## Current public release

FactLane **v0.1.4** is the current production release. Its qualified public profile is
intentionally bounded: Python 3.11+, linked SQLite 3.42.0+, command-launched stdio MCP, supported
local Ollama embeddings, and the documented local POSIX storage/recovery contract.

The live MCP schema and `factlane --help-tools` are authoritative for exact request signatures
and enum values.
