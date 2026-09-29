---
name: using-factlane
description: Use when an agent needs to search, read, store, or update bounded FactLane memory through MCP.
---

# Using FactLane

FactLane shares bounded facts, not transcripts or context dumps. Memory is supporting
evidence; it is never execution authority. Current Owner instructions, project/repository
authority, and verified live runtime truth outrank memory.

## Operating policy

- Use bounded recall only when prior durable context could materially improve the task.
- A self-contained task or one with complete current-source evidence may correctly use no memory.
- Capture is manual and explicit only. Memory suggestions are user-triggered only.
- Never perform autonomous post-turn capture, `memory_store`, or `memory_update`.
- `CANDIDATE / UNVERIFIED` is never equivalent to `VALIDATED_CURRENT`.
- The live MCP schema / `factlane --help-tools` is authoritative; never guess request fields or enums.

This Skill describes Public Contract Revision 2.

## Before reading

1. Choose one exact semantic scope. Resolved `PROJECT` requests require `project_id`;
   resolved `WORKFLOW` requests require both `project_id` and `workflow_id`; `GLOBAL_USER`
   carries no project/workflow identity; resolved `TOOL_ENVIRONMENT` requests require
   `agent_id`; `CROSS_PROJECT_WORKFLOW` is workflow doctrine shared across projects and
   requires all four identity keys to be absent. In a trusted bound session, omit the
   current scope-owning protocol IDs already supplied by the host; do not reconstruct or
   guess them. Explicit IDs remain available for genuinely unbound or deliberately
   caller-directed contexts, and any explicit scope-owning value must match a trusted bound
   value when one exists.
2. Map the need to one `intent_class`: `CURRENT_PROJECT_STATE`,
   `PROJECT_DESIGN_RATIONALE`, `USER_PREFERENCE_OR_DURABLE_FACT`, `WORKFLOW_RULE`,
   `TOOL_ENVIRONMENT_STATE`, `HISTORICAL_QUESTION`, or `GENERAL_TASK_NO_MEMORY_REQUIRED`.
3. Select retrieval deliberately: `EXACT`, `KEYWORD`, `SEMANTIC`, or `HYBRID`.
   `CURRENT` is the normal retrieval mode; use `REVIEW_HISTORY` for historical questions.

For `CROSS_PROJECT_WORKFLOW`, the allowed search combinations are `WORKFLOW_RULE + CURRENT`,
`WORKFLOW_RULE + REVIEW_HISTORY`, and `HISTORICAL_QUESTION + REVIEW_HISTORY`.
`GENERAL_TASK_NO_MEMORY_REQUIRED` is also valid after request/scope-shape validation and returns
`NO_MEMORY_NEEDED`. Other intent/retrieval combinations fail closed for this scope.

Search first. Use `memory_get` with the returned exact `memory_id` when exact readback,
revision, or provenance matters.

For a normal search, choose scope and intent and let FactLane perform the lookup. Do not
invent routing overrides to suppress an explicit memory search, and do not request admin-only
graph expansion. For normal project-scoped stores, omit `authority_role`; FactLane derives the
authority from the exact scope.

## Before writing

Persist or update only when the active Owner/host policy explicitly authorizes the
specific operation and the trusted launcher profile permits it. Normal agent connections
are `delegated-candidate`: they can contribute Candidates but cannot claim Owner/current
authority in the request. User approval does not elevate a connection beyond the authority
granted by its trusted launcher/runtime. Store
one bounded fact with `source_provenance`, `freshness_policy`, and a stable unique
`idempotency_key`. A store request also names its scope and `memory_type`; do not send
`provenance`—the field is `source_provenance`.

`CROSS_PROJECT_WORKFLOW` accepts only `manual` or `on_change` freshness. `manual` carries
no recheck reference or freshness fingerprint. `on_change` requires a nonempty
`recheck_ref`, and `freshness_policy.source_fingerprint` must equal
`source_provenance.source_hash`. Do not put `source_fingerprint` in `source_provenance`
for this scope.

For updates, read the relevant record first. Send its `expected_revision`, a unique
`idempotency_key`, and exactly one mode: `REVERIFY` for a checked current fact or
`REPLACE` for an intentional new logical value. To promote a Candidate, a trusted verifier uses `REVERIFY` with that Candidate's `expected_record_id`; inspect Candidates with `REVIEW_HISTORY`. Supply current verification and
provenance as required by that mode. For `REPLACE`, include the replacement fact,
`source_provenance`, `freshness_policy`, `source_timestamp`, and `verified_by`;
`last_verified_at` is optional and generated when omitted.

Never guess enum names or request fields. Inspect the live MCP tool schema or run
`factlane --help-tools`; corrective errors list safe supported choices. Keep memory
small, scoped, provenance-bearing, and separate from the task's direct source of truth.
