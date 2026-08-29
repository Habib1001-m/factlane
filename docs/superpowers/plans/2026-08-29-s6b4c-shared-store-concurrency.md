# S6B.4C Shared-Store Concurrency Implementation Plan

> **For Hermes:** Execute this plan task-by-task with strict TDD. Do not start a later slice until its predecessor is verified and the Owner/Advisor gate is clear.

**Goal:** Establish a portable, immutable transport-bound host identity and one shared gateway over the existing five-operation `MemoryAdapter`, with a disposable sequential cross-gateway visibility proof. Do not implement atomic CAS, lost-update prevention, or concurrent writers in this slice.

**Architecture:** Keep identity at the trusted transport/server construction boundary. A small project-neutral `HostBinding` is created outside MCP request payloads, then an immutable `MemoryGateway` validates request identity claims, dispatches to the existing adapter, and adds a bounded non-secret host-binding audit projection. The stdio server supplies `TRANSPORT_KIND=stdio`; no host identity is derived from HOME, hostname, cwd, username, credentials, or `runtime_agent_id`.

**Tech Stack:** Python 3.11+, stdlib dataclasses/uuid/argparse, existing FastMCP server, existing FactLane adapter/storage/contract, pytest, and the existing lockfile.

---

## Scope guard

This execution covers 4C-02 only:

```text
4C-02 transport-bound host identity / shared gateway
```

4C-01 is CLOSED_PASS. The campaign boundary remains:

```text
4C-01 CLOSED_PASS
4C-02 CURRENT
4C-03 NOT_STARTED
4C-04 NOT_STARTED
4C-05 NOT_STARTED
4C-06 NOT_STARTED
```

4C-03 atomic multi-client revision/CAS, lost-update prevention, simultaneous writers, crash injection, and any new coordination mechanism are forbidden here.

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

## Current execution: S6B.4C-02 transport-bound host identity and shared gateway

### Task 9: Write the 4C-02 RED contract tests

**Objective:** Define the immutable binding, request-claim denial, host/scope separation, bounded audit, unbound fail-closed behavior, public five-tool surface, disposable sequential visibility, secret exclusion, and carried missing-Git regression before production code.

**Files:**
- Create: `tests/unit/test_gateway.py`
- Modify: `tests/unit/test_execution_context.py` only for `MISSING_GIT_BINARY_EXPLICIT_REGRESSION_TEST`

**Required cases:**

- unbound gateway fails closed;
- invalid/empty/oversized/control-bearing/sensitive binding fails closed;
- request cannot override a bound host identity, including a matching fake claim;
- transport/host mismatch fails closed;
- binding and audit are immutable for the gateway lifetime;
- separate gateway instances receive distinct internally generated IDs;
- host identity never becomes `scope.agent_id`, and the scope remains unchanged;
- audit host binding is gateway-owned and excludes request identity, secrets, config, and environment material;
- one disposable store is sequentially visible across two differently bound gateways;
- exactly five public tools remain and no identity/admin tool appears;
- missing Git binary regression remains fail-closed.

**RED verification:**

```bash
uv run pytest tests/unit/test_gateway.py -q
```

Expected: collection/import failure for the not-yet-created gateway contract, plus the explicit missing-Git regression failing if its production behavior is absent. Fix only test setup errors before implementation.

### Task 10: Implement the immutable HostBinding

**Objective:** Add the smallest validated frozen binding value object with bounded non-secret fields and an internal gateway instance identity.

**Files:**
- Create: `src/factlane/gateway.py`

**Constraints:**

- explicit trusted `bound_host_id` and `binding_source` only;
- `transport_kind` is selected by the gateway/server implementation (`stdio` in this slice);
- never derive identity from request payload, HOME, hostname, cwd, username, credentials, or `runtime_agent_id`;
- reject non-string, empty, oversized, newline/control-bearing, or sensitive values;
- generate `gateway_instance_id` internally and expose it read-only;
- no cryptographic principal-authentication claim.

**Verification:**

```bash
uv run pytest tests/unit/test_gateway.py::test_binding_validation_and_immutability -q
```

### Task 11: Implement the shared gateway dispatch boundary

**Objective:** Wrap the existing `MemoryAdapter` without changing its scope semantics or adding tools.

**Files:**
- Modify: `src/factlane/gateway.py`

**Constraints:**

- reject reserved top-level identity claims with bounded `AdapterError` codes;
- reject an unbound gateway before adapter dispatch;
- dispatch only the existing five operation names;
- copy the adapter response before adding `audit.host_binding` so the adapter remains transport-neutral;
- add only bounded, non-secret, gateway-owned host-binding audit data;
- do not mutate adapter scope or payload identity.

**Verification:**

```bash
uv run pytest tests/unit/test_gateway.py -q
```

### Task 12: Wire the stdio server through the gateway

**Objective:** Require explicit startup binding and use the shared gateway as the sole server dispatch layer while preserving exactly five MCP tools.

**Files:**
- Modify: `src/factlane/server.py`
- Modify: `tests/acceptance/s6b4b_pilot.py`

**Constraints:**

- add a narrow required `--host-id` launcher argument and optional bounded `--binding-source`;
- build `HostBinding(..., transport_kind="stdio", ...)` in server construction, not from request fields;
- missing/invalid host binding fails closed before an operational server starts;
- existing disposable MCP acceptance probes pass an explicit non-secret host id;
- use the gateway for all five tool handlers;
- do not add identity/admin/login/register tools or alter `MemoryAdapter.tool_names()`.

**Verification:**

```bash
uv run factlane --help
uv run pytest tests/unit/test_gateway.py -q
```

### Task 13: Prove disposable sequential visibility and final scope

**Objective:** Run two separately bound gateway instances against one disposable FactLane adapter/store and prove only sequential post-commit visibility.

**Files:**
- Modify: `tests/unit/test_gateway.py`
- Create locally only: `.factlane-local/evidence/s6b4c-02/`

**Verification:**

```text
POST_COMMIT_VISIBILITY=PASS_SEQUENTIAL_FOUNDATION
CONCURRENT_WRITE_TEST_RUN=NO
ATOMIC_CAS_IMPLEMENTATION=NONE
LOST_UPDATE_CAMPAIGN=NOT_STARTED
NATIVE_MEMORY_MUTATION=NONE
LIVE_CODEX_CONFIG_MUTATION=NONE
LIVE_HERMES_CONFIG_MUTATION=NONE
GLOBAL_MCP_REGISTRATION=NONE
BACKEND_PIN_CHANGE=NONE
EMBEDDING_CHANGE=NONE
UV_LOCK_CHANGE=NONE
```

Then run the complete repository gate, record raw output only under the ignored evidence root, and leave the branch for one fresh bounded independent reviewer before commit.

## Later-slice handoff notes (not executed here)

- 4C-02 must bind host identity to transport/gateway context before any shared write-plane claim.
- 4C-03 must prove single-winner atomic revision/CAS semantics across independent clients/processes.
- 4C-04 must use disposable stores and separate Codex/Hermes execution contexts.
- 4C-05 must preserve exact embedding/model/backend identities while testing async contention.
- 4C-06 must inject process termination/crash boundaries and rerun final acceptance.
