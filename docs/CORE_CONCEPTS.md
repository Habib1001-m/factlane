# Core concepts

FactLane is easier to understand if you separate four questions:

1. **What is worth remembering?**
2. **What is trusted as Current?**
3. **Where is that fact allowed to apply?**
4. **What outranks memory when the world changes?**

## The mental model in plain language

| Plain-language question | FactLane term |
| --- | --- |
| Is this worth keeping for later? | **Candidate** |
| Has it been separately verified and is it eligible now? | **Current** |
| Where is this fact allowed to apply? | **Scope** |
| Could this fact have become stale? | **Freshness** |
| Where did the fact come from? | **Provenance** |

You can understand the product with those five ideas. The sections near the end of this page add
the exact integration mechanics for developers and operators.

## Memory supports decisions; it is not execution authority

FactLane memory is supporting state.

Current user instructions, current repository or product state, and verified live sources can
outrank remembered information. A stored preference may be useful tomorrow, but it cannot force
an assistant to ignore what you are asking for now.

This is the central product boundary.

## Candidate: worth remembering, not trusted yet

A compatible host can allow an assistant to contribute a **Candidate**: a fact that looks useful
enough to keep available for review.

Think of Candidate as:

> “This may be useful later. Keep it available for review.”

It does **not** mean:

> “Treat this as verified current truth.”

The assistant that contributed it cannot make it Current merely by claiming more authority.

## Current: verified and eligible to use now

An eligible Candidate becomes Current only after a separate trusted verification step. Normal
current-state use excludes unverified Candidates.

Think of Current as:

> “Verified, fresh enough, and eligible for this current-state lookup.”

It still remains memory. It does not supersede a newer authoritative instruction or live source.

## Scope: where does this fact belong?

FactLane defines five public scopes:

| Scope | Plain-language meaning |
| --- | --- |
| `GLOBAL_USER` | A fact that belongs to the user without project/workflow identity. |
| `PROJECT` | A fact for one exact project. |
| `WORKFLOW` | A fact for one exact project + workflow. |
| `TOOL_ENVIRONMENT` | A fact tied to one exact agent/tool environment. |
| `CROSS_PROJECT_WORKFLOW` | An identity-free workflow rule that applies across projects under its specific contract. |

`CROSS_PROJECT_WORKFLOW` does **not** mean “search all projects.”

## Freshness: could the fact have expired?

Some facts are long-lived:

> “I prefer concise answers.”

Others become stale quickly:

> “The current release is v0.1.4.”

FactLane stores freshness policy with memory so current-state retrieval can exclude facts that
are no longer eligible.

Freshness is one reason a memory database should not be treated as an unquestionable truth
source.

## Provenance: where did this fact come from?

A durable fact is more useful when an operator or agent can inspect its source. FactLane records
source provenance rather than treating the text alone as sufficient context.

## For integrators: authority and revision mechanics

The rest of this page uses runtime terminology that matters when configuring or integrating a
FactLane host. If you are evaluating the product as an end user, you can continue directly to the
[Quick start](QUICKSTART.md) when someone is ready to perform the setup.

### Write profiles: seeing a tool is not the same as being allowed to use it

FactLane exposes the same five public MCP tools, but launcher configuration controls authority.

| Launcher profile | Practical meaning |
| --- | --- |
| omitted / `read-only` | Search, get and status only. |
| `delegated-candidate` | Ordinary agent may contribute Candidate memory. |
| trusted verifier/owner profiles | Restricted operator/verifier actions, including governed promotion. |

An agent does not become a verifier simply because `memory_update` appears in tool discovery.

### Bound identity and current retrieval

A trusted launcher can bind relevant project, workflow, or tool identity context. Conflicting
caller-supplied bound identities are rejected rather than silently rewritten.

Normal `CURRENT` retrieval admits eligible `VALIDATED_CURRENT` facts, not unverified Candidates.
Semantic or hybrid `CURRENT` retrieval can also preserve source diversity so one provenance source
does not crowd every result slot merely because it contains several nearby matches.

### REVERIFY vs REPLACE

Trusted promotion uses `memory_update` with `REVERIFY`, the expected revision, and the exact
expected record identity.

`REVERIFY` preserves the logical memory identity while refreshing verification of the same
fact/scope/type/subject.

`REPLACE` is used when the semantic identity itself changes.

This distinction prevents a “verification” operation from quietly turning into an unrelated
reclassification.

### History is different from Current

`REVIEW_HISTORY` can expose past revisions and Candidates for explicit inspection.

That history is useful evidence, but it is not automatically eligible for a `CURRENT` answer.

## Next

- [Quick start](QUICKSTART.md)
- [Five MCP tools](TOOLS.md) — developer reference after the connection works.
- [Architecture](ARCHITECTURE.md) — optional deeper system model.
