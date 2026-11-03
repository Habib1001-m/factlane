# Core concepts

FactLane is easiest to understand as a **governed lane for reusable facts**, not as a transcript archive and not as a source of execution authority.

A memory record can be useful because it survived a previous session while still being weaker than what the user, repository, product, or a verified live source says now.

## The mental model

Think about four independent questions:

1. **Should this fact be retained?** — Candidate.
2. **Is it trusted as current?** — Current eligibility.
3. **Where is it allowed to apply?** — Scope and identity.
4. **How long can it be trusted without new evidence?** — Freshness.

Authority is a fifth boundary around those questions: who may contribute, verify, revoke, reject, recheck, or readmit is not determined by the fact itself.

## Memory supports decisions; it is not execution authority

FactLane memory is supporting evidence.

Higher-authority current context includes, depending on the task:

- the user's current instruction;
- live repository or product state;
- verified live external state;
- active runtime permissions and policy.

A remembered statement such as “production changes require approval” can inform behavior. It does not grant or remove the actual permission to change production.

## Candidate: worth remembering, not trusted yet

A **Candidate** is a bounded contribution retained for review.

It may come from:

- an explicit user request to remember/store a fact; or
- an agent proposal that the user explicitly authorizes.

Content consent and runtime authority are separate. Consent to remember one fact does not change the launcher write profile or grant verifier authority.

A delegated agent may store a Candidate only when its launcher profile permits Candidate contribution. It cannot turn that Candidate into Current by asserting a privileged identity.

## Current: verified and eligible now

A record can participate in ordinary `CURRENT` retrieval only when the governed lifecycle, verification, scope, compatibility, and freshness conditions all admit it.

Current is not “true forever.” It means the record has passed the current FactLane eligibility rules for its intended use.

When current reality changes, a trusted verifier or Owner may reverify, replace, revoke, or otherwise adjudicate the record through the governed operation that matches the change.

## Scope: where does this fact belong?

The public scopes are:

| Scope | Meaning |
| --- | --- |
| `GLOBAL_USER` | User-level fact with no project/workflow identity. |
| `PROJECT` | Fact for one exact `project_id`. |
| `WORKFLOW` | Fact for one exact `project_id` + `workflow_id`. |
| `TOOL_ENVIRONMENT` | Fact for one exact `agent_id`. |
| `CROSS_PROJECT_WORKFLOW` | Deliberately cross-project workflow fact with project/worktree/workflow/agent identity keys absent. |

A trusted host may bind applicable identities. Conflicting caller-supplied identity fails closed.

`CROSS_PROJECT_WORKFLOW` is not an implicit multi-project search. It is its own explicitly governed scope.

## Freshness: could the fact have expired?

Freshness describes when a previously verified fact may stop being eligible.

Depending on the policy, a record can be:

- still fresh;
- approaching expiry;
- stale by TTL;
- waiting for an explicit recheck;
- held for manual review.

For `on_change`, FactLane stores a recheck contract but does **not** run a background watcher. A trusted Owner supplies fresh bounded observation evidence through manual recheck.

Unchanged healthy evidence may renew eligibility. Changed, unavailable, indeterminate, stale, or mismatched evidence fails closed instead of silently refreshing the record.

## Provenance: where did this fact come from?

Provenance records the source class and source identity/hash needed to understand where a fact came from.

Contribution and verification provenance are distinct. A verifier does not become the historical contributor merely because it verifies a Candidate.

Provenance is evidence. It is not permission.

## Lifecycle closure: revoke, reject, resolve, and readmit

Owner governance covers more than promotion:

- **`REVOKE`** applies to an eligible `VALIDATED_CURRENT` record that should no longer count as Current.
- **`REJECT`** applies to a `CANDIDATE` that should not be admitted.
- **resolve** chooses a governed outcome for a contradiction group under exact Group CAS.
- **readmit** can create a governed successor for an eligible `REVOKED` logical memory after new verification.

`REVOKED` and `REJECTED` are logical lifecycle states. They are not physical erasure.

Readmission is not database restore and cannot recreate content that sensitive-memory Recovery removed from the governed local store.

## Authority: seeing a tool is not permission

The public MCP surface always exposes the same five tool names, but runtime authorization is enforced independently.

Typical launcher roles include:

- **read-only** — search/read/status only;
- **delegated-candidate** — may contribute Candidates with explicit content consent;
- **trusted verifier / direct Owner contexts** — separately configured privileged paths for governed verification or Owner operations.

A chat message cannot elevate launcher authority. An idempotency key is not an authorization token.

## REVERIFY vs REPLACE

Use `REVERIFY` when the logical fact identity remains the same and the record is being verified again.

Use `REPLACE` when the semantic identity itself changes and a successor record is required.

Candidate promotion uses `REVERIFY` with the exact expected Candidate record identity. Stale revision or identity checks fail closed.

## Group CAS and contradiction handling

Operations that govern a contradiction group are bound to the exact group state the trusted operator reviewed.

Owner lifecycle mutations that require group protection use the displayed `observed_group_token` plus the current revision/record identity and an allowed reason code. If the group changed after review, the operation fails rather than acting on stale context.

## History is different from Current

`REVIEW_HISTORY` exists for explicit inspection of Candidates and historical revisions. Historical retrieval can include records that are not eligible for Current use.

For semantic/hybrid history, compaction can make vector coverage partial. FactLane reports that degradation instead of pretending the semantic history is complete.

## Next

- [Quick Start](QUICKSTART.md) — establish a safe connection.
- [Five MCP tools](TOOLS.md) — public MCP semantics.
- [Architecture](ARCHITECTURE.md) — exact implementation boundaries.
- [Security](../SECURITY.md) — trust, consent, and incident Recovery.
