# FactLane Agent Bootstrap

FactLane is a local-first, multi-host governed memory plane for AI agents. Its core rule is **share facts, not context**. Memory is supporting state, never execution authority.

## Instruction-stack contract

- This is the primary project-local layer and is sufficient for normal FactLane work.
- Parent workspace reference: `/home/habib1001/workspace/AGENTS.md` (`AUTO_READ_PARENT=NO`).
- Use parent/host instructions already injected by the runtime; open the parent manually only for a material unresolved cross-scope conflict, intentional cross-project/workspace work, or instruction-stack maintenance.

## Project-local read order

1. `AGENTS.md`
2. `TASKBOARD.md`
3. the active approved spec/plan named by `TASKBOARD.md`
4. only the code and documentation relevant to the active task

## Authority and execution rules

- Repository truth and accepted evidence outrank summaries and remembered context.
- `TASKBOARD.md` is the one live project board. Update it in place whenever project truth, debt, closure, or the next action changes.
- A bounded envelope may come from the Owner, or be transmitted/narrowed by a Team Lead inside an active Owner-authorized workflow and within delegated scope. It authorizes its named normal action classes without repeated approval for ordinary mechanical substeps, but cannot create or broaden guarded authority or imply cross-project/host-global authority.
- Work on a task branch or isolated worktree. Do not develop directly on `main`.
- Do not expand scope silently. Stop on unresolved authority, provenance, compatibility, security, or acceptance ambiguity that can materially change the task.
- Destructive Git/history rewrites; deletion of authoritative, unique, protected, active/uncommitted, credential-bearing, live-data, accepted-evidence, or materially uncertain state; credential changes; live host configuration changes; production deployment; and external side effects require Owner-originated authority, either directly or already carried by an Owner-approved active envelope. Proven disposable/reproducible, non-authoritative, with no active consumer, recoverable/recreatable local state may be cleaned as ordinary local hygiene when relevant to the active project/workspace scope and not prohibited by the current envelope, without per-item approval.

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
