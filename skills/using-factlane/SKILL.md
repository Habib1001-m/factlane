---
name: using-factlane
description: Use when an agent needs to search, read, store, or update bounded FactLane memory through MCP.
---

# Using FactLane

FactLane stores bounded, provenance-bearing facts, not transcripts. Memory is **supporting
evidence**, never execution authority. Current user instructions, current repository/product
truth, and verified live sources outrank remembered facts.

## Decide whether memory is needed

- A self-contained task needs no recall. Search only when durable context could change the
  answer or action.
- Capture is explicit; suggestions are user-triggered. Never perform autonomous post-turn
  `memory_store` or `memory_update`.
- A `CANDIDATE` or `UNVERIFIED` record is **not** `VALIDATED_CURRENT`.
- Inspect the live MCP schema or `factlane --help-tools` for supported fields and enums;
  never guess.

This Skill targets **Public Contract Revision 2**.

## Read within one exact scope

Select one scope before searching. `PROJECT` requires an exact `project_id`; `WORKFLOW`
requires that project ID and `workflow_id`; `GLOBAL_USER` is not project-bound;
`TOOL_ENVIRONMENT` requires `agent_id`. `CROSS_PROJECT_WORKFLOW` is for cross-project
workflow doctrine and **forbids** `project_id`, `worktree_id`, `workflow_id`, and
`agent_id`, including explicit null values.

In a trusted bound session, omit host-supplied scope-owning IDs; do not reconstruct them.
Explicit scope-owning IDs must match trusted values (`PROJECT.project_id`,
`WORKFLOW.project_id`/`workflow_id`, `TOOL_ENVIRONMENT.agent_id`). Other permitted
caller-directed filters remain caller-directed.

Choose an `intent_class`: `CURRENT_PROJECT_STATE`, `PROJECT_DESIGN_RATIONALE`,
`USER_PREFERENCE_OR_DURABLE_FACT`, `WORKFLOW_RULE`, `TOOL_ENVIRONMENT_STATE`,
`HISTORICAL_QUESTION`, or `GENERAL_TASK_NO_MEMORY_REQUIRED`.

Choose `EXACT`, `KEYWORD`, `SEMANTIC`, or `HYBRID`. Use `CURRENT` for validated current
facts and `REVIEW_HISTORY` to inspect Candidates or older revisions. Search first; use
`memory_get` on the returned `memory_id` when exact provenance or revision matters. Do not
invent routing overrides or request admin-only graph expansion.

For `CROSS_PROJECT_WORKFLOW`, permitted search intent/mode pairs are
`WORKFLOW_RULE + CURRENT`, `WORKFLOW_RULE + REVIEW_HISTORY`, and
`HISTORICAL_QUESTION + REVIEW_HISTORY`. `GENERAL_TASK_NO_MEMORY_REQUIRED` returns
`NO_MEMORY_NEEDED` after request validation. Other combinations fail closed; there is no
implicit cross-scope fanout.

Compaction may remove vectors from `HISTORICAL` records. In `REVIEW_HISTORY`, incomplete
`SEMANTIC`/`HYBRID` coverage reports `HISTORY_SEMANTIC_PARTIAL`. Prefer `KEYWORD`,
`EXACT`, or `memory_get` when complete compacted history matters. Concurrent result-budget
loss sets `budget.truncated=true` without erasing the history degradation.

## Write only with trusted authority

The launcher defaults to read-only. A normal `delegated-candidate` agent may call
`memory_store` to propose a Candidate, but cannot call `memory_update` or promote itself by
claiming Owner authority. Chat approval does not elevate launcher/runtime privileges.

Store one fact with `memory_type`, `source_provenance`, `freshness_policy`, and a stable
`idempotency_key`. Use **`source_provenance`**, not `provenance`; do not supply a derived
`authority_role`.

For `CROSS_PROJECT_WORKFLOW`, freshness is `manual` (no recheck reference or fingerprint)
or `on_change` (non-empty `recheck_ref` and `freshness_policy.source_fingerprint` equal to
`source_provenance.source_hash`). Do not put that fingerprint in `source_provenance` for
this scope.

A separately trusted verifier can use `memory_update`: read first, then provide
`expected_revision`, a unique `idempotency_key`, and `REVERIFY` or `REPLACE`. Candidate
promotion requires `REVERIFY` with its exact `expected_record_id`. `REPLACE` needs
`replacement.fact`, `replacement.source_provenance`, `replacement.freshness_policy`,
`replacement.source_timestamp`, and `replacement.verified_by`; omitted
`replacement.last_verified_at` is generated.

Keep each memory small, scoped, and attributable. Recheck the current source of truth before
relying on it.
