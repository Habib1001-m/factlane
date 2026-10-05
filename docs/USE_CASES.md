# Everyday use cases

You do not need to understand vector search or database internals to understand why FactLane
exists.

The basic problem is simple: an AI assistant is more useful when it can reuse important facts
from earlier work, but **old memory should not silently become the final authority over what is
true now**.

FactLane does not replace your assistant. It sits behind a **compatible AI-agent app or host** as
its local memory layer. It does not add memory to every chat product automatically.

In the current v0.1.3 release, someone still needs to perform the technical setup: the compatible
host launches FactLane locally over stdio MCP and connects to its tools. Once that connection is
configured, the everyday use case can stay much simpler than the setup.

## Remember my preferences between sessions

Imagine telling an assistant:

> Keep answers concise, explain in Arabic, and preserve English technical terms.

That preference may be useful next week or in another session. FactLane can store it as a
bounded fact rather than requiring the assistant to replay the old conversation.

The important boundary is that a newer instruction still wins. If you later say:

> Go deep on this one.

FactLane memory does not have the authority to override your current request.

## Remember the important facts about my project

You may repeatedly need an assistant to know things such as:

- the product goal;
- the current supported release;
- an architectural decision;
- the repository or project scope;
- an agreed workflow rule.

Instead of pasting a long project history into every session, a compatible host can retrieve
eligible FactLane facts for the exact project scope.

Facts that can become stale still need freshness and verification discipline. “Current release is
v0.1.3” is not the same kind of memory as “I prefer concise answers.”

## Remember how I like recurring work handled

FactLane can represent durable workflow facts such as:

> Production changes require owner approval.

or:

> Run local tests before preparing a review.

The exact authority of the connected agent still comes from the host configuration and current
instructions. A memory describing a rule is supporting state; it is not a mechanism for granting
new execution permissions.

## Reuse approved facts across more than one agent host

FactLane is a local, multi-host memory plane. More than one compatible local agent host can use
the same governed fact store when the operator configures them against that environment.

This lets the hosts reuse **facts** instead of sharing entire transcripts.

The hosts do not gain identical authority merely because they see the same tools. Launcher
profiles and trusted context determine what each connection may do.

## Let an assistant propose a useful memory without trusting it immediately

An ordinary delegated agent can be allowed to contribute a **Candidate**:

> This looks worth remembering.

That is deliberately different from:

> This is verified Current truth.

A separate trusted verification operation is required to promote an eligible Candidate. The
agent cannot self-promote by putting a privileged identity claim into its request.

Read [Core concepts](CORE_CONCEPTS.md) for the Candidate → Current lifecycle.

## Keep memory inside the right boundary

FactLane does not treat memory as one giant bucket. Public scopes include:

- `GLOBAL_USER`
- `PROJECT`
- `WORKFLOW`
- `TOOL_ENVIRONMENT`
- `CROSS_PROJECT_WORKFLOW`

For example, a fact about one project should not implicitly fan out into another project merely
because the same assistant is connected.

See [Architecture](ARCHITECTURE.md) for the exact identity rules.

## What FactLane is not

FactLane is not:

- a full transcript archive;
- a bulk file or document crawler;
- a remote memory cloud;
- a substitute for current instructions or live authoritative sources;
- an automatic permission system for an agent;
- a general backup service.

Facts are bounded to 2,000 UTF-8 bytes. The current server supports command-launched stdio MCP
and supported local Ollama embeddings.

When the use case above matches what you want, continue with the [Quick start](QUICKSTART.md).
