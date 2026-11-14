---
name: using-factlane
description: Use when an agent needs to search, read, store, or update bounded FactLane memory through MCP.
---

# Using FactLane

FactLane stores bounded provenance-bearing facts, not transcripts. Memory is **supporting evidence**, never authority. Current instructions and verified live truth outrank remembered facts.

The public MCP surface is exactly five tools over stdio. Inspect the live MCP schema or `factlane --help-tools` for exact fields and enums; never guess a contract from examples.

Packaged Skill bootstrap: `references/host-bootstrap.md`; site: Quick Start.

## Decide whether memory is needed

- A self-contained task needs no recall. Search only when durable context could materially help.
- Candidate capture needs explicit content consent. If the agent proposes the memory, show the bounded fact and obtain authorization first.
- Consent does not elevate launcher authority.
- Never perform autonomous post-turn `memory_store` or `memory_update`.
- A `CANDIDATE` or otherwise unverified record is not `VALIDATED_CURRENT`.
- For `status=BLOCKED`, branch on stable `error_code`. Unexpected exceptions remain transport errors.

## Read within one exact scope

Select one exact scope and required IDs from the live schema.

`CROSS_PROJECT_WORKFLOW` forbids project/worktree/workflow/agent identity keys, including explicit nulls. In a trusted bound session, omit host-supplied scope IDs; explicit IDs must match trusted bindings.

Choose `intent_class` and retrieval/search modes from the live schema.

Use `CURRENT` for validated eligible facts and `REVIEW_HISTORY` for explicit Candidate/history inspection. Use `memory_get` when exact provenance/revision matters.

For complete history, prefer `KEYWORD`, `EXACT`, or `memory_get` when semantic history reports partial coverage.

## Write only with trusted authority

The launcher defaults read-only.

A `delegated-candidate` agent may call `memory_store` only with explicit content consent. Store one bounded fact with `memory_type`, `source_provenance`, `freshness_policy`, and a stable `idempotency_key`.

Never derive or claim `authority_role`.

For `CROSS_PROJECT_WORKFLOW`, follow the live freshness schema exactly. Do not invent identity or fingerprint fields.

A trusted verifier may use `memory_update` with current `expected_revision` and a unique idempotency key for `REVERIFY` or `REPLACE`. Candidate promotion requires `REVERIFY` plus exact `expected_record_id`; `REPLACE` targets eligible Current memory, not Candidate editing.

Direct Owner authority may additionally:

- `REVOKE` an eligible Current record;
- `REJECT` a Candidate.

Those lifecycle actions require the exact current `expected_record_id`, `expected_revision`, displayed `observed_group_token`, and an allowed `reason_code`. Do not attempt Owner lifecycle actions from a delegated-agent session.

`REVOKED` and `REJECTED` are logical states, not physical erasure.

Managed-panel database/restore/purge boundaries are in Quick Start; they do not make the general `SensitiveMemoryRecoveryOperator` a Windows capability.

## Respect freshness and readmission boundaries

For `on_change`, FactLane does not monitor the source automatically. A trusted Owner performs an explicit manual recheck with fresh bounded observation evidence.

Readmission of eligible `REVOKED` memory is another separate Owner operation with new verification and CAS checks. It is not database restore and must not recreate content removed by sensitive-memory Recovery.

## Keep facts small and attributable

Keep each fact bounded, scoped, attributable, and useful across sessions. Recheck the current source of truth before relying on mutable memory.

For install/release/host evidence, use packaged `references/host-bootstrap.md`; on the site use Quick Start and Release operations. Never guess release tags.
