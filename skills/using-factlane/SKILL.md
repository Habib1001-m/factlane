---
name: using-factlane
description: Use when an agent needs to search, read, store, or update bounded FactLane memory through MCP.
---

# Using FactLane

FactLane stores bounded provenance-bearing facts, not transcripts. Memory is **supporting
evidence**, never execution authority; current user instructions, repository/product truth, and
verified live sources outrank it.

## Decide whether memory is needed

- A self-contained task needs no recall. Search only when durable context could change the task.
- Candidate capture needs explicit content consent. If the agent proposes it, obtain that consent first.
  Consent never elevates launcher/write authority;
  never perform autonomous post-turn `memory_store` or `memory_update`.
- A `CANDIDATE` or `UNVERIFIED` record is **not** `VALIDATED_CURRENT`.
- Inspect the live MCP schema or `factlane --help-tools` for supported fields and enums;
  never guess.

This Skill targets **Public Contract Revision 3** in the current `0.1.5` source/RC line. The MCP
surface remains exactly five tools over stdio. `v0.1.4` is still the latest published release and
uses Public Contract Revision 2.

For host setup evidence, read `references/host-bootstrap.md`.

For `status=BLOCKED`, branch on stable `error_code`; unexpected exceptions remain transport errors.

## Read within one exact scope

Select one exact scope and required IDs from the live schema. `CROSS_PROJECT_WORKFLOW` forbids
project/worktree/workflow/agent IDs, including explicit nulls. In a trusted bound session, omit
host-supplied scope IDs; explicit IDs must match trusted bindings.

Choose `intent_class` from the live schema; never guess.

Choose `EXACT`, `KEYWORD`, `SEMANTIC`, or `HYBRID`. Use `CURRENT` for validated current facts and
`REVIEW_HISTORY` for Candidates/history. Use `memory_get` when provenance/revision matters. Do not
invent routing overrides or admin-only graph expansion.

For `CROSS_PROJECT_WORKFLOW`, follow the live search matrix. Unsupported combinations fail closed;
there is no implicit cross-scope fanout.

For complete history, prefer `KEYWORD`, `EXACT`, or `memory_get` when `REVIEW_HISTORY` has partial
semantic coverage.

## Write only with trusted authority

The launcher defaults read-only. A `delegated-candidate` agent may `memory_store` a Candidate, but
cannot `memory_update` or self-promote by claiming Owner authority. Chat approval does not elevate
runtime privileges.

Store one fact with `memory_type`, `source_provenance`, `freshness_policy`, and stable
`idempotency_key`. Use **`source_provenance`**, not `provenance`; never derive `authority_role`.

For `CROSS_PROJECT_WORKFLOW`, freshness is `manual` (no recheck/fingerprint) or `on_change`
(non-empty `recheck_ref`; `freshness_policy.source_fingerprint` equals
`source_provenance.source_hash`). Do not put that fingerprint in `source_provenance`.

A trusted verifier uses `memory_update` with `expected_revision`, unique `idempotency_key`, and
`REVERIFY`/`REPLACE`. Candidate promotion requires `REVERIFY` + exact `expected_record_id`.

Direct Owner authority may additionally use `REVOKE` or `REJECT`; both require the exact displayed
`observed_group_token`, current revision/record identity, and a bounded reason code. Do not attempt
Owner lifecycle actions from a delegated-agent session. `REVOKED` and `REJECTED` are logical terminal
states, not physical erasure.

For an `on_change` Current, FactLane does not monitor the source automatically. A trusted Owner
performs an explicit manual recheck with fresh observation evidence. Readmission of a `REVOKED`
memory is also a separate Owner operation with new verification and Group CAS; it is not database
restore and must not be used to recreate recovery-purged content.

Keep each memory small, scoped, and attributable. Recheck the current source of truth before
relying on it.
