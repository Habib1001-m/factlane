# S6B.4C-04 Disposable Codex/Hermes Acceptance Runbook

```text
STATUS=CLOSED_PASS_HISTORICAL_RUNBOOK
ROLE=SUPPORTING_EXECUTION_RUNBOOK_NOT_PARALLEL_AUTHORITY
CURRENT_EXECUTION_AUTHORITY=NO
SLICE=S6B_4C_04_CODEX_HERMES_DISPOSABLE_SHARED_STORE_CONCURRENCY
```

The canonical authority remains `AGENTS.md` and `TASKBOARD.md`. S6B.4C-04 is
CLOSED_PASS; this runbook is retained as accepted historical execution evidence
and only defines its bounded mechanical proof. It is not current execution
authority or a parallel authority.

## Proof boundary

4C-04 has two distinct evidence levels. They must not be conflated.

```text
PROCESS_BOUNDARY_CI_PROOF=REQUIRED
REAL_CODEX_HERMES_EXECUTION_CONTEXT_PROOF=REQUIRED
ACTOR_LABEL_ALONE_IS_REAL_HOST_PROOF=NO
```

The repository integration test may launch two ordinary Python subprocesses with distinct `HOME`, PID, gateway instance, and explicit disposable host binding. A PASS there proves process-isolated shared-store behavior only. It does **not** prove that one process was launched by Codex and the other by Hermes.

The real-host acceptance reuses the exact tracked harness but each `actor` command must be launched from its actual execution context: one by Codex and one by Hermes. The actor label is an explicit trusted launcher binding for the disposable gateway; the launch provenance is external accepted evidence and must not be inferred from `HOME`, PID, hostname, cwd, credentials, or the label itself.

## Read-only host-home isolation

A real Codex execution-context probe on the first 4C-04 candidate established that the host may expose the Owner home as read-only. The pinned `mcp-memory-service` initializes a writable base directory at import time and otherwise defaults to `~/.local/share/mcp-memory`.

The tracked 4C-04 actor harness therefore sets `MCP_MEMORY_BASE_DIR` **inside the actor process only**, before importing FactLane/backend modules, to:

```text
MCP_MEMORY_BASE_DIR=<RUN_DIR>/upstream-runtime/<actor>
```

This is disposable process-local runtime state, not live host configuration. It does not copy, alter, chmod, or write the real Owner memory directory. Codex/Hermes actor acceptance must succeed even when their effective `HOME` is read-only. The integration regression deliberately uses read-only actor homes to enforce this boundary.

Do not work around a failed actor by changing permissions under the real home, installing another backend, or setting a persistent host-global environment variable.

## Bounded actor phase telemetry

A later real Codex diagnostic established that Codex can read and write the same host-visible acceptance path, while the actor process can remain alive without reaching the tracked write barrier. To localize that pre-barrier stall without changing product behavior, the acceptance harness records one atomically replaced last-known phase file per actor:

```text
phases/codex-disposable.json
phases/hermes-disposable.json
```

Possible phases are:

```text
BOOTSTRAP_PRE_IMPORT
IMPORT_COMPLETE
ACTOR_ENTER
MANIFEST_LOADED
OPEN_ADAPTER_START
OPEN_ADAPTER_COMPLETE
GATEWAY_READY
DISPATCH_START
BARRIER_READY
BARRIER_RELEASED
DISPATCH_COMPLETE
VERSION_CONFLICT
RESULT_WRITTEN
```

This telemetry is acceptance instrumentation only. It is not authoritative project state, does not prove real-host provenance, and does not change the CAS or storage contract. If an actor is interrupted or stalls, its last phase identifies the narrow component boundary to investigate next. A phase value alone is never an acceptance PASS.

## Established execution truth

The established candidate was exercised in three bounded contexts. Actual Codex execution under its normal sandbox could read and write the shared evidence path but repeatedly stalled at `OPEN_ADAPTER_START`. The same candidate and Python 3.11 environment reached `BARRIER_READY` from the Owner shell and from an actual Codex execution context launched with:

```text
codex --sandbox danger-full-access
```

This localizes the failure boundary to Codex sandbox interaction, not FactLane CAS/storage behavior.

```text
CODEX_SANDBOX_INTERACTION=CONFIRMED
PYMILVUS_CAUSE=REJECTED
PYTHON_3_14_RUNTIME_DRIFT=REJECTED
THREAD_AFFINITY_CAUSE=REJECTED
```

No `pymilvus` addition, supported-Python-range pin or narrowing, or storage/backend change follows from this evidence.

## Final real-host acceptance execution profile

For the final 4C-04 real-host acceptance only:

- use the already-established Python 3.11 acceptance environment built from the same locked dependencies used by canonical CI;
- treat that Python 3.11 choice as an acceptance reproducibility choice, not a new FactLane product Python-support restriction;
- launch the Codex actor from an actual Codex session started explicitly with `codex --sandbox danger-full-access`;
- treat `--sandbox danger-full-access` as an acceptance-only execution profile; do not persist it into global Codex configuration or describe it as a production requirement.

Disposable `HOME` isolation below is acceptance isolation only. `HOME` does not establish real-host provenance, and the actor label does not establish real-host provenance. Actual Codex/Hermes launcher provenance remains external accepted evidence.

## Reconciled boundary

This is a documentation-only reconciliation. The following boundaries remain unchanged:

```text
DOCUMENTATION_RECONCILIATION_ONLY=YES
COORDINATOR_INTRODUCED=NO
PRODUCT_LOCK_BACKOFF_CHANGES=NONE
STORAGE_BACKEND_BEHAVIOR_CHANGE=NONE
SCHEMA_CHANGE=NONE
BACKEND_PIN_CHANGE=NONE
PUBLIC_TOOL_CHANGE=NONE
EMBEDDING_CONCURRENCY_WORK=NOT_STARTED
CRASH_INJECTION=NONE
NATIVE_MEMORY_MUTATION=NONE
HOST_GLOBAL_CONFIGURATION_MUTATION=NONE
S6B_4C_05_WORK=NOT_STARTED
```

## Why the harness uses a deterministic provider

4C-04 changes only the execution-context/process dimension. The harness therefore uses an acceptance-only deterministic embedding provider while preserving the FactLane adapter, gateway, SQLite-vec storage boundary, schema, and pinned backend mechanics.

This deliberately avoids exercising external/local embedding runtime contention. Async embedding concurrency and exact provider/backend runtime proof belong to 4C-05.

## Disposable-only invariants

The run must use one new path under the ignored evidence root:

```text
.factlane-local/evidence/s6b4c-04/<fresh-run-id>/
```

The harness must not mutate live Codex/Hermes configuration, credentials, native memory, global MCP registration, production data, the backend pin, `uv.lock`, schema, or embedding model identity.

## Prepare once

From the candidate FactLane checkout, after `uv sync --frozen --dev` has produced the repository-local environment:

```bash
RUN_DIR="$(pwd)/.factlane-local/evidence/s6b4c-04/run-001"
./.venv/bin/python tools/s6b4c04_disposable_shared_store.py prepare --run-dir "$RUN_DIR"
mkdir -p "$RUN_DIR/homes/codex-disposable" "$RUN_DIR/homes/hermes-disposable"
```

`prepare` creates a new disposable SQLite store, seeds exactly one current revision, and writes a non-secret manifest. The run directory must not already exist.

## Actual Codex execution context

Start the actual Codex session explicitly with `codex --sandbox danger-full-access`. From that actual Codex execution context, launch:

```bash
HOME="$RUN_DIR/homes/codex-disposable" \
  ./.venv/bin/python tools/s6b4c04_disposable_shared_store.py actor \
  --run-dir "$RUN_DIR" \
  --actor codex-disposable
```

The command waits at the tracked write barrier until the Hermes actor has also completed the same pre-write point.

## Actual Hermes execution context

Launch this command **from the actual Hermes execution context** against the same checkout-visible run directory:

```bash
HOME="$RUN_DIR/homes/hermes-disposable" \
  ./.venv/bin/python tools/s6b4c04_disposable_shared_store.py actor \
  --run-dir "$RUN_DIR" \
  --actor hermes-disposable
```

Either actor may be started first. The first waits for the second; no timing-based race is required.

The `HOME=...` assignments are process-local and are the only HOME overrides for these actor launches; do not export or persist them. The tracked harness continues to set its own process-local `MCP_MEMORY_BASE_DIR=<RUN_DIR>/upstream-runtime/<actor>` before importing FactLane/backend modules.

## Verify after both actors return

```bash
./.venv/bin/python tools/s6b4c04_disposable_shared_store.py verify --run-dir "$RUN_DIR"
```

Required verifier result:

```text
PROCESS_BOUNDARY_PROOF=PASS
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

The final verifier result must derive `DISTINCT_EFFECTIVE_HOMES=YES` from the two recorded disposable HOME paths. A shared or inherited effective HOME is a verifier failure, regardless of actor labels or PIDs.

For 4C-04 closure, the surrounding accepted evidence must additionally establish that the two actor commands were actually launched by the Codex and Hermes execution contexts. The verifier does not manufacture that provenance.

## Evidence to retain locally

Keep the fresh disposable run directory until the Owner/Advisor accepts or rejects the result. It contains only disposable proof state:

```text
manifest.json
phases/codex-disposable.json
phases/hermes-disposable.json
ready/codex-disposable.json
ready/hermes-disposable.json
results/codex-disposable.json
results/hermes-disposable.json
homes/codex-disposable/
homes/hermes-disposable/
shared.db
upstream-runtime/codex-disposable/
upstream-runtime/hermes-disposable/
```

Capture the three command outputs (`prepare`, both actors, `verify`) and the candidate Git HEAD in the local evidence packet. Do not commit the database or evidence directory.

## Closure boundary

A CI subprocess PASS alone is insufficient for `S6B_4C_04=CLOSED_PASS`. Closure requires:

1. exact tracked harness and tests green in repository CI;
2. the same candidate bytes used for the real-host run;
3. actual Codex and actual Hermes execution-context launch provenance;
4. verifier PASS with one winner / one `VERSION_CONFLICT` and no partial loser state;
5. exact final-head full repository verification;
6. normal PR + Owner/Advisor merge disposition.

4C-04 does not authorize 4C-05, crash injection, live configuration mutation, native-memory mutation, global registration, or production migration.
