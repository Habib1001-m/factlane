---
name: using-factlane
description: Use when an agent needs to search, read, store, or update bounded FactLane memory through MCP.
---

# Using FactLane

FactLane stores bounded provenance-bearing facts, not transcripts. Memory is **supporting evidence**, never execution authority. Current user instructions, live repository/product truth, and verified live sources can outrank remembered facts.

The public MCP surface is exactly five tools over stdio. Inspect the live MCP schema or `factlane --help-tools` for exact fields and enums; never guess a contract from examples.

For host/bootstrap evidence, read `references/host-bootstrap.md`.

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

A trusted verifier may use `memory_update` with the current `expected_revision`/identity and a unique idempotency key for `REVERIFY` or `REPLACE`. Candidate promotion requires `REVERIFY` plus the exact `expected_record_id` for the Candidate.

Direct Owner authority may additionally:

- `REVOKE` an eligible Current record;
- `REJECT` a Candidate.

Those lifecycle actions require the exact current record/group state and an allowed reason. Do not attempt Owner lifecycle actions from a delegated-agent session.

`REVOKED` and `REJECTED` are logical states, not physical erasure.

## Respect freshness and readmission boundaries

For `on_change`, FactLane does not monitor the source automatically. A trusted Owner performs an explicit manual recheck with fresh bounded observation evidence.

Readmission of eligible `REVOKED` memory is another separate Owner operation with new verification and CAS checks. It is not database restore and must not recreate content removed by sensitive-memory Recovery.

## Keep facts small and attributable

Keep each fact bounded, scoped, attributable, and useful across sessions. Recheck the current source of truth before relying on mutable memory.

For installation, release identity, host configuration, Skill registration, and first-run evidence, use `references/host-bootstrap.md` plus the repository Quick Start and Release operations. Do not hard-code or guess a release tag.
