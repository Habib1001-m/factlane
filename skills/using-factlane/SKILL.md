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
- Candidate capture needs explicit content consent: the user asks to remember/store it, or the
  agent proposes it and asks first. Consent never elevates launcher/write authority. Never perform
  autonomous post-turn `memory_store` or `memory_update`.
- A `CANDIDATE` or `UNVERIFIED` record is **not** `VALIDATED_CURRENT`.
- Inspect the live MCP schema or `factlane --help-tools` for supported fields and enums;
  never guess.

This Skill targets **Public Contract Revision 2**.

For host-neutral setup/registration evidence, read `references/host-bootstrap.md`; never guess a
host-specific Skill path or mechanism.

Governed failures return `status=BLOCKED` with stable `error_code`, safe `message`, and
`audit.retryable`; branch on the code. Unexpected internal exceptions remain transport errors.

## Read within one exact scope

Select one scope. `PROJECT` needs exact `project_id`; `WORKFLOW` needs that ID + `workflow_id`;
`GLOBAL_USER` is unbound; `TOOL_ENVIRONMENT` needs `agent_id`. `CROSS_PROJECT_WORKFLOW`
**forbids** project/worktree/workflow/agent IDs, including explicit nulls.

In a trusted bound session, omit host-supplied scope IDs; do not reconstruct them. Explicit scope
IDs must match trusted bindings. Other permitted filters remain caller-directed.

Choose an `intent_class`: `CURRENT_PROJECT_STATE`, `PROJECT_DESIGN_RATIONALE`,
`USER_PREFERENCE_OR_DURABLE_FACT`, `WORKFLOW_RULE`, `TOOL_ENVIRONMENT_STATE`,
`HISTORICAL_QUESTION`, or `GENERAL_TASK_NO_MEMORY_REQUIRED`.

Choose `EXACT`, `KEYWORD`, `SEMANTIC`, or `HYBRID`. Use `CURRENT` for validated current facts and
`REVIEW_HISTORY` for Candidates/history. Use `memory_get` when provenance/revision matters. Do not
invent routing overrides or admin-only graph expansion.

For `CROSS_PROJECT_WORKFLOW`, permitted search intent/mode pairs are
`WORKFLOW_RULE + CURRENT`, `WORKFLOW_RULE + REVIEW_HISTORY`, and
`HISTORICAL_QUESTION + REVIEW_HISTORY`. `GENERAL_TASK_NO_MEMORY_REQUIRED` returns
`NO_MEMORY_NEEDED` after request validation. Other combinations fail closed; there is no
implicit cross-scope fanout.

Compaction may remove `HISTORICAL` vectors. In `REVIEW_HISTORY`, incomplete `SEMANTIC`/`HYBRID`
coverage reports `HISTORY_SEMANTIC_PARTIAL`; use `KEYWORD`, `EXACT`, or `memory_get` for complete
history. Concurrent result-budget loss still sets `budget.truncated=true`.

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
`REPLACE` needs `replacement.fact`, `.source_provenance`, `.freshness_policy`, `.source_timestamp`,
and `.verified_by`; omitted `.last_verified_at` is generated.

Keep each memory small, scoped, and attributable. Recheck the current source of truth before
relying on it.
