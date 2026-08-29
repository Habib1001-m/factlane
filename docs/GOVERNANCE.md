# FactLane Governance

## Canonical development loop

```text
repo truth
-> TASKBOARD.md
-> task branch / isolated worktree
-> implementation
-> tests and bounded evidence
-> review
-> commit
-> TASKBOARD/docs reconciliation
-> merge
```

`TASKBOARD.md` is the one live project board. Its version is an internal field. Never create a new numbered taskboard file per cycle.

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
