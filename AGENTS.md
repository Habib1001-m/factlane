# FactLane Agent Bootstrap

FactLane is a local-first, multi-host governed memory plane for AI agents. Its core rule is **share facts, not context**. Memory is supporting state, never execution authority.

## Mandatory read order

1. `AGENTS.md`
2. `TASKBOARD.md`
3. the active approved spec/plan named by `TASKBOARD.md`
4. only the code and documentation relevant to the active task

## Authority and execution rules

- Repository truth and accepted evidence outrank summaries and remembered context.
- `TASKBOARD.md` is the one live project board. Update it in place whenever project truth, debt, closure, or the next action changes.
- Work on a task branch or isolated worktree. Do not develop directly on `main`.
- Do not expand scope silently. Stop on unresolved authority, provenance, compatibility, security, or acceptance ambiguity.
- Destructive Git/history rewrites, deletion of durable state, credential changes, live host configuration changes, production deployment, and external side effects require an explicit Owner gate.

## Environment policy

```text
MEMORY_IS_SUPPORTING_STATE_NOT_EXECUTION_AUTHORITY
REUSE_FIRST
VERIFY_BEFORE_REUSE
PROVENANCE_ALWAYS
REUSE_THE_ASSET_DO_NOT_INHERIT_THE_OWNER
NO_UNBOUNDED_TOOL_HUNTING_AFTER_BOOTSTRAP
NO_LIVE_HOST_MUTATION_WITHOUT_OWNER_GATE
```

`uv.lock` owns Python dependency resolution. External runtime/build identities belong in `environment-provenance.json`. A cache may seed an installation but must not become hidden runtime authority.

Hermes may operate the environment, but FactLane must not depend on Hermes Python, site-packages, configuration, or caches. Host-specific integration belongs at the edge.

## Verification

Use TDD for behavior changes. Run the smallest relevant test first, then the complete repository verification required by `TASKBOARD.md` before claiming completion. A passing prior run is not evidence for a changed HEAD.

Architecture lives in `docs/ARCHITECTURE.md`; workflow governance lives in `docs/GOVERNANCE.md`.
