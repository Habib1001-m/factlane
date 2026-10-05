# Core concepts

FactLane is easier to understand if you separate four questions:

1. **What is worth remembering?**
2. **What is trusted as Current?**
3. **Where is that fact allowed to apply?**
4. **What outranks memory when the world changes?**

## Memory supports decisions; it is not execution authority

FactLane memory is supporting state.

Current user instructions, current repository or product state, and verified live sources can
outrank remembered information. A stored preference may be useful tomorrow, but it cannot force
an assistant to ignore what you are asking for now.

This is the central product boundary.

## Candidate: worth remembering, not trusted yet

A host running with the `delegated-candidate` write profile can allow an ordinary agent to
contribute a **Candidate**.

Think of Candidate as:

> “This may be useful later. Keep it available for review.”

It does **not** mean:

> “Treat this as verified current truth.”

The contributing agent cannot promote its own Candidate merely by claiming a verifier identity
inside the tool request.

## Current: verified and eligible to use now

A trusted verifier can promote an eligible Candidate through `memory_update` with
`REVERIFY`, the expected revision, and the exact expected record identity.

Normal `CURRENT` retrieval admits eligible `VALIDATED_CURRENT` facts, not unverified
Candidates.

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

A trusted launcher can bind relevant identity context. Conflicting caller-supplied bound
identities are rejected rather than silently rewritten.

## Freshness: could the fact have expired?

Some facts are long-lived:

> “I prefer concise answers.”

Others become stale quickly:

> “The current release is v0.1.3.”

FactLane stores freshness policy with memory so current-state retrieval can exclude facts that
are no longer eligible.

Freshness is one reason a memory database should not be treated as an unquestionable truth
source.

## Provenance: where did this fact come from?

A durable fact is more useful when an operator or agent can inspect its source. FactLane records
source provenance rather than treating the text alone as sufficient context.

Retrieval can also preserve source diversity for semantic/hybrid `CURRENT` search so one source
does not crowd every result slot merely because it contains several nearby matches.

## Write profiles: seeing a tool is not the same as being allowed to use it

FactLane exposes the same five public MCP tools, but launcher configuration controls authority.

| Launcher profile | Practical meaning |
| --- | --- |
| omitted / `read-only` | Search, get and status only. |
| `delegated-candidate` | Ordinary agent may contribute Candidate memory. |
| trusted verifier/owner profiles | Restricted operator/verifier actions, including governed promotion. |

An agent does not become a verifier simply because `memory_update` appears in tool discovery.

## REVERIFY vs REPLACE

`REVERIFY` preserves the logical memory identity while refreshing verification of the same
fact/scope/type/subject.

`REPLACE` is used when the semantic identity itself changes.

This distinction prevents a “verification” operation from quietly turning into an unrelated
reclassification.

## History is different from Current

`REVIEW_HISTORY` can expose past revisions and Candidates for explicit inspection.

That history is useful evidence, but it is not automatically eligible for a `CURRENT` answer.

## Next

- [Everyday use cases](USE_CASES.md)
- [Quick start](QUICKSTART.md)
- [Five MCP tools](TOOLS.md)
- [Architecture](ARCHITECTURE.md)
