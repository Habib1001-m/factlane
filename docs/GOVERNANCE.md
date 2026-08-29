# FactLane Governance

## Canonical development loop

```text
repo truth
-> TASKBOARD.md
-> fresh/synchronized main
-> hermes/<task>
-> implementation/docs/tests
-> bounded evidence
-> commit
-> push task branch
-> pull request
-> CI
-> Owner/Advisor review
-> merge by Owner/Advisor
-> TASKBOARD reconciliation
```

`TASKBOARD.md` is the one live project board. Its version is an internal field. Never create a new numbered taskboard file per cycle.

## Hermes change governance

```text
NO_DIRECT_PUSH_TO_MAIN_BY_EXECUTION_AGENTS=TRUE
HERMES_DIRECT_PUSH_TO_ORIGIN_MAIN=FORBIDDEN
HERMES_DIRECT_PUSH_TO_MAIN=FORBIDDEN
HERMES_FORCE_PUSH=FORBIDDEN
HERMES_MAIN_DEVELOPMENT=FORBIDDEN
HERMES_WORK_BRANCH_PREFIX=hermes/
TASK_BRANCH_REQUIRED=TRUE
PR_REQUIRED=TRUE
CI_BEFORE_MERGE=TRUE
OWNER_OR_ADVISOR_MERGE_GATE=REQUIRED
SAME_ACCOUNT_PR_DOES_NOT_EQUAL_INDEPENDENT_REVIEW=TRUE
```

Execution agents use a task branch and a pull request; they do not push directly to `main`, force-push, self-approve, invent a second reviewer identity, or bypass protection. A pull request from the single GitHub account currently in use is not an independent identity approval (`PR != independent identity approval`), but it remains the change boundary for scope, diff, CI, audit trail, and Owner/Advisor review. Independent decision review is performed outside the GitHub approval identity against the same PR head SHA. Hermes opens the PR, returns evidence, and stops; Owner/Advisor performs the merge.

See [WORKFLOW_TEMPLATE.md](WORKFLOW_TEMPLATE.md) for an adaptable user-facing example. It is a template, not a prescription.

## Operating principles

```text
REUSE_FIRST
VERIFY_BEFORE_REUSE
PROVENANCE_ALWAYS
REUSE_THE_ASSET_DO_NOT_INHERIT_THE_OWNER
CACHE_CAN_SEED_BUT_MUST_NOT_BECOME_HIDDEN_RUNTIME_AUTHORITY
AFTER_BOOTSTRAP_NO_UNBOUNDED_TOOL_HUNTING
```

Reuse is permitted only when identity, compatibility, runtime ownership, and deterministic reacquisition are understood. A working artifact discovered in a cache or another product runtime must not become an undocumented dependency.

## Truth and evidence

Repository truth and accepted current evidence outrank memory summaries and historical reports. Memory is supporting state only. Contradictions are surfaced and reconciled; they are never resolved by majority vote across stale sources.

Every accepted gate records the exact commit/evidence identity needed to reproduce the decision. External reviews are verified against the codebase before implementation; reviewer severity labels are not authority by themselves.

## Scope control

Do not silently expand a task. Unrelated refactors, dependency upgrades, new services, new provider fallbacks, and new automation require demonstrated value and a compatible gate.

Destructive Git/history rewrites, durable-state deletion, credential mutation, live host configuration, production release/deployment, and other irreversible external actions require explicit Owner authorization.

## Dependency and tool policy

`uv.lock` owns Python dependency resolution. Project tools such as pytest are declared project dependencies, not discovered globally at runtime. External assets are recorded in `environment-provenance.json` when the lock does not sufficiently describe them.

After bootstrap, a missing declared tool follows its declared reacquisition path. Do not burn a development cycle searching arbitrary caches, old worktrees, or unrelated runtimes.
