# S6B.4C Shared-Store Concurrency Implementation Plan

> **For Hermes:** Execute this plan task-by-task with strict TDD. Do not start a later slice until its predecessor is verified and the Owner/Advisor gate is clear.

**Goal:** Establish a portable, fail-closed execution-context preflight and disposable harness before implementing any shared-store concurrency behavior.

**Architecture:** Keep 4C-01 at the host edge. A small Python module reports exact repository, binary, HOME, and context-scoped auth facts without copying or exposing credentials. A read-only native-memory capacity gate is represented explicitly and remains closed when no trustworthy host primitive exists. Later concurrency slices consume this foundation; they are not implemented in this plan execution.

**Tech Stack:** Python 3.11+, stdlib subprocess/pathlib/shutil/argparse, pytest, existing FactLane package and lockfile.

---

## Scope guard

This execution covers 4C-01 only:

```text
4C-01 execution-context + entry harness
```

The campaign order is documented for future work but not executed here:

```text
4C-02 transport-bound host identity / shared gateway
4C-03 atomic multi-client revision/CAS + lost-update prevention
4C-04 Codex/Hermes disposable shared-store concurrency
4C-05 async embedding concurrency + pinned-backend runtime proof
4C-06 process/crash injection + final acceptance
```

Do not implement retention/compaction/reclaim/recovery (S6B.4D), native-memory bootstrap or migration (S6B.5), live configuration, registration, backend-pin changes, embedding-model changes, or reopening accepted 4B results.

## Task 1: Confirm the entry boundary

**Objective:** Prove the fresh baseline and record the current Owner-approved slice before code changes.

**Files:**
- Read: `AGENTS.md`, `TASKBOARD.md`, `docs/ARCHITECTURE.md`, `docs/GOVERNANCE.md`
- Read: `src/factlane/adapter.py`, `src/factlane/storage.py`, `src/factlane/contract.py`, `src/factlane/server.py`, `src/factlane/embeddings.py`
- Modify: `TASKBOARD.md`
- Create: `docs/S6B_4C_SHARED_STORE_CONCURRENCY_SPEC.md`
- Create: `docs/superpowers/plans/2026-08-29-s6b4c-shared-store-concurrency.md`

**Verification:**

```bash
git rev-parse HEAD
git rev-parse origin/main
git status --porcelain
git diff --check
```

Expected: branch is `hermes/s6b4c-entry-preflight`, main baseline is synchronized, and no product source has changed.

## Task 2: Write the RED preflight tests

**Objective:** Define the required report fields, exact-context readiness, redaction, and fail-closed behavior before implementation.

**Files:**
- Create: `tests/unit/test_execution_context.py`

**Test cases:**

- `test_report_distinguishes_effective_and_explicit_owner_home`
- `test_same_binary_can_have_different_context_readiness`
- `test_auth_failure_is_scoped_to_its_execution_context`
- `test_report_redacts_secret_material_and_probe_output`
- `test_safe_serialization_is_bounded_before_output`
- `test_missing_or_invalid_owner_context_fails_closed`
- `test_invalid_owner_object_fails_closed`
- `test_missing_repository_context_fails_closed`
- `test_missing_effective_home_fails_closed`
- `test_null_byte_repository_context_fails_closed`
- `test_null_byte_home_contexts_fail_closed`
- `test_invalid_repository_cwd_type_fails_closed`
- `test_default_auth_probe_uses_exact_home_without_auth_env`
- `test_missing_gh_binary_fails_closed_even_with_injected_probe`
- `test_invalid_gh_binary_fails_closed_even_with_injected_probe`
- `test_native_memory_gate_is_read_only_and_fail_closed_when_unavailable`
- `test_available_capacity_status_does_not_authorize_native_write`
- `test_capacity_pressure_holds_and_remains_closed`
- `test_invalid_capacity_status_holds_and_remains_closed`
- `test_unhashable_capacity_status_holds_and_remains_closed`
- `test_render_neutralizes_multiline_report_values`

**RED verification:**

```bash
uv run pytest tests/unit/test_execution_context.py -q
```

Expected: collection/import failure because the reusable preflight contract does not exist yet. Fix only test setup errors; do not implement production code before the tests fail for the expected missing-contract reason.

## Task 3: Implement the minimal context model

**Objective:** Add the smallest reusable Python API that gathers repository context and classifies exact HOME/auth contexts without copying credentials.

**Files:**
- Create: `src/factlane/execution_context.py`
- Modify: none of `adapter.py`, `storage.py`, `contract.py`, `server.py`, or `embeddings.py`

**Implementation constraints:**

- use argument-list `subprocess.run`, never `shell=True`;
- suppress and discard auth command stdout/stderr;
- use an allowlisted auth environment containing the explicit HOME and safe execution variables only;
- accept explicit owner HOME only from a caller argument or explicit environment contract;
- never read or copy credential files/keyrings;
- report binary presence separately from readiness;
- classify failures with context-specific codes;
- make the auth checker injectable for unit tests;
- render only bounded safe fields.

**GREEN verification:**

```bash
uv run pytest tests/unit/test_execution_context.py -q
```

Expected: all 4C-01 preflight tests pass.

## Task 4: Add the executable entry point

**Objective:** Make the preflight runnable through the package without changing the existing `factlane` MCP CLI.

**Files:**
- Modify: `src/factlane/execution_context.py`

**Interface:**

```bash
uv run python -m factlane.execution_context --actor HERMES --owner-home "$HERMES_REAL_HOME"
```

The command prints safe `KEY=VALUE` facts only. It must never print subprocess output, tokens, credential paths, or secret values.

**Verification:**

```bash
uv run python -m factlane.execution_context --help
uv run python -m factlane.execution_context --actor HERMES --owner-home "$HERMES_REAL_HOME"
```

Expected: help succeeds; the report contains the required fields and context-scoped readiness classifications.

## Task 5: Add the native-memory interim gate

**Objective:** Represent read-only capacity/status introspection without performing a native-memory write or inventing a capacity estimate.

**Files:**
- Modify: `src/factlane/execution_context.py`
- Modify: `tests/unit/test_execution_context.py`

**Verification:**

```bash
uv run pytest tests/unit/test_execution_context.py -q
```

Expected: no-reader result is `CAPACITY_INTROSPECTION=UNAVAILABLE` and future mutation is fail-closed; a supplied safe status reader is classified without authorizing mutation; invalid reader output is `HOLD`.

## Task 6: Capture bounded local evidence

**Objective:** Keep raw execution output private and make the evidence root explicit without tracking it.

**Files:**
- Create locally only: `.factlane-local/evidence/s6b4c-01/entry-preflight.txt`
- Create locally only: `.factlane-local/evidence/s6b4c-01/verification.txt`

**Verification:**

```bash
uv run python -m factlane.execution_context --actor HERMES --owner-home "$HERMES_REAL_HOME" > .factlane-local/evidence/s6b4c-01/entry-preflight.txt
uv run pytest -q > .factlane-local/evidence/s6b4c-01/verification.txt
```

Expected: files are ignored, contain no credential material, and are not included in `git status` or the commit.

## Task 7: Run the complete 4C-01 gate

**Objective:** Verify the exact requested acceptance commands and scan the final diff before commit.

**Commands:**

```bash
git diff --check
uv run pytest -q
uv run python -c "import factlane; print(factlane.__name__)"
uv run factlane --help
```

**Read-only scans:**

```text
NO_SECRET=PASS
NO_PRIVATE_ABSOLUTE_PATH_IN_TRACKED_FILES=PASS
OWNER_CONTEXT_BOARD_TRACKED=NO
NATIVE_MEMORY_MUTATION=NONE
LIVE_CODEX_CONFIG_MUTATION=NONE
LIVE_HERMES_CONFIG_MUTATION=NONE
GLOBAL_MCP_REGISTRATION=NONE
NO_BACKEND_PIN_CHANGE=PASS
NO_UV_LOCK_CHANGE=PASS
```

Do not commit if any required gate fails or if the diff contains files outside the approved 4C-01 set.

## Task 8: Commit and hand off

**Objective:** Commit only verified 4C-01 changes on `hermes/s6b4c-entry-preflight`, push that branch, and open a PR to `main`.

**Allowed tracked files:**

```text
TASKBOARD.md
docs/S6B_4C_SHARED_STORE_CONCURRENCY_SPEC.md
docs/superpowers/plans/2026-08-29-s6b4c-shared-store-concurrency.md
src/factlane/execution_context.py
tests/unit/test_execution_context.py
```

**Commit:**

```bash
git add TASKBOARD.md docs/S6B_4C_SHARED_STORE_CONCURRENCY_SPEC.md \
  docs/superpowers/plans/2026-08-29-s6b4c-shared-store-concurrency.md \
  src/factlane/execution_context.py tests/unit/test_execution_context.py
git commit -m "feat: add S6B.4C execution context preflight"
```

Push only the task branch with the explicit Owner GitHub context. Open a PR with base `main`, report the exact head SHA and CI status, and stop for Owner/Advisor review. Do not self-approve or merge.

## Later-slice handoff notes (not executed here)

- 4C-02 must bind host identity to transport/gateway context before any shared write-plane claim.
- 4C-03 must prove single-winner atomic revision/CAS semantics across independent clients/processes.
- 4C-04 must use disposable stores and separate Codex/Hermes execution contexts.
- 4C-05 must preserve exact embedding/model/backend identities while testing async contention.
- 4C-06 must inject process termination/crash boundaries and rerun final acceptance.
