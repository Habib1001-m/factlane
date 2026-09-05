---
name: using-factlane
description: Use when an agent needs to search, read, store, or update bounded FactLane memory through MCP.
---

# Using FactLane

FactLane shares bounded facts, not transcripts or context dumps. Memory is supporting
evidence; it is never execution authority.

## Before reading

1. Choose one exact scope. `PROJECT` requires `project_id`; `WORKFLOW` requires both
   `project_id` and `workflow_id`; `GLOBAL_USER` carries no project/workflow identity;
   `TOOL_ENVIRONMENT` requires `agent_id`.
2. Map the need to one `intent_class`: `CURRENT_PROJECT_STATE`,
   `PROJECT_DESIGN_RATIONALE`, `USER_PREFERENCE_OR_DURABLE_FACT`, `WORKFLOW_RULE`,
   `TOOL_ENVIRONMENT_STATE`, `HISTORICAL_QUESTION`, or `GENERAL_TASK_NO_MEMORY_REQUIRED`.
3. Select retrieval deliberately: `EXACT`, `KEYWORD`, `SEMANTIC`, or `HYBRID`.
   `CURRENT` is the normal retrieval mode; use `REVIEW_HISTORY` for historical questions.

Search first. Use `memory_get` with the returned exact `memory_id` when exact readback,
revision, or provenance matters.

## Before writing

Persist or update only when the active Owner/host policy explicitly authorizes it. Store
one bounded fact with `source_provenance`, `freshness_policy`, and a stable unique
`idempotency_key`. A store request also names its scope and `memory_type`; do not send
`provenance`—the field is `source_provenance`.

For updates, read the current record first. Send its `expected_revision`, a unique
`idempotency_key`, and exactly one mode: `REVERIFY` for a checked current fact or
`REPLACE` for an intentional new logical value. Supply current verification and
provenance as required by that mode. For `REPLACE`, include the replacement fact,
`source_provenance`, `freshness_policy`, `source_timestamp`, and `verified_by`;
`last_verified_at` is optional and generated when omitted.

Never guess enum names or request fields. Inspect the live MCP tool schema or run
`factlane --help-tools`; corrective errors list safe supported choices. Keep memory
small, scoped, provenance-bearing, and separate from the task's direct source of truth.
