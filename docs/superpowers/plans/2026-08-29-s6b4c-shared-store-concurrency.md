# S6B.4C Shared-Store Concurrency Implementation Plan

> **For execution agents:** Execute only the current Owner-authorized slice with strict TDD. Closed slices below are retained as implementation history and must not be replayed.

**Goal:** Establish the S6B.4C shared-store concurrency campaign slice-by-slice. S6B.4C-01, 4C-02, and 4C-03 are closed; the current slice is S6B.4C-04 Codex/Hermes disposable shared-store concurrency.

**Architecture:** Preserve the accepted execution-context, transport-bound gateway, and transaction-local CAS boundaries from 4C-01/02/03. For 4C-04, vary only the execution-context/process boundary: two separately launched actors use independent FactLane gateway/adapter/storage instances over one disposable database and synchronize only at a test acceptance barrier after reading the same current revision. The accepted SQLite transaction remains the sole CAS authority; no coordinator or new lock/retry layer is introduced.

**Tech Stack:** Python 3.11+, stdlib asyncio/subprocess/pathlib/json, existing FactLane gateway/adapter/storage/contract, pytest, and the existing lockfile.

---

## Scope guard

Current execution:

```text
4C-01 CLOSED_PASS
4C-02 CLOSED_PASS
4C-03 CLOSED_PASS
4C-04 CURRENT
4C-05 NOT_STARTED
4C-06 NOT_STARTED
```

The detailed Tasks 1–17 below are retained as closed 4C-01/02/03 implementation history only. Do not rerun them as gates for 4C-04.

4C-05 async embedding contention and 4C-06 crash injection remain forbidden in the current slice.

Do not implement retention/compaction/reclaim/recovery (S6B.4D), native-memory bootstrap or migration (S6B.5), live configuration, registration, backend-pin changes, embedding-model changes, or reopening accepted 4B results.

## Closed implementation history — S6B.4C-01 and S6B.4C-02

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

## Closed implementation history — S6B.4C-02 transport-bound host identity and shared gateway

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

## Later-slice handoff notes

- 4C-01, 4C-02, and 4C-03 are CLOSED_PASS; their detailed tasks are retained for provenance only.
- 4C-04 is the current Owner-authorized slice and must use one disposable store plus actual separate Codex/Hermes execution contexts.
- 4C-05 must preserve exact embedding/model/backend identities while testing async contention.
- 4C-06 must inject process termination/crash boundaries and rerun final acceptance.

## Closed implementation history — S6B.4C-03 atomic multi-client revision/CAS and lost-update prevention

### Task 14: Write the deterministic RED lost-update proof

Use two independent `SQLiteVecEngine` instances and two independent `MemoryAdapter` instances against one disposable database. Seed one current record, allow both clients to finish reading the same revision, then release both writes through a test-only barrier.

The pre-fix implementation must demonstrate the defect by allowing both writers to succeed. The intended RED contract is exactly one success and one `VERSION_CONFLICT`.

### Task 15: Implement transaction-local parent-current CAS

Modify only `src/factlane/storage.py` unless a failing test proves more production surface is required. After `BEGIN IMMEDIATE` and before any insert, require the supplied `supersede_record_id` to exist, remain `VALIDATED_CURRENT`, and equal the successor's `parent_record_id`. Failure raises `VERSION_CONFLICT` and rolls back. Success writes the successor/vector and supersedes that checked parent in the same transaction.

No new lock, retry loop, coordinator, schema, public tool, backend pin, lockfile, or embedding identity is allowed.

### Task 16: Prove REVERIFY and REPLACE invariants

For both modes prove:

```text
SUCCESSFUL_WRITERS=1
VERSION_CONFLICT_WRITERS=1
CURRENT_LINEAGE_FORKS=0
CURRENT_RECORD_COUNT=1
PARTIAL_LOSER_ROWS=0
```

Additionally prove REVERIFY advances one linear revision and REPLACE creates exactly one new current logical memory while the old parent is superseded exactly once.

### Task 17: Exact-head verification and PR handoff

Run the exact final branch head through:

```bash
uv sync --frozen --dev
uv run pytest -q
uv run python -c "import factlane; print(factlane.__name__)"
uv run factlane --help
```

Inspect the final diff and confirm no change to `uv.lock`, backend pin, schema, embedding identity, public five-tool surface, live configuration, native memory, global MCP registration, or 4C-04/05/06 implementation.

The 4C-03 implementation and canonical merged closure are complete. Do not replay this task as a gate for 4C-04.

## Current execution — S6B.4C-04 Codex/Hermes disposable shared-store concurrency

### Task 18: Add the deterministic process-boundary RED proof

Create `tests/integration/test_disposable_host_concurrency.py` before the tracked harness exists. The test launches two independent Python processes with distinct effective `HOME` values and explicit `codex-disposable` / `hermes-disposable` bindings against one fresh disposable run directory.

The RED result must be attributable to the missing 4C-04 harness or a concrete 4C-04 contract failure, not to an unrelated existing test regression.

### Task 19: Implement the minimal reusable disposable harness

Create `tools/s6b4c04_disposable_shared_store.py` with `prepare`, `actor`, and `verify` commands.

Constraints:

- reuse the existing `SQLiteVecEngine`, `MemoryAdapter`, `HostBinding`, and `MemoryGateway`;
- use one fresh disposable SQLite database only;
- install synchronization only at the acceptance write seam after both actors have read the same parent/revision;
- retain the accepted 4C-03 transaction as the sole CAS authority;
- do not add product locks, retries, coordinators, schema, tools, or server paths;
- use a deterministic acceptance-only embedding provider so embedding contention remains 4C-05 work;
- capture only bounded non-secret actor/PID/HOME/gateway/outcome evidence;
- keep all run state under `.factlane-local/evidence/s6b4c-04/`.

Required process-level assertions:

```text
DISTINCT_PIDS=YES
DISTINCT_EFFECTIVE_HOMES=YES
DISTINCT_GATEWAY_INSTANCES=YES
DISTINCT_HOST_BINDINGS=YES
SHARED_PRE_READ_PARENT=YES
SUCCESSFUL_WRITERS=1
VERSION_CONFLICT_WRITERS=1
CURRENT_RECORD_COUNT=1
CURRENT_LINEAGE_FORKS=0
PARTIAL_LOSER_ROWS=0
WINNER_MATCHES_CURRENT=YES
WINNER_AUDIT_MATCHES_HOST_BINDING=YES
LOST_UPDATE_PREVENTION=PASS
```

### Task 20: Run the same harness from actual Codex and Hermes execution contexts

Use the exact candidate checkout and one shared disposable `RUN_DIR`. Prepare once. Then launch the `codex-disposable` actor command from the actual Codex execution context and the `hermes-disposable` actor command from the actual Hermes execution context. Either may start first; it waits at the deterministic barrier for its peer.

Established diagnostics record `CODEX_SANDBOX_INTERACTION=CONFIRMED`: normal Codex
sandbox execution stalled at `OPEN_ADAPTER_START`, while the same candidate and Python
3.11 environment reached `BARRIER_READY` from the Owner shell and from an actual Codex
context launched with `codex --sandbox danger-full-access`. The alternate hypotheses
`PYMILVUS_CAUSE=REJECTED`, `PYTHON_3_14_RUNTIME_DRIFT=REJECTED`, and
`THREAD_AFFINITY_CAUSE=REJECTED`; this does not indicate a FactLane CAS/storage defect.

For final real-host acceptance only, use the established Python 3.11 environment from
the same locked dependencies as canonical CI and the acceptance-only Codex launch
profile `codex --sandbox danger-full-access`. The separate process-local HOME paths are
acceptance isolation only; HOME and actor labels do not establish provenance, and actual
Codex/Hermes launch provenance remains external accepted evidence. The tracked harness
continues to set process-local `MCP_MEMORY_BASE_DIR=<RUN_DIR>/upstream-runtime/<actor>`.

The verifier does not establish host provenance. Accepted surrounding evidence must prove which real execution context launched each actor. Actor labels, `HOME`, PID, hostname, cwd, and credentials are not substitutes for that provenance.

Follow `docs/S6B_4C_04_DISPOSABLE_HOST_ACCEPTANCE_RUNBOOK.md` exactly. Keep the disposable database and JSON evidence untracked.

A process-only CI PASS is insufficient to complete this task.

### Task 21: Reconcile, verify exact final head, and hand off by PR

After the real Codex/Hermes acceptance passes:

```bash
uv sync --frozen --dev
uv run pytest -q
uv run python -c "import factlane; print(factlane.__name__)"
uv run factlane --help
```

Audit the final diff and confirm:

```text
PRODUCT_SOURCE_CHANGE=NONE_UNLESS_REAL_HOST_PROOF_FOUND_A_PRODUCT_DEFECT
UV_LOCK_CHANGE=NONE
BACKEND_PIN_CHANGE=NONE
SCHEMA_CHANGE=NONE
EMBEDDING_MODEL_CHANGE=NONE
PUBLIC_TOOL_SURFACE_CHANGE=NONE
LIVE_CODEX_CONFIG_MUTATION=NONE
LIVE_HERMES_CONFIG_MUTATION=NONE
NATIVE_MEMORY_MUTATION=NONE
GLOBAL_MCP_REGISTRATION=NONE
S6B_4C_05_STARTED=NO
S6B_4C_06_STARTED=NO
```

Open a PR to `main`, record the exact candidate head and CI evidence, and stop for Owner/Advisor merge disposition. Do not self-merge. Canonical merged-closure reconciliation follows the merge boundary.
