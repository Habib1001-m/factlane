# S6B.4C Shared-Store Concurrency Implementation Plan

> **Current execution:** S6B.4C-03 only. Closed slices are carried forward as accepted evidence; do not replay their implementation workflows.

**Goal:** Prove and implement atomic single-winner revision/CAS semantics for independent FactLane clients sharing one disposable store, preventing lost updates while preserving the existing public `memory_update` contract and pinned backend ownership boundary.

**Architecture:** Keep the adapter's early `expected_revision` validation for fast sequential rejection, but make the authoritative CAS decision inside `SQLiteVecEngine.write_record` after `BEGIN IMMEDIATE` and before any new adapter/native/vector row is inserted. The expected parent must still be `VALIDATED_CURRENT` and must match the new record's `parent_record_id`. The pinned backend continues to own SQLite WAL, `busy_timeout`, and bounded `locked`/`busy` retry. No coordinator service or duplicate lock/backoff layer is introduced.

**Tech Stack:** Python 3.11+, existing `MemoryAdapter`, existing `SQLiteVecEngine`, exact pinned `mcp-memory-service` / SQLite-vec backend, pytest, and the existing lockfile.

---

## Authority and carried-forward state

```text
4C-01 CLOSED_PASS
4C-02 CLOSED_PASS
4C-03 CURRENT
4C-04 NOT_STARTED
4C-05 NOT_STARTED
4C-06 NOT_STARTED
```

4C-01 established exact execution-context preflight. 4C-02 established transport-bound host identity and the shared gateway plus sequential post-commit visibility. Those results are inputs to this slice, not workflows to rerun.

The active contract is `docs/S6B_4C_SHARED_STORE_CONCURRENCY_SPEC.md`.

## Scope guard

This execution covers only:

```text
S6B.4C-03 — Atomic Multi-Client Revision/CAS & Lost-Update Prevention
```

Allowed implementation surface:

- `src/factlane/storage.py` for the transaction-local CAS boundary;
- `tests/integration/test_atomic_cas.py` for deterministic independent-client proof;
- the active S6B.4C spec/plan and canonical taskboard only as required to reconcile project truth.

Do not implement:

- real Codex/Hermes simultaneous host execution (4C-04);
- async embedding contention or provider concurrency (4C-05);
- process/crash injection (4C-06);
- retention/compaction/reclaim/recovery (S6B.4D);
- native-memory bootstrap, migration, or production registration (S6B.5);
- live Codex/Hermes configuration mutation or global MCP registration;
- backend pin, schema, `uv.lock`, or embedding-model changes.

## Task 1: Confirm the actual lost-update gap

**Objective:** Verify from current code that process-local serialization is not multi-client CAS.

Read only:

- `AGENTS.md`
- `TASKBOARD.md`
- active spec/plan
- `docs/ARCHITECTURE.md`
- `docs/GOVERNANCE.md`
- `src/factlane/adapter.py`
- `src/factlane/storage.py`

Required finding:

```text
ADAPTER_EXPECTED_REVISION_PRECHECK=SEQUENTIAL_ONLY
ADAPTER_WRITE_LOCK=INSTANCE_LOCAL_ONLY
STORAGE_TRANSACTION=BEGIN_IMMEDIATE
PARENT_CURRENT_RECHECK_INSIDE_TRANSACTION=ABSENT_BEFORE_4C03
LOST_UPDATE_RACE=PRESENT
```

## Task 2: Write deterministic RED multi-client proof

**Objective:** Demonstrate the bug without relying on scheduler luck.

Create `tests/integration/test_atomic_cas.py` with two independent `SQLiteVecEngine` instances and two independent `MemoryAdapter` instances sharing one disposable database. Seed one `VALIDATED_CURRENT` record, let both clients complete their read of revision 1, then release both storage writes through a test-only barrier.

First RED case:

- both clients call `REVERIFY` with `expected_revision=1`;
- different idempotency keys and verification payloads;
- expected contract is exactly one success and one `VERSION_CONFLICT`;
- pre-fix code must fail because both writers can succeed.

RED evidence must preserve the passing baseline tests and fail only for the intended missing CAS behavior.

## Task 3: Implement the smallest transaction-local CAS

**Objective:** Move the authoritative parent-current decision into the same write transaction that creates the successor and supersedes its parent.

Modify only `src/factlane/storage.py` unless a failing test proves another production surface is necessary.

Inside `write_record`, after `BEGIN IMMEDIATE` and before any insert:

1. if `supersede_record_id` is present, read that parent inside the transaction;
2. require that it exists and is still `VALIDATED_CURRENT`;
3. require `record.parent_record_id == supersede_record_id`;
4. otherwise raise `AdapterError("VERSION_CONFLICT", ...)`;
5. on success, write the new adapter/native/vector state and supersede that checked parent within the same transaction;
6. any failure rolls back the transaction completely.

Do not add a new lock, retry loop, service, schema, or public operation.

## Task 4: Expand proof to both update modes and lineage invariants

**Objective:** Prove the transaction rule protects both existing update modes.

`REVERIFY` assertions:

```text
SUCCESSFUL_WRITERS=1
VERSION_CONFLICT_WRITERS=1
CURRENT_RECORD_COUNT=1
WINNER_REVISION=2
PARENT_RECORD_ID=ORIGINAL_RECORD
HISTORY_REVISIONS=1,2
CURRENT_LINEAGE_FORKS=0
```

`REPLACE` assertions:

```text
SUCCESSFUL_WRITERS=1
VERSION_CONFLICT_WRITERS=1
WINNER_NEW_MEMORY_ID=YES
WINNER_REVISION=1
WINNER_PARENT_RECORD_ID=ORIGINAL_RECORD
OLD_RECORD=SUPERSEDED
VALIDATED_CURRENT_COUNT=1
SUPERSEDED_COUNT=1
PARTIAL_LOSER_ROWS=0
```

## Task 5: Verify exact candidate head

Use the repository's frozen dependency contract and full CI-equivalent checks on the exact final candidate head:

```bash
uv sync --frozen --dev
uv run pytest -q
uv run python -c "import factlane; print(factlane.__name__)"
uv run factlane --help
```

Inspect the final diff and confirm:

```text
BACKEND_PIN_CHANGE=NONE
UV_LOCK_CHANGE=NONE
SCHEMA_CHANGE=NONE
EMBEDDING_IDENTITY_CHANGE=NONE
PUBLIC_TOOL_SURFACE_CHANGE=NONE
LIVE_CODEX_CONFIG_MUTATION=NONE
LIVE_HERMES_CONFIG_MUTATION=NONE
GLOBAL_MCP_REGISTRATION=NONE
NATIVE_MEMORY_MUTATION=NONE
4C04_IMPLEMENTATION=NONE
4C05_IMPLEMENTATION=NONE
4C06_IMPLEMENTATION=NONE
```

## Task 6: PR handoff and stop

Push only the task branch and open a PR to `main`. Record:

- base SHA;
- exact final head SHA;
- RED commit/run evidence;
- GREEN exact-head CI evidence;
- changed-file scope;
- no later-slice authorization or implementation.

Governance remains:

```text
PR_REQUIRED=TRUE
CI_BEFORE_MERGE=TRUE
OWNER_OR_ADVISOR_MERGE_GATE=REQUIRED
SELF_MERGE=NO
```

After PR creation, stop for Owner/Advisor decision. Do not merge and do not start 4C-04. Canonical merged-closure status is reconciled in `TASKBOARD.md` only after the merge boundary changes repository truth.
